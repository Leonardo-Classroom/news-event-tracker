"""事件合併（任務 26）。

**歸屬階段的漏判必然產生重複事件，這不是附加功能。** ADR-0005 的兩階段
偵測——先歸屬既有事件、未歸屬者才叢集——意味著同一案件在不同時間點
可能被建立成兩個獨立事件（例如：案件剛起訴時建立事件 A，半年後同一
案件的後續報導因為措辭差異未被歸屬到 A，觸發 HDBSCAN 叢集成事件 B）。
合併機制因此是核心流程的必要配套，不是事後補的邊角案例。

## 為何刪除來源事件而非改狀態

考慮過在 ``EventStatus`` 加一個 ``MERGED`` 終態、保留來源事件的列。
問題出在 slug：``Event.slug`` 全表唯一，若來源事件的列還在，它的 slug
就還被佔用，無法讓 ``EventAlias`` 用同一個 slug 指向目標事件做 301
導向——那正是合併機制存在的目的。因此改為刪除來源事件，把需要留存的
資訊（slug、標題、文件數）寫進 ``EventMergeLog``，該表的
``source_event`` 外鍵設 ``SET_NULL``，來源事件刪除後記錄依然完整。
"""
from __future__ import annotations

from django.db import transaction

from apps.events.models import EventAlias, EventDocument, EventMergeLog

__all__ = ["merge_events"]


def merge_events(source, target, *, reason: str = "") -> EventMergeLog:
    """把 ``source`` 併入 ``target``：轉移文件與別名，刪除 source。

    Returns:
        建立的 ``EventMergeLog``，供呼叫端記錄或顯示。
    """
    if source.pk == target.pk:
        raise ValueError("不能將事件併入自己")

    with transaction.atomic():
        already_in_target = EventDocument.objects.filter(
            event=target, document__in=EventDocument.objects.filter(event=source).values("document")
        ).values_list("document_id", flat=True)
        moved = EventDocument.objects.filter(event=source).exclude(document_id__in=already_in_target)
        document_count = moved.count()
        moved.update(event=target)
        # 剩下的是兩邊都已歸屬同一篇文件的重複列，source 這邊直接捨棄
        EventDocument.objects.filter(event=source).delete()

        existing_alias_names = set(
            EventAlias.objects.filter(event=target).values_list("name", flat=True))
        for alias in EventAlias.objects.filter(event=source):
            if alias.name not in existing_alias_names:
                alias.event = target
                alias.save(update_fields=["event"])
                existing_alias_names.add(alias.name)
            # 名稱已存在於 target：這筆連同 source 一起刪除，不需搬移

        if source.slug not in existing_alias_names:
            EventAlias.objects.create(event=target, name=source.slug, is_former_slug=True)

        target.core_terms = sorted(set(target.core_terms) | set(source.core_terms))
        target.case_numbers = sorted(set(target.case_numbers) | set(source.case_numbers))
        if source.last_progress_at and (
            target.last_progress_at is None or source.last_progress_at > target.last_progress_at
        ):
            target.last_progress_at = source.last_progress_at
        target.save(update_fields=["core_terms", "case_numbers", "last_progress_at", "updated_at"])

        log = EventMergeLog.objects.create(
            source_event=source, source_slug=source.slug, source_title=source.title,
            target_event=target, document_count=document_count, reason=reason,
        )
        source.delete()

    return log
