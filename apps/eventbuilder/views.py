"""建立事件：與 AI 討論、產出候選、加入追蹤（任務 63）。

對話與候選都是**每個使用者自己的**——討論過程是草稿，不是共享資料。
所有查詢一律先以 ``owner=request.user`` 收斂，避免靠猜 id 讀到別人的
對話。
"""
from __future__ import annotations

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from apps.eventbuilder.models import Conversation, EventSuggestion
from apps.eventbuilder.service import create_event_from, default_title, send_message
from apps.web.permissions import Role, require_role


def _owned(request):
    return Conversation.objects.filter(owner=request.user)


def _context(request, conversation=None):
    return {
        "nav": "eventbuilder",
        "conversations": _owned(request).alive(),
        "conversation": conversation,
        "messages_list": list(conversation.messages.all()) if conversation else [],
        "suggestions": (list(conversation.suggestions.alive()
                             .select_related("created_event"))
                        if conversation else []),
    }


@require_role(Role.USER)
def builder(request, pk: int | None = None):
    conversation = None
    if pk is not None:
        conversation = get_object_or_404(_owned(request).alive(), pk=pk)
    return render(request, "eventbuilder/builder.html", _context(request, conversation))


@require_role(Role.USER)
@require_POST
def new_conversation(request):
    conversation = Conversation.objects.create(
        owner=request.user, title=default_title())
    return redirect("eventbuilder:conversation", pk=conversation.pk)


@require_role(Role.USER)
@require_POST
def post_message(request, pk: int):
    """送出訊息並回傳新的訊息與候選事件片段。

    以 fetch 呼叫、回傳 HTML 片段而非整頁重載——對話介面若每次都整頁
    跳轉，捲動位置與輸入焦點都會丟失。
    """
    conversation = get_object_or_404(_owned(request).alive(), pk=pk)
    result = send_message(conversation, request.POST.get("text", ""))
    conversation.refresh_from_db()
    if result.error:
        return JsonResponse({"error": result.error}, status=502)
    return JsonResponse({
        "reply": result.reply,
        "title": conversation.title,
        "suggestions_html": render_to_string(
            "eventbuilder/_suggestions.html",
            {"suggestions": list(conversation.suggestions.alive()
                                 .select_related("created_event"))},
            request=request),
    })


@require_role(Role.USER)
@require_POST
def rename_conversation(request, pk: int):
    conversation = get_object_or_404(_owned(request).alive(), pk=pk)
    title = (request.POST.get("title") or "").strip()
    if title:
        conversation.title = title[:128]
        # 使用者親手改的名字不該被下一輪對話的自動命名覆寫。
        conversation.title_generated = True
        conversation.save(update_fields=["title", "title_generated", "updated_at"])
    return redirect("eventbuilder:conversation", pk=conversation.pk)


@require_role(Role.USER)
@require_POST
def trash_conversation(request, pk: int):
    conversation = get_object_or_404(_owned(request).alive(), pk=pk)
    conversation.trash()
    return redirect("eventbuilder:builder")


@require_role(Role.USER)
def trash(request):
    return render(request, "eventbuilder/trash.html", {
        "nav": "eventbuilder",
        "conversations": _owned(request).alive(),
        "trashed": _owned(request).trashed(),
    })


@require_role(Role.USER)
@require_POST
def restore_conversation(request, pk: int):
    conversation = get_object_or_404(_owned(request).trashed(), pk=pk)
    conversation.restore()
    return redirect("eventbuilder:conversation", pk=conversation.pk)


@require_role(Role.USER)
@require_POST
def delete_forever(request, pk: int):
    """真的刪除。只允許已經在回收桶裡的——避免一次誤點就永久失去。"""
    get_object_or_404(_owned(request).trashed(), pk=pk).delete()
    return redirect("eventbuilder:trash")


def _suggestion(request, pk: int) -> EventSuggestion:
    return get_object_or_404(
        EventSuggestion.objects.filter(conversation__owner=request.user), pk=pk)


@require_role(Role.USER)
@require_POST
def track_suggestion(request, pk: int):
    """把候選事件加入追蹤中。"""
    suggestion = _suggestion(request, pk)
    event = create_event_from(suggestion)
    return JsonResponse({"event_slug": event.slug, "event_title": event.title})


@require_role(Role.USER)
@require_POST
def delete_suggestion(request, pk: int):
    suggestion = _suggestion(request, pk)
    from django.utils import timezone

    suggestion.deleted_at = timezone.now()
    suggestion.save(update_fields=["deleted_at"])
    return JsonResponse({"ok": True})
