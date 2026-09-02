"""以自然語言與 AI 討論、產出候選事件（任務 63）。

**為什麼獨立成一個 app 而不是塞進 apps.events。** 這裡的資料是
「討論過程」——對話、訊息、被丟進回收桶的草稿。它們與 ``Event``
的生命週期無關：對話可以刪掉、重來、永遠不產出任何事件，而 ``Event``
一旦建立就進入審核與公開的正式流程。混在同一個 app 會讓「什麼是
正式資料、什麼是草稿」變得模糊。

**刪除一律是軟刪除。** 使用者要的是回收桶，不是真的刪除；而且
對話可能已經產生了追蹤中的事件，硬刪會讓那些事件失去來歷。
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class ConversationQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def trashed(self):
        return self.filter(deleted_at__isnull=False)


class Conversation(models.Model):
    """一串與 AI 的討論。"""

    #: 建立時以日期時間為名，第一輪討論後由 AI 換成主題名稱。
    title = models.CharField(max_length=128)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,
                              on_delete=models.CASCADE,
                              related_name="event_conversations")
    #: 軟刪除。非 null 即在回收桶裡。
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: 標題是否已由 AI 依主題重新命名——只做一次，之後使用者改的名字
    #: 不該被下一輪對話覆蓋。
    title_generated = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    objects = ConversationQuerySet.as_manager()

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title

    def trash(self) -> None:
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at", "updated_at"])

    def restore(self) -> None:
        self.deleted_at = None
        self.save(update_fields=["deleted_at", "updated_at"])


class Role(models.TextChoices):
    USER = "user", "使用者"
    ASSISTANT = "assistant", "AI"


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     related_name="messages")
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    #: AI 在這則回覆中引用的館藏文件。**外鍵而非自由文字**——模型
    #: 只能從我們檢索出來的候選裡挑，挑不到就沒有連結；讓它自己寫
    #: 標題與網址等於允許它捏造不存在的新聞。
    documents = models.ManyToManyField("ingest.Document", blank=True,
                                       related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.get_role_display()}：{self.content[:40]}"


class SuggestionQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)


class EventSuggestion(models.Model):
    """AI 推薦的候選事件。按下「建立事件」才會產生真正的 ``Event``。"""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     related_name="suggestions")
    title = models.CharField(max_length=256)
    summary = models.TextField(blank=True)
    #: 自由標籤（人物、機關、案件類型…），供使用者快速判斷。
    tags = models.JSONField(default=list, blank=True)
    #: 建立出來的事件。非 null 表示已加入追蹤，按鈕要變成「已追蹤」。
    created_event = models.ForeignKey(
        "events.Event", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="from_suggestions")
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SuggestionQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return self.title

    @property
    def is_tracked(self) -> bool:
        return self.created_event_id is not None
