"""事件層的 Celery 任務（任務 27+33）。"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name="apps.events.tasks.check_official_records",
    queue="fetch",
    acks_late=True,
    soft_time_limit=600,
    time_limit=720,
)
def check_official_records() -> dict:
    """派發官方源檢查。走 fetch 佇列——這是輕量 HTTP 下載（雖然單檔
    可達 200MB+，但不是 Playwright），與 browser 佇列的資源特性不同
    （ADR-0007）。

    逾時設得比一般 fetch 任務寬鬆：下載月封存檔本身可能需要數十秒到
    數分鐘（實測 270MB 在正常頻寬下約 1-2 分鐘），加上可能查兩個月份。
    """
    from apps.events.official_check import check_due_events

    summary = check_due_events()
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
