"""裁判書入庫與接回事件（任務 34、36、37）。

與 ``apps.ingest.services.ingest_source`` 分開寫，而非重用它——後者
是為「一次抓一批列表、內文另外補」的新聞來源設計（``defaults`` 不含
``raw_body``，全文由任務 9.5 的三層抽取另外補齊）。裁判書相反：
單次查詢就是一份完整文件，全文在解析當下就已經有了，沒有「先建立
再補內文」的理由。

## 這裡把「入庫」與「接回事件」放在同一個函式

任務 34（`public_record` 處理）與任務 36（案號比對接回事件）在概念上
是兩件事，但拆開寫會製造一個真實的失效模式：裁判書入庫成功、
接回事件失敗（或反過來），兩者發生在不同時間點，而中間那個「有
裁判書、但沒接上事件」的狀態不會被任何東西看見。ADR-0003 的原則
「圖的可見性與 Event 狀態同表同交易」在這裡延伸為「入庫與接回同一
次呼叫」——不是同一個資料庫交易（案號可能對到 0 個事件，那不是
錯誤），但至少是同一次操作看得到完整結果。
"""
from __future__ import annotations

import dataclasses
import logging

from django.db import transaction
from django.utils import timezone

from apps.events.assignment import identifier_match
from apps.events.models import AssignmentMethod, Event, EventDocument, EventStatus
from apps.ingest.adapters.judicial import JudicialAdapter
from apps.ingest.models import ContentClass, Document, Source

__all__ = ["JudgmentIngestResult", "ingest_judgment"]

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class JudgmentIngestResult:
    document: Document | None = None
    created: bool = False
    matched_event: Event | None = None
    woke_dormant_event: bool = False
    error: str = ""


def ingest_judgment(
    html: str, *, url: str, source: Source, candidate_events: list[Event] | None = None,
) -> JudgmentIngestResult:
    """解析一份裁判書頁面、入庫，並嘗試以案號接回既有事件。

    Args:
        candidate_events: 案號比對的候選集合。預設為全部追蹤中
            （active／dormant）的事件——裁判書要能喚醒 dormant 事件
            正是任務 37 的核心價值，若只比對 active 就會漏掉這個情境。
    """
    parsed = JudicialAdapter().list_documents(html, base_url=url)
    if not parsed:
        return JudgmentIngestResult(error="無法從頁面解析出案號，未建立文件")

    item = parsed[0]

    with transaction.atomic():
        document, created = Document.objects.update_or_create(
            url=item.url,
            defaults={
                "source": source,
                "title": item.title[:512],
                "raw_body": item.body,
                "published_at": item.published_at,
                "external_id": item.external_id,
                # 裁判書依著作權法第 9 條不受著作權保護，可全文公開
                # ——這與新聞的 COPYRIGHTED 是本系統最重要的一組區分
                "content_class": ContentClass.PUBLIC_RECORD,
                "relevant": True,  # 裁判書本質上就是司法案件，不需再跑規則過濾
                "fetched_at": timezone.now(),
            },
        )

    result = JudgmentIngestResult(document=document, created=created)

    events = candidate_events
    if events is None:
        events = list(Event.objects.filter(
            status__in=[EventStatus.ACTIVE, EventStatus.DORMANT]))

    matched = identifier_match(document, events)
    if matched is None:
        logger.info("裁判書 %s 未比對到任何追蹤中的事件（案號：%s）",
                   document.pk, item.external_id)
        return result

    with transaction.atomic():
        EventDocument.objects.get_or_create(
            event=matched, document=document,
            defaults={"method": AssignmentMethod.IDENTIFIER,
                     "reason": f"裁判書案號精確比對：{item.external_id}"},
        )
        was_dormant = matched.status == EventStatus.DORMANT
        moment = document.published_at or timezone.now()
        woke = matched.record_progress(moment, save=True)
        matched.record_official_check(timezone.now(), save=True)

    result.matched_event = matched
    result.woke_dormant_event = woke
    if woke:
        logger.info(
            "★ 事件 %s 因官方紀錄喚醒（%s → active）——"
            "這是規格 G4／任務 37 的核心價值：新聞停了，追蹤沒停。",
            matched.slug, "dormant" if was_dormant else matched.status,
        )
    return result
