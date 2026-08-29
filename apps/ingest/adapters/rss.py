"""通用 RSS / Atom adapter。

規格 §4.9 的採集策略改變：主力從 Playwright 改為 RSS 輪詢，
Playwright 僅保留給無 feed 或有 Cloudflare 的站台。多數台灣新聞網
都提供 RSS，成本差一到兩個數量級。

RSS 的摘要欄位通常只有前幾句，不是全文。因此本 adapter 只負責
取得文章清單與中繼資料；全文由後續的內頁抓取任務補上。
這個分離讓清單輪詢可以很頻繁（便宜），全文抓取只對新文章做一次。
"""
from __future__ import annotations

import datetime as dt
import re
import logging
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser

from .base import ParsedDocument, register_adapter

logger = logging.getLogger(__name__)

# 這些查詢參數是追蹤用途，會讓同一篇文章產生不同 URL，
# 破壞 Document.url 的唯一約束所提供的冪等保證。
_TRACKING_PARAMS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "from", "ref", "share_source",
)


def normalize_url(url: str, base_url: str = "") -> str:
    """正規化 URL：補上網域、移除追蹤參數與 fragment。

    不做這一步的話，同一篇文章從不同管道取得會被視為不同文件，
    去重與續爬都會失效。
    """
    if not url:
        return ""
    if base_url:
        url = urljoin(base_url, url)

    parts = urlsplit(url)
    if parts.query:
        kept = [
            kv for kv in parts.query.split("&")
            if kv and kv.split("=", 1)[0].lower() not in _TRACKING_PARAMS
        ]
        query = "&".join(kept)
    else:
        query = ""

    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _to_datetime(entry) -> dt.datetime | None:
    """從 feedparser 的解析結果取出發布時間（aware datetime）。

    feedparser 對不合規的 RFC-822 日期會回傳 None 而非拋錯。實例：
    ETtoday 的 feed 送出 ``'Sat,29 Aug 2026 19:24:00  +0800'``——
    逗號後缺空格、時區前多一個空格，feedparser 直接放棄解析。
    若不補這層 fallback，該來源全部文件都會沒有發布時間，
    而時間是事件時間線的基礎。
    """
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            try:
                return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue

    for key in ("published", "updated"):
        raw = (getattr(entry, key, "") or "").strip()
        if raw:
            cleaned = _relax_rfc822(raw)
            if cleaned:
                return cleaned
    return None


# 逗號後補空格、壓縮連續空白——涵蓋實務上常見的不合規寫法
_RFC822_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",
    "%d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
)


def _relax_rfc822(raw: str) -> dt.datetime | None:
    cleaned = re.sub(r",(?=\S)", ", ", raw)      # 逗號後補空格
    cleaned = re.sub(r"\s+", " ", cleaned).strip()  # 壓縮連續空白
    for fmt in _RFC822_FORMATS:
        try:
            parsed = dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    logger.debug("無法解析日期字串：%r", raw)
    return None


@register_adapter("rss")
class RssAdapter:
    """通用 RSS/Atom 解析。多數新聞來源可直接沿用，不需自訂 adapter。"""

    def list_documents(self, raw: str, *, base_url: str = "") -> list[ParsedDocument]:
        feed = feedparser.parse(raw)

        # 判準是 version 而非 bozo。實測 feedparser 的行為：
        #
        #   正常 RSS      version='rss20'  bozo=False  entries=N
        #   HTML 維護頁   version=''       bozo=False  entries=0   ← 關鍵
        #   純文字        version=''       bozo=1      entries=0
        #
        # HTML 頁面不會觸發 bozo，只是解析出零個項目。若以 bozo 判斷，
        # 來源改回維護頁時會被當成「今天沒新聞」，consecutive_failures
        # 永遠不增加，ADR-0007 賴以偵測網站改版的健康度監測完全失明。
        # 這種失敗不會報錯，只會讓某個來源靜靜地停止供稿。
        version = feed.get("version") if hasattr(feed, "get") else None
        if not version:
            reason = feed.get("bozo_exception") if hasattr(feed, "get") else None
            raise ValueError(
                f"回應不是可辨識的 feed 格式（version={version!r}）"
                f"{f'：{reason}' if reason else ''}"
            )

        docs: list[ParsedDocument] = []
        skipped = 0
        for entry in feed.entries:
            url = normalize_url(getattr(entry, "link", ""), base_url)
            title = (getattr(entry, "title", "") or "").strip()
            if not url or not title:
                skipped += 1
                continue

            docs.append(ParsedDocument(
                url=url,
                title=title,
                # RSS 的 summary 多半只是摘要，全文另行抓取
                body="",
                published_at=_to_datetime(entry),
                author=(getattr(entry, "author", "") or "").strip()[:128],
                external_id=(getattr(entry, "id", "") or "")[:128],
                extra={"summary": (getattr(entry, "summary", "") or "").strip()},
            ))

        # 有項目卻沒有任何一筆可用——這是結構性問題，不是「今天沒新聞」。
        #
        # 實例：聯合新聞網的 RSS 會回傳 20 個結構完整但內容全空的 item
        # （title 為 <![CDATA[]]>、link 為空、pubDate 為 1970 epoch）。
        # 它通過了上方的 version 檢查，若這裡不擋，ingest_source 會記為
        # 成功且 consecutive_failures 歸零——一個永久壞掉的來源將永遠
        # 看起來健康。這與 HTML 維護頁是同一類失敗，只是更難察覺。
        if feed.entries and not docs:
            raise ValueError(
                f"feed 有 {len(feed.entries)} 個項目但無一可用"
                f"（皆缺標題或連結）——來源結構可能已變更"
            )
        if skipped:
            logger.warning("feed 中有 %d/%d 個項目缺標題或連結而被略過",
                           skipped, len(feed.entries))
        return docs
