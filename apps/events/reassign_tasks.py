"""單一事件的重新歸屬（手動觸發）。

``auto_assign_events`` 是全體事件的批次指令；這裡是給 UI 的單事件版本
——使用者在某個事件頁面上看到「這個案子明顯少了幾篇報導」時，應該
能只重跑那一個，不必為此對全部事件重跑一輪 LLM 判定。
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: 每個事件一把鎖。同一事件重複派工只會讓兩份判定互相覆寫並付兩次錢，
#: 但不同事件可以並行。
LOCK_PREFIX = "event:reassign:"
LOCK_TTL = 1200

#: 單次重新歸屬的 LLM 呼叫上限。候選視窗 300 篇、20 篇一批 = 15 次，
#: 取 20 留點餘裕。這是節流，預算硬上限仍由 apps.llm.budget 把關。
MAX_LLM_CALLS = 20


def lock_key(slug: str) -> str:
    return f"{LOCK_PREFIX}{slug}"


def is_running(slug: str) -> bool:
    return cache.get(lock_key(slug)) is not None


@shared_task(
    name="apps.events.reassign_tasks.reassign_event",
    queue="fetch", acks_late=True, soft_time_limit=1200, time_limit=1260,
)
def reassign_event(slug: str) -> dict:
    """對單一事件重跑歸屬。新掛上的文件會自動觸發 L1 抽取。"""
    from apps.events.assignment import auto_assign
    from apps.events.models import Event
    from apps.llm.provider import LlmError

    event = Event.objects.filter(slug=slug).first()
    if event is None:
        cache.delete(lock_key(slug))
        return {"error": "事件不存在"}

    try:
        results = auto_assign([event], max_llm_calls=MAX_LLM_CALLS)
    except LlmError as exc:
        # 預算用罄等——已寫入的歸屬保留（auto_assign 每批即時寫入）。
        logger.warning("重新歸屬 %s 中止：%s", slug, exc)
        cache.delete(lock_key(slug))
        return {"error": str(exc)}
    finally:
        cache.delete(lock_key(slug))

    logger.info("重新歸屬 %s：新增 %d 篇", slug, len(results))
    return {"slug": slug, "linked": len(results)}
