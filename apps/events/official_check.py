"""對追蹤中事件的已知案號，定期檢查司法院月封存檔有無新進展（任務 27+33）。

任務 27 已經有「這個事件該不該檢查官方源」的判斷邏輯
（``Event.due_for_official_check``），任務 33 已經有「怎麼從月封存檔
撈出一筆案件」的機制（``apps.ingest.judicial_opendata``）。本模組是
把兩者接起來的那一層，此前一直缺這一層——`due_for_official_check`
判斷完「該檢查了」之後沒有東西真的去檢查。

## 每個封存檔只下載一次，不是每個事件下載一次

若對每個到期事件各自下載月封存檔，N 個事件、M 個月份就要下載 N×M
次 200MB+ 的檔案。改成先收集所有到期事件的案號，每個月封存檔只下載
一次，用其比對全部待查案號——這不是效能優化的錦上添花，是避免
把同一份 270MB 檔案下載幾十次的必要設計。

## session 過期時整批中止，不是略過繼續

若 session 在檢查到一半過期，讓例外往上拋而非吞掉繼續下一個事件——
吞掉的後果是「其餘事件全部檢查失敗，卻沒有任何記錄顯示為什麼」，
且 ``record_official_check`` 若仍照跑，會讓這些事件被記為「已檢查」
而其實根本沒有真的查到資料。
"""
from __future__ import annotations

import dataclasses
import logging

from django.utils import timezone

from apps.core.identifiers import parse_case_number
from apps.events.models import Event, EventStatus
from apps.ingest.judicial import JudgmentIngestResult, ingest_parsed_judgment
from apps.ingest.judicial_opendata import (
    SessionExpired, case_json_to_parsed_document, download_archive,
    find_case_in_archive, list_monthly_archives,
)
from apps.ingest.models import ContentClass, ExternalSession, Source, SourceType

logger = logging.getLogger(__name__)

__all__ = ["check_due_events", "get_or_create_judicial_source"]

JUDICIAL_OPENDATA_SESSION_SLUG = "judicial-opendata"


def get_or_create_judicial_source() -> Source:
    return Source.objects.get_or_create(
        slug="judicial-opendata-archive",
        defaults={
            "name": "司法院資料開放平台（月封存檔）",
            "type": SourceType.JUDICIAL_API,
            "base_url": "https://opendata.judicial.gov.tw",
            "content_class": ContentClass.PUBLIC_RECORD,
        },
    )[0]


@dataclasses.dataclass
class OfficialCheckSummary:
    events_checked: int = 0
    archives_downloaded: int = 0
    results: list[JudgmentIngestResult] = dataclasses.field(default_factory=list)
    session_expired: bool = False


def check_due_events(*, months_back: int = 2, dry_run: bool = False) -> OfficialCheckSummary:
    """對所有到期的追蹤中事件，依已知案號查最近幾個月的封存檔。

    Returns:
        ``OfficialCheckSummary``。``session_expired`` 為 True 時，
        呼叫端應提示使用者重新登入（見 ``/crawlers/`` 的登入卡片），
        而不是把這次的空結果當成「這批事件都沒有新進展」。
    """
    summary = OfficialCheckSummary()

    due_events = [
        e for e in Event.objects.filter(
            status__in=[EventStatus.ACTIVE, EventStatus.DORMANT, EventStatus.CLOSED])
        if e.case_numbers and e.due_for_official_check()
    ]
    if not due_events:
        return summary

    session = ExternalSession.objects.filter(slug=JUDICIAL_OPENDATA_SESSION_SLUG).first()
    if session is None or not session.is_set:
        logger.warning("司法院資料開放平台尚未設定登入 session，無法檢查")
        summary.session_expired = True
        return summary

    # case_number 字串 → 該案號所屬的事件。多個事件理論上不該共用同一
    # 案號（那代表歸屬有誤），但用 dict 而非直接假設一對一，
    # 遇到時至少不會互相覆蓋成錯誤結果。
    pending: dict[str, Event] = {}
    for event in due_events:
        for case_number_str in event.case_numbers:
            pending.setdefault(case_number_str, event)

    archives = list_monthly_archives(limit=months_back)
    now = timezone.now()

    try:
        for archive in archives:
            if not pending:
                break
            archive_bytes = download_archive(archive, session=session)
            summary.archives_downloaded += 1

            for case_number_str in list(pending.keys()):
                parsed = parse_case_number(case_number_str)
                if parsed is None:
                    pending.pop(case_number_str)
                    continue
                case_json = find_case_in_archive(
                    archive_bytes, roc_year=parsed.year,
                    category=parsed.category, number=parsed.number)
                if case_json is None:
                    continue

                event = pending.pop(case_number_str)
                parsed_doc = case_json_to_parsed_document(case_json)
                if parsed_doc is None:
                    # 依法不公開的案件類型（任務 35）——不入庫，
                    # 但仍算「查過了」，不該讓它一直被視為待查
                    continue
                if dry_run:
                    logger.info("〔dry-run〕事件 %s 案號 %s 命中，未實際入庫",
                               event.slug, case_number_str)
                    continue

                source = get_or_create_judicial_source()
                result = ingest_parsed_judgment(
                    parsed_doc, source=source, candidate_events=[event])
                summary.results.append(result)

    except SessionExpired:
        # **不執行 record_official_check。** session 過期代表這批事件
        # 根本沒有被真的檢查過，若仍記錄檢查時間，這些事件會被系統
        # 誤判為「最近查過、還沒到期」，實際上一次有效的檢查都沒發生。
        logger.warning("司法院資料開放平台 session 已過期，本次檢查中止，"
                       "請至 /crawlers/ 重新登入")
        summary.session_expired = True
        return summary

    if not dry_run:
        for event in due_events:
            event.record_official_check(now, save=True)
    summary.events_checked = len(due_events)

    return summary
