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

歷史回補不寫入 ``Source.consecutive_failures``——那是即時輪詢的
健康度，兩者混在一起會讓「立即爬取」被歷史空頁毒掉。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlencode

from apps.core.dates import parse_datetime
from apps.ingest.adapters.base import ParsedDocument
from apps.ingest.adapters.rss import normalize_url

TAIPEI = dt.timezone(dt.timedelta(hours=8))

KIND_DATE = "date"
KIND_PAGE = "page"
KIND_OFFSET = "offset"

JSON_CNA = "cna"
JSON_LTN = "ltn"
JSON_TWREPORTER = "twreporter"


@dataclass(frozen=True)
class ArchiveSpec:
    """單一來源的歷史清單走法。"""

    slug: str
    kind: str
    note: str
    url_template: str
    earliest: dt.date | None = None
    adapter_slug: str = ""
    json_parser: str = ""
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
        url_template="https://www.ctee.com.tw/livenews/ctee?page={page}",
        earliest=None,
        adapter_slug="ctee", chunk_size=10, max_units=500,
        note="分類頁 ?page=N 純 HTTP；帶日期的路徑被 Cloudflare 擋",
    ),
    "pts": _spec(
        slug="pts", kind=KIND_PAGE,
        url_template="https://news.pts.org.tw/dailynews?page={page}",
        earliest=dt.date(2011, 8, 2),
        adapter_slug="pts", chunk_size=20, max_units=2000,
        note="dailynews?page=N；頁碼過高會繞回第 1 頁。文章 ID 1 為 2011-08-02",
    ),
    "cna-society": _spec(
        slug="cna-society", kind=KIND_PAGE,
        url_template="https://www.cna.com.tw/cna2018api/api/WNewsList",
        earliest=None, json_parser=JSON_CNA, method="POST",
        post_category="asoc", chunk_size=5, max_units=20,
        note="WNewsList POST 只給近約 100 則，沒有可用的日期參數",
    ),
    "cna-politics": _spec(
        slug="cna-politics", kind=KIND_PAGE,
        url_template="https://www.cna.com.tw/cna2018api/api/WNewsList",
        earliest=None, json_parser=JSON_CNA, method="POST",
        post_category="aipl", chunk_size=5, max_units=20,
        note="WNewsList POST 只給近約 100 則，沒有可用的日期參數",
    ),
    "cna-mainland": _spec(
        slug="cna-mainland", kind=KIND_PAGE,
        url_template="https://www.cna.com.tw/cna2018api/api/WNewsList",
        earliest=None, json_parser=JSON_CNA, method="POST",
        post_category="acn", chunk_size=5, max_units=20,
        note="WNewsList POST 只給近約 100 則，沒有可用的日期參數",
    ),
    "cna-finance": _spec(
        slug="cna-finance", kind=KIND_PAGE,
        url_template="https://www.cna.com.tw/cna2018api/api/WNewsList",
        earliest=None, json_parser=JSON_CNA, method="POST",
        post_category="aie", chunk_size=5, max_units=20,
        note="WNewsList POST 只給近約 100 則，沒有可用的日期參數",
    ),
    "ltn": _spec(
        slug="ltn", kind=KIND_PAGE,
        url_template="https://news.ltn.com.tw/ajax/breakingnews/all/{page}",
        earliest=None, json_parser=JSON_LTN,
        chunk_size=10, max_units=500,
        note="ajax JSON；第 2 頁起 data 為 dict。搜尋頁的日期參數不生效",
    ),
    "twreporter": _spec(
        slug="twreporter", kind=KIND_OFFSET,
        url_template="https://go-api.twreporter.org/v2/posts?offset={offset}&limit=50",
        earliest=dt.date(2015, 12, 14),
        json_parser=JSON_TWREPORTER, chunk_size=4, max_units=200,
        note="go-api /v2/posts 可走完整庫，實測 5922 篇、最早 2015-12-14",
    ),
    "mirrormedia": _spec(
        slug="mirrormedia", kind=KIND_PAGE,
        url_template="",
        earliest=None, chunk_size=1, max_units=0,
        note="站台為 SPA，/api/v2/posts 伺服器 1.5 秒逾時；目前只能靠 RSS 近況",
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


def build_request(spec: ArchiveSpec, cursor: Cursor) -> ArchiveRequest:
    if spec.kind == KIND_DATE:
        if cursor.date is None:
            raise ValueError("日期走訪缺少 cursor.date")
        url = spec.url_template.replace(
            "{date}", cursor.date.strftime(spec.date_format))
        return ArchiveRequest(url=url)
    if spec.kind == KIND_OFFSET:
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


def advance_cursor(spec: ArchiveSpec, cursor: Cursor) -> Cursor:
    nxt = Cursor(
        date=cursor.date, page=cursor.page, offset=cursor.offset,
        first_fp=cursor.first_fp, units_done=cursor.units_done + 1,
    )
    if spec.kind == KIND_DATE and nxt.date is not None:
        nxt.date = nxt.date + dt.timedelta(days=1)
    elif spec.kind == KIND_OFFSET:
        nxt.offset += 50
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
    if spec.json_parser:
        parser = _JSON_PARSERS[spec.json_parser]
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


_JSON_PARSERS = {
    JSON_CNA: parse_cna_json,
    JSON_LTN: parse_ltn_json,
    JSON_TWREPORTER: parse_twreporter_json,
}


def tagged_post_url(url: str, body: dict) -> str:
    """測試用：把 POST body 編進 URL，讓 FakeFetcher 能分頁登記。"""
    return url + "?" + urlencode({k: body[k] for k in sorted(body)})
