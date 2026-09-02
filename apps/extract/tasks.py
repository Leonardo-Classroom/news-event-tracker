"""L1 抽取的 Celery 任務。

**只抽已歸屬到追蹤事件的文件。** 原本規劃是對全部「判定為相關」的
文件抽取（實測 76,900 篇約 US$145），但消費 ``Extraction`` 的三個地方
——風險分級、措辭檢查的 ``is_final``、L4 時間線生成——全都只作用在
事件的文件上。抽其餘 76,400 篇的結果沒有任何消費者。

改為事件驅動後，同樣的 6 個事件只需抽 479 篇，約 US$0.90，差 161 倍。

**順序仍是 L3 先、L1 後，這點不變。** L3 判定每篇只送標題加 200 字
摘要、20 篇一批攤提 prompt，單篇 US$0.000108；L1 每篇送 6000 字全文
逐篇呼叫，單篇 US$0.00189——貴 18 倍。先抽再判等於對最後會被剔除的
候選（約佔 85%）付全文抽取的錢。
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

#: 單次任務的抽取上限。超過就自己再入列——L1 是要花錢的呼叫，
#: 一次做太多會讓「跑到一半預算用完」變成難以收拾的狀態。
BATCH = 40


@shared_task(
    name="apps.extract.tasks.extract_for_documents",
    queue="fetch", acks_late=True, soft_time_limit=1800, time_limit=1860,
)
def extract_for_documents(document_ids: list[int]) -> dict:
    """對指定文件執行 L1 抽取。由事件歸屬觸發。

    冪等：已用當前 prompt 版本成功抽取過的會被跳過，重跑不會重複付費。
    """
    from apps.extract.models import PROMPT_VERSION, Extraction
    from apps.extract.service import extract_document
    from apps.ingest.models import Document
    from apps.llm.provider import LlmError

    done = set(Extraction.objects
               .filter(document_id__in=document_ids,
                       prompt_version=PROMPT_VERSION, succeeded=True)
               .values_list("document_id", flat=True))
    todo = [i for i in document_ids if i not in done][:BATCH]
    if not todo:
        return {"extracted": 0, "skipped": len(done), "done": True}

    ok = failed = 0
    for doc in Document.objects.filter(id__in=todo).exclude(raw_body=""):
        try:
            extraction = extract_document(doc, task_name="event_linked")
            ok += extraction.succeeded
            failed += not extraction.succeeded
        except LlmError as exc:
            # 預算用罄或 API 失敗：停止整批，不要把剩下的也一起打掉。
            # 已完成的抽取結果仍然保留。
            logger.warning("L1 抽取中止：%s", exc)
            return {"extracted": ok, "failed": failed, "aborted": str(exc)}

    remaining = [i for i in document_ids if i not in done and i not in set(todo)]
    if remaining:
        extract_for_documents.delay(remaining)
    logger.info("L1 抽取：成功 %d、失敗 %d、尚餘 %d", ok, failed, len(remaining))
    return {"extracted": ok, "failed": failed, "remaining": len(remaining)}
