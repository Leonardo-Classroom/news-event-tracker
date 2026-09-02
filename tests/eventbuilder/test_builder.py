"""建立事件（任務 63）：對話、候選、加入追蹤、回收桶。

LLM 一律注入假的 provider——真呼叫會花錢，而且回應不是決定性的。
"""
import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from apps.eventbuilder.models import Conversation, EventSuggestion, Message, Role
from apps.eventbuilder.service import create_event_from, default_title, send_message
from apps.events.models import Event, EventStatus, EventVisibility, RiskTier


class FakeProvider:
    """回放預錄的模型輸出。"""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        value = self.payloads.pop(0) if self.payloads else ""

        class _R:
            text = value if isinstance(value, str) else json.dumps(value,
                                                                   ensure_ascii=False)

            def json(self):
                return json.loads(self.text)

        return _R()


TURN = {"reply": "了解，這是新竹棒球場的工程弊案。",
        "suggestions": [{"title": "新竹棒球場工程弊案",
                         "summary": "球場啟用後發現多項缺失，檢調偵辦中。",
                         "tags": ["林智堅", "貪瀆", "新竹市"]}]}


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("builder", password="x")


@pytest.fixture
def conversation(user):
    return Conversation.objects.create(owner=user, title=default_title())


@pytest.mark.medium
class TestSendMessage:
    def test_寫入雙方訊息並產生候選(self, conversation):
        provider = FakeProvider([TURN, "新竹棒球場弊案"])
        result = send_message(conversation, "新竹棒球場的案子進度？", provider=provider)

        assert result.error == ""
        assert len(result.suggestions) == 1
        assert result.suggestions[0].tags == ["林智堅", "貪瀆", "新竹市"]
        roles = list(conversation.messages.values_list("role", flat=True))
        assert roles == [Role.USER, Role.ASSISTANT]

    def test_第一輪後自動改名(self, conversation):
        """預設是日期時間，第一輪討論後換成主題名稱。"""
        before = conversation.title
        send_message(conversation, "問題", provider=FakeProvider([TURN, "新竹棒球場弊案"]))
        conversation.refresh_from_db()
        assert conversation.title == "新竹棒球場弊案"
        assert conversation.title != before
        assert conversation.title_generated is True

    def test_改名只做一次(self, conversation):
        """使用者自己改的名字不該被下一輪對話覆寫。"""
        send_message(conversation, "第一輪", provider=FakeProvider([TURN, "自動名稱"]))
        conversation.refresh_from_db()
        conversation.title = "我自己取的名字"
        conversation.save(update_fields=["title"])

        send_message(conversation, "第二輪", provider=FakeProvider([TURN, "又一個自動名稱"]))
        conversation.refresh_from_db()
        assert conversation.title == "我自己取的名字"

    def test_不重複提出已看過的候選(self, conversation):
        provider = FakeProvider([TURN, "標題", TURN])
        send_message(conversation, "第一輪", provider=provider)
        second = send_message(conversation, "第二輪", provider=provider)
        assert second.suggestions == []
        assert conversation.suggestions.count() == 1

    def test_空訊息被拒絕(self, conversation):
        result = send_message(conversation, "   ", provider=FakeProvider([]))
        assert result.error
        assert conversation.messages.count() == 0

    def test_模型失敗時保留使用者訊息(self, conversation):
        """讓使用者看得到自己說過什麼，重試時不必重打。"""
        result = send_message(conversation, "問題", provider=FakeProvider(["不是 JSON"]))
        assert result.error
        assert conversation.messages.count() == 1
        assert conversation.messages.first().role == Role.USER

    def test_只送最近數輪而非整串歷史(self, conversation):
        """討論會越拉越長，全量重送會讓成本隨長度線性成長。"""
        for i in range(40):
            Message.objects.create(conversation=conversation, role=Role.USER,
                                   content=f"訊息{i}")
        provider = FakeProvider([TURN, "標題"])
        send_message(conversation, "最新一則", provider=provider)
        sent = provider.calls[0]["messages"]
        assert len(sent) < 20


@pytest.mark.medium
class TestCreateEvent:
    def test_建立為追蹤中且不公開(self, conversation):
        """加入追蹤與對外公開是兩回事——公開仍須走審核。"""
        s = EventSuggestion.objects.create(conversation=conversation,
                                           title="京華城容積案", summary="摘要")
        event = create_event_from(s)
        assert event.status == EventStatus.ACTIVE
        assert event.visibility == EventVisibility.PRIVATE
        assert event.risk_tier == RiskTier.HIGH
        assert event.first_seen_at and event.last_progress_at

    def test_重複按不會建立第二個(self, conversation):
        s = EventSuggestion.objects.create(conversation=conversation, title="某案")
        assert create_event_from(s).pk == create_event_from(s).pk
        assert Event.objects.count() == 1

    def test_slug衝突時自動編號(self, conversation):
        Event.objects.create(slug="某案", title="既有")
        s = EventSuggestion.objects.create(conversation=conversation, title="某案")
        assert create_event_from(s).slug == "某案-2"


