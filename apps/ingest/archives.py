"""各媒體歷史清單的走訪方式。

即時輪詢走 RSS 或當日清單頁，兩者都只有近況（實測 RSS 約 20–50
則、中時即時分頁約 10 頁）。從古至今必須改走站台自己的日期檔、
分頁或 JSON API。

以下規格是 2026-08-31 對各站實測的結果，不是猜測：

- 聯合新聞網 ``/news/archive?date=YYYYMMDD`` 純 HTTP 可取，
  2016-07-01 起每日約 20 則；更早的日期只有當日推薦、沒有當日新聞。
- ETtoday ``/news/news-list-YYYY-MM-DD-0.htm`` 純 HTTP，
  2012-01-01 起有當日清單（2011 以前頁面在、文章不在）。
- 報導者 ``go-api.twreporter.org/v2/posts`` 可 offset 走完整庫，
  共 5922 篇，最早 2015-12-14。
- 公視 ``/dailynews?page=N`` 可翻頁；頁碼過高會繞回第 1 頁，
  必須比對首頁指紋才能停。文章 ID 1 存在，標題日期 2011-08-02。
- 中央社 ``WNewsList`` POST 只給近約 100 則（約 5 天），沒有日期參數。
- 自由時報 ``/ajax/breakingnews/all/{page}`` JSON；第 2 頁起
  ``data`` 是 dict 不是 list。
- 中時即時 ``?page=N`` 純 HTTP 約 10 頁後變空；日期 URL 404。
- 工商 ``/livenews/ctee?page=N`` 純 HTTP；``/livenews/ctee/日期``
  被 Cloudflare 擋。
- 鏡週刊為 SPA，``/api/v2/posts`` 伺服器端 1.5 秒就逾時。

2026-09-01 逐站量測回補深度後的修正（原註記過於樂觀）：

- **這些端點多半是滾動視窗，不是歷史檔**。中央社硬上限約 100 則
  （約 5 天）、自由第 26–29 頁即見底（約 1.5 天）、公視約
  100–200 頁見底（約 1 個月）。走完不等於拿到該站全部歷史。
- **工商的 ``?page=N`` 根本不生效**：page=1/50/200/400 回傳完全
  相同的 35 篇。分頁是裝飾用的，實際只有一頁。改走
  ``sitemaps/sitemap_newstoday.xml``（1000 篇、含標題與時間、
  純 HTTP）。**更早的內容站台不給**：「載入更多」打的
  ``/api/category/{分類}/{頁}`` 與 robots.txt 列出的 104 個歷史
  子 sitemap 都被 Cloudflare 擋成 403；該 API 即使在真實瀏覽器裡
  也只容許同一 session 前 1–3 次呼叫，之後一律 403。
- **公視的 2011-08-02 是最早的文章 ID，不是清單能翻到的最早**，
  當成 ``earliest`` 會在 UI 顯示一個實際到不了的日期。
- **鏡週刊可以爬，而且能挖到底**：站台的無限捲動實際上是打
  ``adam-weekly-api-server-prod-…run.app/content/graphql``
  （Keystone GraphQL，前端捲動時每個訪客的瀏覽器都打這個），
  ``take``／``skip`` 可一路下探——實測 343,149 篇、最舊
  2016-09-29（創站）。**不需要模擬瀏覽器捲動**。
  端點與查詢是從 ``/_next/static/chunks/pages/section/[slug]*.js``
  讀出來的；sitemap（1500 筆／10 天）與 RSS（20 餘筆）相形之下
  都只是近況，已不再使用。

歷史回補不寫入 ``Source.consecutive_failures``——那是即時輪詢的
健康度，兩者混在一起會讓「立即爬取」被歷史空頁毒掉。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from apps.core.dates import parse_datetime
from apps.ingest.adapters.base import ParsedDocument
from apps.ingest.adapters.rss import normalize_url
from apps.ingest.fetchers import FetchError, Fetcher

TAIPEI = dt.timezone(dt.timedelta(hours=8))

KIND_DATE = "date"
KIND_PAGE = "page"
KIND_OFFSET = "offset"

PARSER_CNA = "cna"
PARSER_LTN = "ltn"
PARSER_TWREPORTER = "twreporter"
PARSER_SITEMAP = "sitemap"
PARSER_MIRRORMEDIA = "mirrormedia"

#: 鏡週刊前端無限捲動實際打的查詢（Keystone GraphQL）。
#: ``skip`` 可以一路下探——實測 skip=343140 仍取得 2016-09-29 的文章。
MIRRORMEDIA_GQL = (
    "query($take:Int,$skip:Int!,$where:PostWhereInput!){"
    "posts(take:$take,skip:$skip,where:$where,"
    "orderBy:[{publishedDate:desc}])"
    "{id slug title publishedDate}}"
)


@dataclass(frozen=True)
class ArchiveSpec:
    """單一來源的歷史清單走法。"""

    slug: str
    kind: str
    note: str
    url_template: str
    earliest: dt.date | None = None
    adapter_slug: str = ""
    list_parser: str = ""
    #: GraphQL 查詢字串。設了就以 ``{take, skip}`` 組 POST body
    #: （見 ``build_request``），``url_template`` 不帶任何佔位符。
    graphql_query: str = ""
    method: str = "GET"
    date_format: str = "%Y%m%d"
    chunk_size: int = 30
    max_units: int = 8000
    pagesize: int = 20
    post_category: str = ""
    #: 空頁或繞回首頁視為終點。日期走訪則走到今天。
    stop_on_empty: bool = True


@dataclass
class Cursor:
    date: dt.date | None = None
    page: int = 1
    offset: int = 0
    first_fp: str = ""
    units_done: int = 0

    def to_json(self) -> str:
        payload = {
            "date": self.date.isoformat() if self.date else "",
            "page": self.page,
            "offset": self.offset,
            "first_fp": self.first_fp,
            "units_done": self.units_done,
        }
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Cursor":
        if not raw or not raw.strip():
            return cls()
        if raw[0] != "{":
            # 舊格式：純日期或純數字
            if "-" in raw:
                return cls(date=dt.date.fromisoformat(raw))
            if raw.isdigit():
                return cls(page=int(raw), offset=int(raw))
            return cls()
        data = json.loads(raw)
        date_raw = data.get("date") or ""
        return cls(
            date=dt.date.fromisoformat(date_raw) if date_raw else None,
            page=int(data.get("page") or 1),
            offset=int(data.get("offset") or 0),
            first_fp=data.get("first_fp") or "",
            units_done=int(data.get("units_done") or 0),
        )


@dataclass
class ArchiveRequest:
    url: str
    body: dict | None = None


def _spec(**kwargs) -> ArchiveSpec:
    return ArchiveSpec(**kwargs)


# 實測日期寫在 earliest，註記寫在 note。新增來源加一筆即可。
ARCHIVE_SPECS: dict[str, ArchiveSpec] = {
    "udn": _spec(
        slug="udn", kind=KIND_DATE,
        url_template="https://udn.com/news/archive?date={date}",
        earliest=dt.date(2016, 7, 1),
        adapter_slug="udn", date_format="%Y%m%d", chunk_size=30,
        note="日檔 /news/archive?date=YYYYMMDD，2016-07-01 起每日約 20 則",
    ),
    "ettoday": _spec(
        slug="ettoday", kind=KIND_DATE,
        url_template="https://www.ettoday.net/news/news-list-{date}-0.htm",
        earliest=dt.date(2012, 1, 1),
        adapter_slug="ettoday", date_format="%Y-%m-%d", chunk_size=20,
        note="日清單 news-list-YYYY-MM-DD-0.htm，2012-01-01 起有當日新聞",
    ),
    "chinatimes": _spec(
        slug="chinatimes", kind=KIND_PAGE,
        url_template="https://www.chinatimes.com/realtimenews/?page={page}&chdtv",
        earliest=None,
        adapter_slug="chinatimes", chunk_size=5, max_units=40,
        note="即時分頁純 HTTP 約 10 頁後變空；日期 URL 404。更早內容需記者頁",
    ),
    "ctee": _spec(
        slug="ctee", kind=KIND_PAGE,
        url_template="https://www.ctee.com.tw/sitemaps/sitemap_newstoday.xml",
        earliest=None, list_parser=PARSER_SITEMAP,
        chunk_size=1, max_units=1,
        note="Google News sitemap 1000 篇（含標題與發布時間），約近 2 天，"
             "純 HTTP 可取；清單頁只有 35 篇且 ?page=N 不生效。"
             "更早的內容站台不給：104 個歷史子 sitemap 與「載入更多」的"
             "/api/category/ 都被 Cloudflare 擋（403）",
    ),
    "pts": _spec(
        slug="pts", kind=KIND_PAGE,
        url_template="https://news.pts.org.tw/dailynews?page={page}",
        # 2011-08-02 是站台最早的「文章」（ID 1），不是清單能翻到的
        # 最早——實測 dailynews 約 100–200 頁就變空（約 2000–3000 則、
        # 回到 2026-07 左右）。earliest 設 None 才不會在 UI 顯示一個
        # 實際上到不了的日期。
        earliest=None,
        adapter_slug="pts", chunk_size=20, max_units=200,
        note="dailynews?page=N 實測約 100–200 頁見底（約 2 個月）；"
             "頁碼過高會繞回第 1 頁。更早的文章 ID 仍在，但清單翻不到",
    ),
    "cna-society": _spec(
        slug="cna-society", kind=KIND_PAGE,
        url_template="https://www.cna.com.tw/cna2018api/api/WNewsList",
        earliest=None, list_parser=PARSER_CNA, method="POST",
        post_category="asoc", chunk_size=5, max_units=20,
        note="WNewsList POST 只給近約 100 則，沒有可用的日期參數",
    ),
    "cna-politics": _spec(
        slug="cna-politics", kind=KIND_PAGE,
        url_template="https://www.cna.com.tw/cna2018api/api/WNewsList",
        earliest=None, list_parser=PARSER_CNA, method="POST",
        post_category="aipl", chunk_size=5, max_units=20,
        note="WNewsList POST 只給近約 100 則，沒有可用的日期參數",
    ),
    "cna-mainland": _spec(
        slug="cna-mainland", kind=KIND_PAGE,
        url_template="https://www.cna.com.tw/cna2018api/api/WNewsList",
        earliest=None, list_parser=PARSER_CNA, method="POST",
        post_category="acn", chunk_size=5, max_units=20,
        note="WNewsList POST 只給近約 100 則，沒有可用的日期參數",
    ),
    "cna-finance": _spec(
        slug="cna-finance", kind=KIND_PAGE,
        url_template="https://www.cna.com.tw/cna2018api/api/WNewsList",
        earliest=None, list_parser=PARSER_CNA, method="POST",
        post_category="aie", chunk_size=5, max_units=20,
        note="WNewsList POST 只給近約 100 則，沒有可用的日期參數",
    ),
    "ltn": _spec(
        slug="ltn", kind=KIND_PAGE,
        url_template="https://news.ltn.com.tw/ajax/breakingnews/all/{page}",
        earliest=None, list_parser=PARSER_LTN,
        chunk_size=10, max_units=500,
        note="ajax JSON；第 2 頁起 data 為 dict。搜尋頁的日期參數不生效",
    ),
    "twreporter": _spec(
        slug="twreporter", kind=KIND_OFFSET,
        url_template="https://go-api.twreporter.org/v2/posts?offset={offset}&limit=50",
        earliest=dt.date(2015, 12, 14),
        list_parser=PARSER_TWREPORTER, chunk_size=4, max_units=200,
        # 必須與網址裡的 limit=50 一致：advance_cursor 用 pagesize 當步長
        pagesize=50,
        note="go-api /v2/posts 可走完整庫，實測 5922 篇、最早 2015-12-14",
    ),
    # 監察院四類公文（任務 57）。分頁是真的，但頁碼過大會夾到最後
    # 一頁並重複其內容——終止條件是「與前一頁相同」，不是「與第一頁
    # 相同」（後者是公視那種繞回首頁的樣式）。
    "cy-investigation": _spec(
        slug="cy-investigation", kind=KIND_PAGE,
        url_template="https://www.cy.gov.tw/CyBsBox.aspx?CSN=1&n=133&sms=0&page={page}",
        adapter_slug="control-yuan", chunk_size=10, max_units=600,
        note="調查報告。每頁 20 筆，清單即含案號與案由；正文為 DOCX／PDF 附件",
    ),
    "cy-correction": _spec(
        slug="cy-correction", kind=KIND_PAGE,
        url_template="https://www.cy.gov.tw/CyBsBox.aspx?CSN=2&n=134&sms=0&page={page}",
        adapter_slug="control-yuan", chunk_size=10, max_units=600,
        note="糾正案文。每頁 20 筆",
    ),
    "cy-censure": _spec(
        slug="cy-censure", kind=KIND_PAGE,
        url_template="https://www.cy.gov.tw/CyBsBox.aspx?CSN=3&n=136&sms=0&page={page}",
        adapter_slug="control-yuan", chunk_size=10, max_units=600,
        note="糾舉案文。每頁 20 筆",
    ),
    "cy-impeachment": _spec(
        slug="cy-impeachment", kind=KIND_PAGE,
        url_template="https://www.cy.gov.tw/CyBsBox.aspx?CSN=4&n=135&sms=0&page={page}",
        adapter_slug="control-yuan", chunk_size=10, max_units=600,
        note="彈劾案文。每頁 20 筆，實測 27 頁見底",
    ),
    "mirrormedia": _spec(
        slug="mirrormedia", kind=KIND_OFFSET,
        url_template=(
            "https://adam-weekly-api-server-prod-ufaummkd5q-de.a.run.app"
            "/content/graphql"
        ),
        earliest=dt.date(2016, 9, 29),
        list_parser=PARSER_MIRRORMEDIA, graphql_query=MIRRORMEDIA_GQL,
        method="POST", pagesize=100, chunk_size=20, max_units=4000,
        note="無限捲動的 GraphQL（skip 可深挖）：實測 343,149 篇、"
             "最舊 2016-09-29。這是前端捲動時實際打的端點，"
             "不需要模擬瀏覽器",
    ),
}


def get_archive_spec(slug: str) -> ArchiveSpec | None:
    return ARCHIVE_SPECS.get(slug)


def fingerprint_urls(urls: list[str]) -> str:
    blob = "\n".join(sorted(urls)).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def initial_cursor(spec: ArchiveSpec) -> Cursor:
    if spec.kind == KIND_DATE:
        return Cursor(date=spec.earliest or dt.date(2010, 1, 1))
    if spec.kind == KIND_OFFSET:
        return Cursor(offset=0)
    return Cursor(page=1)


def live_cursor(spec: ArchiveSpec, today: dt.date) -> Cursor:
    """即時輪詢要的是「最新」，不是「最舊」。

    ``initial_cursor`` 給歷史回補的起點（日期走訪從 ``earliest``
    開始），即時輪詢反過來要當天／第一頁／offset 0——同一份
    ``ArchiveSpec`` 兩種呼叫端各自要不同的起點，不能共用。
    """
    if spec.kind == KIND_DATE:
        return Cursor(date=today)
    if spec.kind == KIND_OFFSET:
        return Cursor(offset=0)
    return Cursor(page=1)


def build_request(spec: ArchiveSpec, cursor: Cursor) -> ArchiveRequest:
    if spec.kind == KIND_DATE:
        if cursor.date is None:
            raise ValueError("日期走訪缺少 cursor.date")
        url = spec.url_template.replace(
            "{date}", cursor.date.strftime(spec.date_format))
        return ArchiveRequest(url=url)
    if spec.kind == KIND_OFFSET:
        if spec.graphql_query:
            return ArchiveRequest(url=spec.url_template, body={
                "query": spec.graphql_query,
                "variables": {
                    "take": spec.pagesize,
                    "skip": cursor.offset,
                    "where": {"state": {"equals": "published"}},
                },
            })
        url = spec.url_template.replace("{offset}", str(cursor.offset))
        return ArchiveRequest(url=url)
    url = spec.url_template.replace("{page}", str(cursor.page))
    if spec.method == "POST":
        body = {
            "action": "0",
            "category": spec.post_category,
            "pageidx": cursor.page,
            "pagesize": spec.pagesize,
        }
        return ArchiveRequest(url=url, body=body)
    return ArchiveRequest(url=url)


def fetch_archive_page(fetcher: Fetcher, spec: ArchiveSpec, request: ArchiveRequest) -> str:
    """依規格的 method 送出請求。歷史回補與即時輪詢共用同一份，
    避免 GET/POST 分支各寫一份後彼此漂移。"""
    if spec.method == "POST" and hasattr(fetcher, "post"):
        response = fetcher.post(request.url, json=request.body)
    else:
        response = fetcher.get(request.url)
    if not response.ok:
        raise FetchError(f"HTTP {response.status_code}")
    return response.text


def advance_cursor(spec: ArchiveSpec, cursor: Cursor) -> Cursor:
    nxt = Cursor(
        date=cursor.date, page=cursor.page, offset=cursor.offset,
        first_fp=cursor.first_fp, units_done=cursor.units_done + 1,
    )
    if spec.kind == KIND_DATE and nxt.date is not None:
        nxt.date = nxt.date + dt.timedelta(days=1)
    elif spec.kind == KIND_OFFSET:
        # 步長必須等於單次取回的筆數，否則會重複抓（步長太小）
        # 或整段跳過（步長太大）。原本寫死 50，只對報導者正確。
        nxt.offset += spec.pagesize
    else:
        nxt.page += 1
    return nxt


def date_at_noon(day: dt.date) -> dt.datetime:
    """清單沒給時間時，用當日 12:00 +08，避免被排到午夜變成「最早」。"""
    return dt.datetime(day.year, day.month, day.day, 12, 0, tzinfo=TAIPEI)


def apply_archive_date(
    docs: list[ParsedDocument], spec: ArchiveSpec, cursor: Cursor,
) -> list[ParsedDocument]:
    if spec.kind != KIND_DATE or cursor.date is None:
        return docs
    fallback = date_at_noon(cursor.date)
    for doc in docs:
        if doc.published_at is None:
            doc.published_at = fallback
    return docs


def parse_archive_documents(
    spec: ArchiveSpec, raw: str, *, base_url: str,
) -> list[ParsedDocument]:
    if spec.list_parser:
        parser = _LIST_PARSERS[spec.list_parser]
        return parser(raw, base_url=base_url)
    if not spec.adapter_slug:
        return []
    from apps.ingest.adapters import get_adapter
    try:
        return get_adapter(spec.adapter_slug).list_documents(
            raw, base_url=base_url)
    except ValueError as exc:
        # 歷史空頁是終點，不是網站改版。即時輪詢仍應把空頁當失敗。
        message = str(exc)
        if "未解析出任何文章" in message or "空的" in message:
            return []
        raise


def parse_cna_json(raw: str, *, base_url: str = "") -> list[ParsedDocument]:
    data = json.loads(raw)
    payload = data.get("ResultData") or {}
    items = payload.get("Items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return []
    docs: list[ParsedDocument] = []
    for item in items:
        url = item.get("PageUrl") or ""
        title = (item.get("HeadLine") or "").strip()
        if not url or not title:
            continue
        docs.append(ParsedDocument(
            url=normalize_url(url, base_url),
            title=title,
            published_at=_parse_slash_datetime(item.get("CreateTime")),
            external_id=str(item.get("Id") or "")[:128],
        ))
    return docs


def parse_ltn_json(raw: str, *, base_url: str = "") -> list[ParsedDocument]:
    data = json.loads(raw.lstrip("\ufeff"))
    payload = data.get("data")
    if isinstance(payload, dict):
        items = [v for v in payload.values() if isinstance(v, dict)]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    docs: list[ParsedDocument] = []
    for item in items:
        url = item.get("url") or ""
        title = (item.get("title") or "").strip()
        if not url or not title:
            continue
        docs.append(ParsedDocument(
            url=normalize_url(url, base_url),
            title=title,
            published_at=_parse_slash_datetime(item.get("time")),
            external_id=str(item.get("no") or "")[:128],
        ))
    return docs


def parse_twreporter_json(raw: str, *, base_url: str = "") -> list[ParsedDocument]:
    data = json.loads(raw)
    records = ((data.get("data") or {}).get("records")) or []
    docs: list[ParsedDocument] = []
    for item in records:
        slug = item.get("slug") or ""
        title = (item.get("title") or "").strip()
        if not slug or not title:
            continue
        url = f"https://www.twreporter.org/a/{slug}"
        docs.append(ParsedDocument(
            url=url,
            title=title,
            published_at=parse_datetime(item.get("published_date")),
            external_id=str(item.get("id") or "")[:128],
        ))
    return docs


def parse_mirrormedia_graphql(raw: str, *, base_url: str = "") -> list[ParsedDocument]:
    """鏡週刊 GraphQL 的 ``posts``。文章網址由 slug 組出。"""
    data = json.loads(raw)
    posts = ((data.get("data") or {}).get("posts")) or []
    docs: list[ParsedDocument] = []
    for item in posts:
        slug = (item.get("slug") or "").strip()
        title = (item.get("title") or "").strip()
        if not slug:
            continue
        docs.append(ParsedDocument(
            url=f"https://www.mirrormedia.mg/story/{slug}",
            title=title,
            published_at=parse_datetime(item.get("publishedDate")),
            external_id=str(item.get("id") or "")[:128],
        ))
    return docs


def parse_sitemap_xml(raw: str, *, base_url: str = "") -> list[ParsedDocument]:
    """sitemap.xml 的 ``<url><loc>`` 清單。

    給沒有可用清單頁或 API 的站台（鏡週刊是 SPA，其 ``/api/v2/posts``
    實測伺服器端就逾時）。sitemap 是站台自己公開給搜尋引擎的檔案，
    純 HTTP 可取、不需要渲染。

    只有網址沒有標題——``<loc>`` 就是全部。標題與內文由既有的內頁
    補齊流程（``fill_missing_bodies``）處理，這裡不猜。
    """
    docs: list[ParsedDocument] = []
    for block in re.findall(r"<url>(.*?)</url>", raw, re.DOTALL):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.DOTALL)
        if not loc:
            continue
        url = normalize_url(loc.group(1).strip(), base_url)
        if not url:
            continue
        # Google News sitemap（工商）另有 news:title 與 news:publication_date，
        # 比一般 sitemap 的 lastmod 精確，且直接給了標題。
        title = _first_tag(block, "news:title")
        when = (_first_tag(block, "news:publication_date")
                or _first_tag(block, "lastmod"))
        docs.append(ParsedDocument(
            url=url,
            # 沒有 news:title 時留白，交給內頁補齊——硬塞網址片段會污染
            # 轉載歸併與檢索（標題是 bigram 指紋的一部分）。
            title=title,
            published_at=parse_datetime(when) if when else None,
        ))
    return docs


def _first_tag(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", block, re.DOTALL)
    return match.group(1).strip() if match else ""


def _parse_slash_datetime(raw) -> dt.datetime | None:
    """中央社 ``2026/08/30 22:09``、自由時報 ``2026/08/30 23:58``。

    只有時分（``09:39``）沒有日期，無法確定是哪一天，回傳 None
    交給後續內頁補齊——亂填「今天」會在跨日重跑時把舊文時間改掉。
    """
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=TAIPEI)
        except ValueError:
            continue
    return parse_datetime(text)


_LIST_PARSERS = {
    PARSER_CNA: parse_cna_json,
    PARSER_LTN: parse_ltn_json,
    PARSER_TWREPORTER: parse_twreporter_json,
    PARSER_SITEMAP: parse_sitemap_xml,
    PARSER_MIRRORMEDIA: parse_mirrormedia_graphql,
}


def tagged_post_url(url: str, body: dict) -> str:
    """測試用：把 POST body 編進 URL，讓 FakeFetcher 能分頁登記。"""
    return url + "?" + urlencode({k: body[k] for k in sorted(body)})
