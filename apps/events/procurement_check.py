"""對追蹤中的事件查詢政府採購網（任務 55）。

與 ``official_check``（司法院）同一個模式，理由也相同：官方源不是
「訂閱最新」，而是「拿著事件去問有沒有新進展」。差別只在查詢鍵——
司法院用案號精確比對，採購網用事件的核心識別詞（任務 19 校準過的
``build_event_query``）與相關廠商名。

**不做全量入庫。** 採購網有數百萬筆標案，與追蹤事件相關的是極少數；
把整個資料庫爬下來再過濾，成本與價值完全不成比例。
"""
from __future__ import annotations

import dataclasses
import logging

from django.utils import timezone

from apps.events.models import Event, EventStatus
from apps.events.terms import build_event_query
from apps.ingest.fetchers import Fetcher, HttpFetcher
from apps.ingest.models import ContentClass, Source, SourceType
from apps.ingest.procurement import (
    SEARCH_BY_COMPANY_NAME, SEARCH_BY_TITLE, hit_to_parsed_document, search,
)
from apps.ingest.services import upsert_parsed_documents

logger = logging.getLogger(__name__)

PROCUREMENT_SOURCE_SLUG = "pcc-tenders"

#: 只有看起來像公司的支援詞才拿去查廠商——人名查廠商一定是雜訊，
#: 而 build_event_query 的 support 混了被告姓名與公司名。
_COMPANY_SUFFIXES = ("公司", "營造", "建設", "工程", "企業", "事務所",
                     "實業", "科技", "開發", "工業", "商行", "行號")


def looks_like_company(term: str) -> bool:
    return any(term.endswith(suffix) for suffix in _COMPANY_SUFFIXES)


def get_or_create_source() -> Source:
    """採購網公告是**公文**，可全文公開（著作權法第 9 條）。"""
    source, _ = Source.objects.get_or_create(
        slug=PROCUREMENT_SOURCE_SLUG,
        defaults={
            "name": "政府電子採購網",
            "type": SourceType.PCC_DATASET,
            "base_url": "https://web.pcc.gov.tw",
            "content_class": ContentClass.PUBLIC_RECORD,
            # 查詢驅動，不走清單輪詢——這裡的間隔只是給 UI 顯示用
            "poll_interval_minutes": 1440,
            "enabled": True,
        },
    )
    return source


@dataclasses.dataclass
class ProcurementSummary:
    events_checked: int = 0
    queries: int = 0
    created: int = 0
    updated: int = 0
    hits: int = 0


def check_events(
    *, fetcher: Fetcher | None = None, dry_run: bool = False,
    only_slug: str = "",
) -> ProcurementSummary:
    """對每個追蹤中的事件查採購網並入庫命中的標案。"""
    summary = ProcurementSummary()
    events = Event.objects.filter(
        status__in=[EventStatus.ACTIVE, EventStatus.DORMANT, EventStatus.CLOSED])
    if only_slug:
        events = events.filter(slug=only_slug)
    events = list(events)
    if not events:
        return summary

    owns_fetcher = fetcher is None
    fetcher = fetcher or HttpFetcher()
    source = None if dry_run else get_or_create_source()
    now = timezone.now()

    try:
        for event in events:
            query = build_event_query(event)
            if not query.identity:
                continue

            # 標案名稱用核心識別詞；廠商名另外查，因為 searchbytitle
            # 只比對標案名稱，查公司名一定是 0 筆（實測「巨佳營造」
            # 走 title 為 0、走 companyname 為 252）。
            plans = [(query.identity, SEARCH_BY_TITLE)]
            plans += [(term, SEARCH_BY_COMPANY_NAME)
                      for term in query.support if looks_like_company(term)]

            parsed = []
            for term, endpoint in plans:
                summary.queries += 1
                for hit in search(term, fetcher=fetcher, endpoint=endpoint):
                    doc = hit_to_parsed_document(hit)
                    if doc is not None:
                        parsed.append(doc)

            summary.events_checked += 1
            summary.hits += len(parsed)
            if dry_run or not parsed:
                logger.info("採購網 %s：命中 %d 筆%s",
                            event.slug, len(parsed), "（dry-run）" if dry_run else "")
                continue

            created, updated, _ = upsert_parsed_documents(source, parsed, now)
            summary.created += created
            summary.updated += updated
            logger.info("採購網 %s：命中 %d、新增 %d、更新 %d",
                        event.slug, len(parsed), created, updated)
    finally:
        if owns_fetcher and hasattr(fetcher, "close"):
            fetcher.close()

    return summary