@pytest.mark.medium
class TestViews:
    def test_未登入導向登入頁(self, client, conversation):
        assert client.get("/build/").status_code == 302

    def test_看不到別人的對話(self, client, conversation, django_user_model):
        other = django_user_model.objects.create_user("other", password="x")
        client.force_login(other)
        assert client.get(f"/build/c/{conversation.pk}/").status_code == 404

    def test_送訊息回傳候選片段(self, client, user, conversation):
        client.force_login(user)
        with patch("apps.eventbuilder.service.get_provider",
                   return_value=FakeProvider([TURN, "標題"])):
            r = client.post(f"/build/c/{conversation.pk}/send/", {"text": "問題"})
        assert r.status_code == 200
        data = r.json()
        assert "新竹棒球場工程弊案" in data["suggestions_html"]
        assert data["title"] == "標題"

    def test_模型失敗回502(self, client, user, conversation):
        client.force_login(user)
        with patch("apps.eventbuilder.service.get_provider",
                   return_value=FakeProvider(["壞掉"])):
            r = client.post(f"/build/c/{conversation.pk}/send/", {"text": "問題"})
        assert r.status_code == 502

    def test_刪除是移到回收桶不是真刪(self, client, user, conversation):
        client.force_login(user)
        client.post(f"/build/c/{conversation.pk}/trash/")
        conversation.refresh_from_db()
        assert conversation.deleted_at is not None
        assert Conversation.objects.filter(pk=conversation.pk).exists()
        assert conversation not in Conversation.objects.alive()

    def test_回收桶可還原(self, client, user, conversation):
        client.force_login(user)
        client.post(f"/build/c/{conversation.pk}/trash/")
        client.post(f"/build/c/{conversation.pk}/restore/")
        conversation.refresh_from_db()
        assert conversation.deleted_at is None

    def test_只有回收桶裡的才能永久刪除(self, client, user, conversation):
        """避免一次誤點就永久失去。"""
        client.force_login(user)
        assert client.post(f"/build/c/{conversation.pk}/delete/").status_code == 404
        assert Conversation.objects.filter(pk=conversation.pk).exists()

    def test_追蹤候選事件(self, client, user, conversation):
        client.force_login(user)
        s = EventSuggestion.objects.create(conversation=conversation, title="某案")
        r = client.post(f"/build/s/{s.pk}/track/")
        assert r.status_code == 200
        s.refresh_from_db()
        assert s.is_tracked
        assert r.json()["event_slug"] == s.created_event.slug

    def test_刪除候選是軟刪除(self, client, user, conversation):
        client.force_login(user)
        s = EventSuggestion.objects.create(conversation=conversation, title="某案")
        client.post(f"/build/s/{s.pk}/delete/")
        s.refresh_from_db()
        assert s.deleted_at is not None
        assert s not in EventSuggestion.objects.alive()


@pytest.mark.medium
class TestStaleConversation:
    """分頁開著、對話卻已被刪除。這個端點只被 fetch 呼叫，任何失敗
    都必須回 JSON——回 Django 預設的 HTML 錯誤頁的話，前端解析只會
    得到「Unexpected token '<'」，訊息完全指不出真正原因。"""

    def test_對話不存在回json而非html(self, client, user):
        client.force_login(user)
        r = client.post("/build/c/999999/send/", {"text": "問題"})
        assert r.status_code == 404
        assert r["Content-Type"].startswith("application/json")
        assert r.json()["stale"] is True

    def test_對話已在回收桶回json(self, client, user, conversation):
        client.force_login(user)
        conversation.trash()
        r = client.post(f"/build/c/{conversation.pk}/send/", {"text": "問題"})
        assert r.status_code == 404
        assert r.json()["stale"] is True

    def test_別人的對話仍是404且不洩漏存在與否(self, client, conversation,
                                              django_user_model):
        other = django_user_model.objects.create_user("stranger", password="x")
        client.force_login(other)
        r = client.post(f"/build/c/{conversation.pk}/send/", {"text": "問題"})
        assert r.status_code == 404
        assert r.json()["stale"] is True
