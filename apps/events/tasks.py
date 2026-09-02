"""事件層的 Celery 任務（任務 27+33）。"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

JUDICIAL_SEARCH_SLUG = "judicial-search"


def run_official_check(*, force: bool = False) -> dict:
    """執行官方源檢查，並把結果寫回爬蟲頁上的司法來源列。

    不寫的話，點「立即爬取」之後 ``last_success_at`` 永遠是空的，
    UI 會一直顯示「從未成功」，像是根本沒開始。
    """
    from apps.events.official_check import check_due_events
    from apps.ingest.models import Source
    from apps.ingest.services import _record_failure, _record_success, mark_poll_started

    source = Source.objects.filter(slug=JUDICIAL_SEARCH_SLUG).first()
    if source is not None:
        mark_poll_started(source)
    summary = check_due_events(force=force)
    now = timezone.now()
    if source is not None:
        if summary.session_expired:
            _record_failure(source, now, "司法院登入 session 過期或尚未設定")
        else:
            _record_success(source, now)

    if summary.session_expired:
        logger.warning(
            "司法院資料開放平台登入 session 已過期或尚未設定，"
            "本次官方源檢查未執行——請至 /crawlers/ 重新登入")
    else:
        logger.info(
            "官方源檢查完成：%d 個事件、%d 份封存檔、%d 筆新進展",
            summary.events_checked, summary.archives_downloaded, len(summary.results))
    return {
        "events_checked": summary.events_checked,
        "archives_downloaded": summary.archives_downloaded,
        "hits": len(summary.results),
        "session_expired": summary.session_expired,
    }


@shared_task(
    name="apps.events.tasks.check_official_records",
    queue="fetch",
    acks_late=True,
    soft_time_limit=600,
    time_limit=720,
)
def check_official_records(force: bool = False) -> dict:
    """派發官方源檢查。走 fetch 佇列——這是輕量 HTTP 下載（雖然單檔
    可達 200MB+，但不是 Playwright），與 browser 佇列的資源特性不同
    （ADR-0007）。

    逾時設得比一般 fetch 任務寬鬆：下載月封存檔本身可能需要數十秒到
    數分鐘（實測 270MB 在正常頻寬下約 1-2 分鐘），加上可能查兩個月份。
    """
    return run_official_check(force=force)


@shared_task(
    name="apps.events.tasks.check_procurement",
    queue="fetch",
    acks_late=True,
    soft_time_limit=900,
    time_limit=960,
)
def check_procurement(dry_run: bool = False) -> dict:
    """對追蹤中的事件查政府採購網（任務 55）。

    查詢驅動而非全量入庫——採購網有數百萬筆標案，相關的是極少數。
    冪等：寫入走 upsert。
    """
    from apps.events.procurement_check import check_events

    summary = check_events(dry_run=dry_run)
    return {
        "events_checked": summary.events_checked,
        "queries": summary.queries,
        "hits": summary.hits,
        "created": summary.created,
        "updated": summary.updated,
    }
