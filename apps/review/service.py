"""審核動作（任務 45，規格 §4.3.1）。

視圖只負責接表單；規則在這裡強制。高風險禁止批次、必須逐條確認
時間線與措辭——這兩條如果只靠 UI 不畫按鈕，改 POST 就能繞過。
"""
from __future__ import annotations

import dataclasses

from apps.events.models import (
    Event, EventStatus, InvalidTransition, RiskTier, WordingGateFailed,
)

__all__ = [
    "ReviewError", "ReviewIncomplete", "NotAwaitingReview", "ReviewBlocked",
    "ReviewItems", "BatchResult",
    "collect_review_items", "missing_confirmations",
    "approve", "reject", "batch_approve",
]


class ReviewError(Exception):
    """審核動作被拒絕。message 可直接顯示給使用者。"""


class ReviewIncomplete(ReviewError):
    """高風險事件尚未逐條確認完畢。"""


class NotAwaitingReview(ReviewError):
    """不在 candidate／draft，無法審核。"""


class ReviewBlocked(ReviewError):
    """措辭閘門或狀態機拒絕。"""


@dataclasses.dataclass(frozen=True)
class ReviewItems:
    wording_keys: tuple[str, ...]
    document_ids: tuple[int, ...]
    node_ids: tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class BatchResult:
    approved: tuple[Event, ...]
    skipped_high: tuple[Event, ...]
    blocked: tuple[tuple[Event, str], ...]


_PENDING = frozenset({EventStatus.CANDIDATE, EventStatus.DRAFT})

_WORDING_LABELS = {
    "title": "標題",
    "summary": "摘要",
    "current_status_text": "目前進度",
}


def collect_review_items(event: Event) -> ReviewItems:
    """此事件「逐條確認」需要勾選的項目。

    未存檔的事件沒有關聯列可查（測試用 save=False 路徑），只回傳措辭欄位。
    """
    wording = ["title"]
    if event.summary:
        wording.append("summary")
    if event.current_status_text:
        wording.append("current_status_text")
    document_ids: tuple[int, ...] = ()
    node_ids: tuple[int, ...] = ()
    if event.pk:
        document_ids = tuple(
            event.event_documents.values_list("document_id", flat=True)
        )
        node_ids = tuple(event.timeline_nodes.values_list("pk", flat=True))
    return ReviewItems(tuple(wording), document_ids, node_ids)


def missing_confirmations(
    *,
    risk_tier: str,
    items: ReviewItems,
    confirmed_wording: set[str] | frozenset[str] = frozenset(),
    confirmed_documents: set[int] | frozenset[int] = frozenset(),
    confirmed_nodes: set[int] | frozenset[int] = frozenset(),
) -> tuple[str, ...]:
    """尚未確認的項目。中低風險永遠回空——形式確認就是按下「通過」。"""
    if risk_tier != RiskTier.HIGH:
        return ()
    missing: list[str] = []
    for key in items.wording_keys:
        if key not in confirmed_wording:
            missing.append(_WORDING_LABELS.get(key, key))
    if items.document_ids and not set(items.document_ids) <= confirmed_documents:
        n = len(set(items.document_ids) - confirmed_documents)
        missing.append(f"{n} 篇時間線文件")
    if items.node_ids and not set(items.node_ids) <= confirmed_nodes:
        n = len(set(items.node_ids) - confirmed_nodes)
        missing.append(f"{n} 則時間線節點")
    return tuple(missing)


def approve(
    event: Event,
    *,
    confirmed_wording: set[str] | frozenset[str] = frozenset(),
    confirmed_documents: set[int] | frozenset[int] = frozenset(),
    confirmed_nodes: set[int] | frozenset[int] = frozenset(),
    publish: bool = False,
    save: bool = True,
) -> None:
    """candidate／draft → active；可選擇接著公開。

    高風險必須把 ``collect_review_items`` 回傳的項目全部勾完。
    多勾的 id 不算數——不能靠多勾無關項目來補漏。
    """
    if event.status not in _PENDING:
        raise NotAwaitingReview(
            f"「{event.title}」不在待審核狀態（目前為 {event.get_status_display()}）"
        )
    items = collect_review_items(event)
    missing = missing_confirmations(
        risk_tier=event.risk_tier, items=items,
        confirmed_wording=confirmed_wording,
        confirmed_documents=confirmed_documents,
        confirmed_nodes=confirmed_nodes,
    )
    if missing:
        raise ReviewIncomplete(
            f"高風險事件必須逐條確認時間線與措辭，尚未確認：{'、'.join(missing)}"
        )
    try:
        if event.status == EventStatus.CANDIDATE:
            event.transition_to(EventStatus.DRAFT, save=save)
        if event.status == EventStatus.DRAFT:
            event.transition_to(EventStatus.ACTIVE, save=save)
        if publish:
            event.publish(save=save)
    except (WordingGateFailed, InvalidTransition) as exc:
        raise ReviewBlocked(_message(exc)) from exc


def reject(event: Event, *, save: bool = True) -> None:
    if event.status not in _PENDING:
        raise NotAwaitingReview(
            f"「{event.title}」不在待審核狀態（目前為 {event.get_status_display()}）"
        )
    try:
        event.transition_to(EventStatus.REJECTED, save=save)
    except InvalidTransition as exc:
        raise ReviewBlocked(_message(exc)) from exc


def batch_approve(events, *, save: bool = True) -> BatchResult:
    """中低風險一次通過。高風險被跳過，不讓整批失敗。

    不公開——批次公開會讓「要不要把真實事件放到公開頁」變成一次誤點
    就發生的事，那個決定應留在單件的「通過並公開」。
    """
    approved: list[Event] = []
    skipped_high: list[Event] = []
    blocked: list[tuple[Event, str]] = []
    for event in events:
        if not event.allows_batch_review:
            skipped_high.append(event)
            continue
        try:
            approve(event, save=save)
            approved.append(event)
        except ReviewError as exc:
            blocked.append((event, str(exc)))
    return BatchResult(tuple(approved), tuple(skipped_high), tuple(blocked))


def _message(exc: BaseException) -> str:
    messages = getattr(exc, "messages", None)
    if messages:
        return messages[0]
    return str(exc)
