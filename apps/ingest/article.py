"""內頁全文抽取。

清單頁（RSS 或 HTML）只給標題與連結，全文必須另外抓。全文是後續
幾乎所有工作的前提：

- SimHash 去重——實測標題只有約 21 個 bigram，指紋幾乎全是雜訊，
  逐字相同的通稿與完全無關的文章距離同為 13，無法區分
- L1 結構化抽取——案號、被告、刑度都在內文，標題沒有
- 向量檢索——標題 embedding 的語意訊息不足
- 時間線事實條目——標題無法支撐可查證的事實

**抽取採三層策略**，依站台無關的程度排序：

1. **JSON-LD ``articleBody``**——新聞網站為 SEO 幾乎都提供結構化資料。
   站台無關、改版時最穩定。實測涵蓋中央社、公視、自由、鏡週刊。
2. **站台專屬 CSS 選擇器**——JSON-LD 缺 articleBody 時使用。
   UDN 與中時的選擇器直接沿用既有爬蟲 ``crawler/sites.py``，
   那是對站台實際結構的實證知識。
3. **Playwright 渲染**——SPA 站台（如報導者）純 HTTP 取不到內容。

實測所有站台的內頁皆可用純 HTTP 取得（中時的內頁沒有 Cloudflare），
因此第 1、2 層都走便宜的 ``fetch`` 佇列，只有第 3 層需要 ``browser``。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from apps.core.dates import parse_datetime

logger = logging.getLogger(__name__)

__all__ = [
    "ExtractedArticle", "extract_article", "extract_published_at",
    "BODY_SELECTORS", "NEEDS_BROWSER", "BODY_API_SOURCES",
    "twreporter_body_url", "extract_twreporter_article",
]

#: 站台專屬的內文選擇器。UDN 與中時沿用 crawler/sites.py 的實證值。
BODY_SELECTORS: dict[str, str] = {
    "udn": "section.article-content__editor",
    "chinatimes": "div.article-body",
    "ctee": "article",
    "ettoday": "div.story",
    # 以下有 JSON-LD，選擇器僅作備援
    "cna-society": "div.paragraph",
    "cna-politics": "div.paragraph",
    "cna-mainland": "div.paragraph",
    "cna-finance": "div.paragraph",
    "pts": "article",
    "ltn": "div.text",
    "mirrormedia": "main",
}

#: 純 HTTP 取不到內容、且沒有可用 API 的站台（SPA）。
#: 這類來源的內文目前補不到——``fill_missing_bodies`` 會跳過它們。
#: 報導者曾列在這裡，但 2026-09-02 實測它的 go-api 有單篇全文
#: （見 BODY_API_SOURCES），根本不需要瀏覽器。
NEEDS_BROWSER: frozenset[str] = frozenset()

#: 內文中常見的雜訊行：圖說、記者署名、推廣文字
_NOISE_PATTERNS = (
    re.compile(r"^[▲▼△▽◤◢]"),                    # 圖說開頭符號
    re.compile(r"^（圖[／/].*）$"),
    re.compile(r"^\s*$"),
)

_MIN_BODY_LENGTH = 80


@dataclass
class ExtractedArticle:
    body: str
    published_at: object | None = None      # datetime.date 或 None
    author: str = ""
    title: str = ""                         # 清單頁沒給標題時（如 sitemap）才用得上
    strategy: str = ""                      # 實際生效的抽取策略，供除錯與監測


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _title_from_meta(tree: HTMLParser) -> str:
    """og:title → <title>。給 sitemap 這類只有網址、沒有標題的清單用。

    標題不是可有可無的欄位：它進 ``search_text``，也是轉載歸併
    bigram 指紋的一部分，缺了會讓該文件在檢索與去重上等同隱形。
    """
    node = tree.css_first("meta[property='og:title']")
    if node:
        value = (node.attributes.get("content") or "").strip()
        if value:
            return value
    node = tree.css_first("title")
    return node.text().strip() if node else ""


def _from_json_ld(tree: HTMLParser) -> tuple[str, str, str, str]:
    """回傳 (內文, 發布時間字串, 作者, 標題)。取不到的部分為空字串。"""
    body = date = author = title = ""
    for block in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(block.text())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _walk(data):
            if not isinstance(node, dict):
                continue
            candidate = node.get("articleBody")
            if isinstance(candidate, str) and len(candidate) > len(body):
                body = candidate
            if not date and isinstance(node.get("datePublished"), str):
                date = node["datePublished"]
            if not author:
                raw_author = node.get("author")
                if isinstance(raw_author, dict):
                    author = str(raw_author.get("name") or "")
                elif isinstance(raw_author, str):
                    author = raw_author
            if not title and isinstance(node.get("headline"), str):
                title = node["headline"]
    return body, date, author, title


def _date_from_meta(tree: HTMLParser) -> str:
    for sel in (
        "meta[property='article:published_time']",
        "meta[name='pubdate']",
        "meta[itemprop='datePublished']",
    ):
        node = tree.css_first(sel)
        if node:
            value = (node.attributes.get("content") or "").strip()
            if value:
                return value
    return ""


def extract_published_at(html: str):
    """只抽發布時間，不要求內文長度。

    清單頁入庫的中時／聯合新聞常常有標題與內文、卻沒有時間——
    JSON-LD 解析失敗時仍可能有 ``article:published_time``。
    """
    if not html or not html.strip():
        return None
    tree = HTMLParser(html)
    _, date_text, _, _ = _from_json_ld(tree)
    if not date_text:
        date_text = _date_from_meta(tree)
    return parse_datetime(date_text) if date_text else None


# ------------------------------------------------------- 走站台 API 的內文
# 報導者是 React SPA：文章頁 HTML 有 214KB 但不含內文，選擇器與 JSON-LD
# 都抽不到。它的 go-api 直接給結構化全文，比渲染瀏覽器快也穩定得多。

TWREPORTER_POST_API = "https://go-api.twreporter.org/v2/posts/{slug}?full=true"

#: 內文區塊裡真正有文字的型別。youtube／embeddedcode／image 之類跳過。
_TWREPORTER_TEXT_TYPES = frozenset({
    "unstyled", "blockquote", "quoteby", "annotation", "infobox",
    "header-one", "header-two", "code",
})
#: 內文夾帶的註解標記，如 <!--__ANNOTATION__={...}--> 與 <!--註解文字-->
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")


def twreporter_body_url(article_url: str) -> str:
    """由文章網址組出單篇 API 網址。``/a/{slug}`` → API 的 ``{slug}``。"""
    slug = article_url.rstrip("/").rsplit("/", 1)[-1]
    return TWREPORTER_POST_API.format(slug=slug)


def _twreporter_block_text(block: dict) -> list[str]:
    content = block.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out = []
    for item in content:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            # infobox 之類把內文放在 body，且是 HTML 片段
            out.append(str(item.get("body") or ""))
    return out


def extract_twreporter_article(raw: str) -> ExtractedArticle:
    """解析報導者 go-api 的單篇回應。介面與 ``extract_article`` 一致。"""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"報導者 API 回應不是 JSON：{exc}") from exc

    data = payload.get("data") or {}
    blocks = ((data.get("content") or {}).get("api_data")) or []
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in _TWREPORTER_TEXT_TYPES:
            continue
        for text in _twreporter_block_text(block):
            text = _HTML_COMMENT.sub("", text)
            text = _HTML_TAG.sub("", text).strip()
            if text:
                parts.append(text)

    body = _clean_body("\n".join(parts))
    if len(body) < _MIN_BODY_LENGTH:
        raise ValueError(
            f"報導者 API 未取得足夠內文（{len(body)} 字，門檻 {_MIN_BODY_LENGTH}）"
        )
    return ExtractedArticle(
        body=body,
        published_at=parse_datetime(data.get("published_date") or ""),
        title=" ".join((data.get("title") or "").split())[:512],
        strategy="twreporter-api",
    )


#: 內文不在文章頁 HTML 裡、改打站台自己 API 的來源。
#: slug -> (由文章網址組出 API 網址, 解析器)
BODY_API_SOURCES = {
    "twreporter": (twreporter_body_url, extract_twreporter_article),
}


def _clean_body(raw: str) -> str:
    """去除圖說、空行與過度空白。

    保守處理：只刪明確可辨識的雜訊。誤刪內文的代價（時間線缺事實）
    高於留下幾行圖說（LLM 抽取時會忽略）。
    """
    lines = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if any(pattern.match(line) for pattern in _NOISE_PATTERNS):
            continue
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_article(html: str, *, source_slug: str = "") -> ExtractedArticle:
    """從內頁 HTML 抽出全文與中繼資料。

    抽不到足夠長度的內文時拋 ``ValueError``——靜默回傳空字串會讓
    「站台改版導致全文抽取失效」看起來像「這篇文章本來就很短」，
    與 RSS adapter 的失敗處理原則一致。
    """
    if not html or not html.strip():
        raise ValueError("空的 HTML 回應")

    tree = HTMLParser(html)

    body, date_text, author, title = _from_json_ld(tree)
    strategy = "json-ld"
    if not date_text:
        date_text = _date_from_meta(tree)
    if not title:
        title = _title_from_meta(tree)

    if len(body) < _MIN_BODY_LENGTH:
        selector = BODY_SELECTORS.get(source_slug)
        if selector:
            nodes = tree.css(selector)
            if nodes:
                body = "\n".join(n.text(separator="\n") for n in nodes)
                strategy = f"selector:{selector}"

    body = _clean_body(body)
    if len(body) < _MIN_BODY_LENGTH:
        raise ValueError(
            f"未能抽出足夠的內文（{len(body)} 字，門檻 {_MIN_BODY_LENGTH}）"
            f"——站台結構可能已變更"
        )

    # 必須保留時間精度：JSON-LD 的 datePublished 是完整 ISO 時間戳，
    # 若只取日期會讓同日文章全部變成午夜，破壞所有依時間排序的邏輯。
    published = parse_datetime(date_text) if date_text else None

    return ExtractedArticle(
        body=body,
        published_at=published,
        author=(author or "").strip()[:128],
        title=" ".join((title or "").split())[:512],
        strategy=strategy,
    )
