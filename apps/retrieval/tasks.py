"""RAG 前處理的 Celery 任務（相關性判定、向量化）。

**一律走 CPU。** ``EMBEDDING_DEVICE`` 預設 cpu（ADR-0011：GPU 被一個
卡在 D 狀態的程序佔滿，且本地 LLM 計畫已取消）。這裡不提供切換到
GPU 的選項——真要改是設定層的事，不該散在任務參數裡。

兩者都設計成可重複執行：相關性只處理尚未評估者、向量化只處理缺向量
者，中途被殺掉重跑不會重做已完成的部分。
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: 進行中旗標。前端據此顯示「執行中」，也用來擋重複派工——這兩個
#: 任務都是長時間、大量寫入的批次，同時跑兩份只會互相搶資料庫。
RELEVANCE_LOCK = "rag:relevance"
EMBED_LOCK = "rag:embed"
#: 每批的處理量。之後由任務自己接力，不是一次做完——長任務被 worker
#: 重啟打斷時，已完成的批次不會白做。
RELEVANCE_BATCH = 2000
EMBED_BATCH = 200
#: 鎖的存活時間要長於單批耗時，但短到 worker 掛掉後能自然解開。
LOCK_TTL = 1800


def is_running(key: str) -> bool:
    return cache.get(key) is not None


@shared_task(
    name="apps.retrieval.tasks.assess_relevance_batch",
    queue="fetch", acks_late=True, soft_time_limit=1800, time_limit=1860,
)
def assess_relevance_batch(resume: bool = False) -> dict:
    """判定尚未評估文件的相關性。規則式，不呼叫 LLM，不花錢。"""
    from django.core.management import call_command

    from apps.ingest.models import Document

    if not resume and not cache.add(RELEVANCE_LOCK, "1", LOCK_TTL):
        return {"skipped": True}

    remaining = Document.objects.pending_relevance().count()
    if not remaining:
        cache.delete(RELEVANCE_LOCK)
        return {"done": True, "remaining": 0}

    # 沿用既有指令而非複製一份判定邏輯——signals 的組法、批次大小的
    # 上限（bulk_update 在 2000 列時實測超過 850 秒）都在那裡，複製
    # 出來遲早會分岔。
    call_command("assess_relevance", limit=RELEVANCE_BATCH)

    # 用 set 而非 touch：每批都重寫，鎖若因任何原因消失（例如測試
    # 誤刪、Redis 重啟）下一批會自己補回來，不會變成「明明在跑卻
    # 顯示沒在跑」。
    cache.set(RELEVANCE_LOCK, "1", LOCK_TTL)
    assess_relevance_batch.delay(resume=True)
    logger.info("相關性判定：本批 %d 篇，尚餘約 %d", RELEVANCE_BATCH, remaining)
    return {"remaining": remaining, "done": False}


@shared_task(
    name="apps.retrieval.tasks.embed_batch",
    queue="fetch", acks_late=True, soft_time_limit=1800, time_limit=1860,
)
def embed_batch(resume: bool = False) -> dict:
    """把判定為相關、但還沒有向量的文件向量化。CPU 執行（ADR-0011）。"""
    from django.core.management import call_command

    from apps.ingest.models import Document

    if not resume and not cache.add(EMBED_LOCK, "1", LOCK_TTL):
        return {"skipped": True}

    remaining = (Document.objects.relevant()
                 .filter(embedding__isnull=True).exclude(raw_body="").count())
    if not remaining:
        cache.delete(EMBED_LOCK)
        return {"done": True, "remaining": 0}

    # 沿用既有指令而非複製一份編碼邏輯——批次大小、執行緒數與模型
    # 版本的處理都在那裡，複製出來遲早會分岔。
    call_command("embed_documents", limit=EMBED_BATCH)

    cache.set(EMBED_LOCK, "1", LOCK_TTL)
    embed_batch.delay(resume=True)
    logger.info("向量化：本批 %d 篇，尚餘約 %d", EMBED_BATCH, remaining)
    return {"remaining": remaining, "done": False}
