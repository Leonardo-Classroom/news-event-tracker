"""政府電子採購網（任務 55）。

**採集模式與新聞來源根本不同：查詢驅動，不是全量入庫。**
採購網有數百萬筆標案，其中與追蹤事件相關的是極少數。把整個資料庫
爬下來再過濾，成本與價值完全不成比例。因此照司法院官方源檢查
（``apps.events.official_check``）的模式：**只對追蹤中的事件查詢**，
用事件已校準過的查詢詞（任務 19 的 ``build_event_query``）。

2026-09-02 以真實事件實測命中數：

    南方澳大橋 57　京華城 35　大巨蛋 31　新竹棒球場 8
    誠新綠能 0　超思 0        （這兩案的標案不在標題裡）

資料來源是 g0v 的採購網 API（原 ``pcc.g0v.ronny.tw``，已 301 轉到
``pcc-api.openfun.app``；任務清單裡「民間 API 已擋機器人」的註記
是舊資訊，新網域實測可用）。三個端點：

    /api/searchbytitle?query=      標案名稱
    /api/searchbycompanyname?query= 廠商名稱
    /api/searchbycompanyid?query=   廠商統編

**每筆結果都帶統編**，可直接餵任務 15 的識別碼精確比對，歸屬不需
要 LLM。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from urllib.parse import quote

from apps.ingest.adapters.base import ParsedDocument
from apps.ingest.fetchers import FetchError, Fetcher

logger = logging.getLogger(__name__)

API_BASE = "https://pcc-api.openfun.app/api"
SITE_BASE = "https://pcc.g0v.ronny.tw"

SEARCH_BY_TITLE = "searchbytitle"
SEARCH_BY_COMPANY_NAME = "searchbycompanyname"
SEARCH_BY_COMPANY_ID = "searchbycompanyid"

TAIPEI = dt.timezone(dt.timedelta(hours=8))

#: 單一事件單次檢查最多取幾頁。標案數多的廠商（實測巨佳營造 252 筆
#: ／3 頁）不需要一次全拉——這是持續追蹤，不是一次性普查。
MAX_PAGES = 3


@dataclasses.dataclass(frozen=True)
class TenderHit:
    """一筆標案公告。"""

    date: dt.date | None
    title: str
    tender_type: str          # 決標公告／招標公告…
    unit_name: str            # 招標機關
    job_number: str
    company_ids: tuple[str, ...]
    company_names: tuple[str, ...]
    url: str

    @property
    def summary(self) -> str:
        parts = [self.tender_type, self.unit_name, self.title]
        if self.company_names:
            parts.append("得標／投標：" + "、".join(self.company_names))
        return " ｜ ".join(p for p in parts if p)


def _parse_date(value) -> dt.date | None:
    """API 的日期是 20240925 這種整數。"""
    text = str(value or "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return dt.date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def parse_records(payload: dict) -> list[TenderHit]:
    hits: list[TenderHit] = []
    for record in payload.get("records") or []:
        brief = record.get("brief") or {}
        companies = brief.get("companies") or {}
        hits.append(TenderHit(
            date=_parse_date(record.get("date")),
            title=(brief.get("title") or "").strip(),
            tender_type=(brief.get("type") or "").strip(),
            unit_name=(record.get("unit_name") or "").strip(),
            job_number=str(record.get("job_number") or ""),
            company_ids=tuple(companies.get("ids") or []),
            company_names=tuple(companies.get("names") or []),
            url=SITE_BASE + (record.get("url") or ""),
        ))
    return hits


def search(
    query: str, *, fetcher: Fetcher, endpoint: str = SEARCH_BY_TITLE,
    max_pages: int = MAX_PAGES,
) -> list[TenderHit]:
    """查詢採購網。失敗回空清單而非拋錯——單一查詢失敗不該中斷
    整輪事件檢查（同 ``official_check`` 的處理原則）。"""
    hits: list[TenderHit] = []
    for page in range(1, max_pages + 1):
        url = f"{API_BASE}/{endpoint}?query={quote(query)}&page={page}"
        try:
            response = fetcher.get(url)
            if not response.ok:
                raise FetchError(f"HTTP {response.status_code}")
            import json

            payload = json.loads(response.text)
        except (FetchError, ValueError) as exc:
            logger.warning("採購網查詢失敗 %s（%s）：%s", query, endpoint, exc)
            break

        hits.extend(parse_records(payload))
        if page >= int(payload.get("total_pages") or 1):
            break
    return hits


def hit_to_parsed_document(hit: TenderHit) -> ParsedDocument | None:
    """轉成可入庫的文件。

    標題含統編：``Document.save()`` 會從 title + raw_body 抽識別碼
    （``extract_tax_ids``），把統編放進標題就能讓精確比對直接生效，
    不必等內文補齊——採購網公告沒有「內文頁」可補。
    """
    if not hit.url or not hit.title:
        return None
    identifiers = " ".join(hit.company_ids)
    title = f"{hit.tender_type}｜{hit.title}"
    if identifiers:
        title = f"{title}（{identifiers}）"
    published_at = (
        dt.datetime(hit.date.year, hit.date.month, hit.date.day, 12, 0,
                    tzinfo=TAIPEI)
        if hit.date else None
    )
    return ParsedDocument(
        url=hit.url,
        title=title[:512],
        # 公告本身就是全部資訊，沒有另外的內文頁可抓——直接把摘要
        # 當內文，否則這些文件會永遠停在「缺內文」並被反覆重試。
        body=hit.summary,
        published_at=published_at,
        external_id=hit.job_number[:128],
    )
