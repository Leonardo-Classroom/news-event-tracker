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
            finish_reason = "stop"
            output_tokens = 0

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


@pytest.mark.medium
class TestCitedDocuments:
    """AI 推薦的新聞必須是館藏裡真實存在的文件。

    讓模型自己寫標題與網址等於允許它捏造不存在的新聞——因此它只能
    從我們檢索出來的候選裡挑編號，其餘一律丟棄。
    """

    @pytest.fixture
    def docs(self, db):
        from apps.ingest.models import ContentClass, Document, Source, SourceType
        source = Source.objects.create(
            slug="s", name="測試報", type=SourceType.NEWS_SCRAPE,
            base_url="https://example.test")
        return [Document.objects.create(
            source=source, url=f"https://example.test/{i}",
            title=f"新竹棒球場相關報導{i}", raw_body="內文" * 60,
            content_class=ContentClass.COPYRIGHTED) for i in range(3)]

    def test_引用被掛在回覆訊息上(self, conversation, docs):
        turn = dict(TURN, documents=[docs[0].pk, docs[1].pk])
        with patch("apps.eventbuilder.service.retrieve_candidates",
                   return_value=docs):
            result = send_message(conversation, "新竹棒球場",
                                  provider=FakeProvider([turn, "標題"]))
        assert {d.pk for d in result.documents} == {docs[0].pk, docs[1].pk}
        assistant = conversation.messages.filter(role=Role.ASSISTANT).first()
        assert assistant.documents.count() == 2

    def test_不存在的編號被丟棄(self, conversation, docs):
        """模型可能回傳幻覺 id——以本輪候選集合做交集。"""
        turn = dict(TURN, documents=[docs[0].pk, 999999])
        with patch("apps.eventbuilder.service.retrieve_candidates",
                   return_value=docs):
            result = send_message(conversation, "問題",
                                  provider=FakeProvider([turn, "標題"]))
        assert [d.pk for d in result.documents] == [docs[0].pk]

    def test_沒提供給它的文件也不能引用(self, conversation, docs):
        """即使 id 真實存在，只要不在本輪候選裡就不算——否則模型可以
        靠猜編號引用任意文件。"""
        turn = dict(TURN, documents=[docs[2].pk])
        with patch("apps.eventbuilder.service.retrieve_candidates",
                   return_value=docs[:2]):
            result = send_message(conversation, "問題",
                                  provider=FakeProvider([turn, "標題"]))
        assert result.documents == []

    def test_檢索失敗不影響對話(self, conversation):
        with patch("apps.eventbuilder.service.BigramFtsBackend.search",
                   side_effect=RuntimeError("索引壞了")):
            result = send_message(conversation, "問題",
                                  provider=FakeProvider([TURN, "標題"]))
        assert result.error == ""
        assert result.documents == []

    def test_引用渲染為連到我方文件頁的新分頁(self, client, user, conversation, docs):
        client.force_login(user)
        turn = dict(TURN, documents=[docs[0].pk])
        with patch("apps.eventbuilder.service.retrieve_candidates",
                   return_value=docs), \
             patch("apps.eventbuilder.service.get_provider",
                   return_value=FakeProvider([turn, "標題"])):
            r = client.post(f"/build/c/{conversation.pk}/send/", {"text": "問題"})
        html = r.json()["cited_html"]
        assert f'href="/documents/{docs[0].pk}/"' in html
        assert 'target="_blank"' in html
        assert docs[0].title in html

    def test_重新載入頁面仍看得到引用(self, client, user, conversation, docs):
        client.force_login(user)
        turn = dict(TURN, documents=[docs[0].pk])
        with patch("apps.eventbuilder.service.retrieve_candidates",
                   return_value=docs), \
             patch("apps.eventbuilder.service.get_provider",
                   return_value=FakeProvider([turn, "標題"])):
            client.post(f"/build/c/{conversation.pk}/send/", {"text": "問題"})
        html = client.get(f"/build/c/{conversation.pk}/").content.decode()
        assert f'href="/documents/{docs[0].pk}/"' in html


@pytest.mark.medium
class TestMalformedResponse:
    """模型偶爾回傳截斷或非 JSON 的內容（使用者實際遇到過空回應，
    以及在 `"suggestions":[],` 就斷掉的截斷 JSON）。"""

    def test_壞掉後重試一次就成功(self, conversation):
        provider = FakeProvider(["不是 JSON", TURN, "標題"])
        result = send_message(conversation, "問題", provider=provider)
        assert result.error == ""
        assert len(result.suggestions) == 1

    def test_截斷的json至少救回reply(self, conversation):
        """實測失敗案例中 reply 本身是完整的，只有後面的欄位被切掉
        ——與其整輪失敗，不如至少把話講完。"""
        broken = '{"reply":"我無法用政黨立場替你篩選案件。","suggestions":[],'
        result = send_message(conversation, "問題",
                              provider=FakeProvider([broken, broken, "標題"]))
        assert result.error == ""
        assert result.reply == "我無法用政黨立場替你篩選案件。"
        assert result.suggestions == []

    def test_完全無法解析時給可讀訊息(self, conversation):
        """不要把 JSONDecodeError 的原文丟給使用者——
        「Expecting value: line 1 column 1 (char 0)」指不出任何原因。"""
        result = send_message(conversation, "問題",
                              provider=FakeProvider(["", ""]))
        assert "格式異常" in result.error
        assert "Expecting value" not in result.error

    def test_撞上max_tokens仍先嘗試救回reply(self, conversation):
        """推理內容會計入 completion_tokens 卻不在 content 裡（實測
        out_tok=3911 但 JSON 只有 419 字元），因此撞上限時 reply 往往
        還是完整的——先救再說。"""
        class Truncated(FakeProvider):
            def complete(self, **kwargs):
                r = super().complete(**kwargs)
                r.finish_reason = "length"
                r.output_tokens = 8192
                return r

        broken = '{"reply":"京華城案二審已開庭。","suggestions":[],'
        result = send_message(conversation, "問題",
                              provider=Truncated([broken, "標題"]))
        assert result.error == ""
        assert result.reply == "京華城案二審已開庭。"

    def test_撞上上限且救不回時不怪使用者(self, conversation):
        """原本寫「請把問題描述得更聚焦一些」是錯的建議——真正的原因
        是推理吃掉了額度，不是使用者問得太廣。"""
        class Truncated(FakeProvider):
            def complete(self, **kwargs):
                r = super().complete(**kwargs)
                r.finish_reason = "length"
                r.output_tokens = 8192
                return r

        result = send_message(conversation, "問題", provider=Truncated(["半句"]))
        assert "再試一次" in result.error
        assert "聚焦" not in result.error

    def test_給足輸出額度(self, conversation):
        provider = FakeProvider([TURN, "標題"])
        send_message(conversation, "問題", provider=provider)
        assert provider.calls[0]["max_tokens"] >= 8192


@pytest.mark.medium
class TestTrashModal:
    """回收桶以 modal 彈出；直接開網址仍要有完整頁面（modal 需要 JS，
    保留可直接連結的版本作為退路）。"""

    def test_fetch時只回片段(self, client, user, conversation):
        client.force_login(user)
        conversation.trash()
        r = client.get("/build/trash/", headers={"X-Modal": "1"})
        html = r.content.decode()
        assert conversation.title in html
        assert "<html" not in html          # 片段，不是整頁
        assert "trash-tb" in html

    def test_直接開網址仍是完整頁面(self, client, user, conversation):
        client.force_login(user)
        conversation.trash()
        html = client.get("/build/trash/").content.decode()
        assert "<html" in html
        assert conversation.title in html

    def test_modal內還原回json不轉址(self, client, user, conversation):
        client.force_login(user)
        conversation.trash()
        r = client.post(f"/build/c/{conversation.pk}/restore/",
                        headers={"X-Modal": "1"})
        assert r.status_code == 200
        assert r["Content-Type"].startswith("application/json")
        conversation.refresh_from_db()
        assert conversation.deleted_at is None

    def test_modal內永久刪除回json不轉址(self, client, user, conversation):
        client.force_login(user)
        conversation.trash()
        r = client.post(f"/build/c/{conversation.pk}/delete/",
                        headers={"X-Modal": "1"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert not Conversation.objects.filter(pk=conversation.pk).exists()

    def test_表單送出仍走轉址(self, client, user, conversation):
        """沒有 JS 時是一般表單，必須維持原本的轉址行為。"""
        client.force_login(user)
        conversation.trash()
        r = client.post(f"/build/c/{conversation.pk}/restore/")
        assert r.status_code == 302


@pytest.mark.medium
class TestRetrievalContext:
    """檢索的兩個實測缺陷：只看最後一則訊息、只看相關性不看時間。"""

    def test_查詢帶入對話脈絡(self, conversation):
        """「有沒有新一點的新聞」本身沒有主題詞，只拿它去檢索會撈到
        除濕機與客語電影，AI 於是說「館藏沒有相關報導」。"""
        from apps.eventbuilder.service import conversation_query

        Message.objects.create(conversation=conversation, role=Role.USER,
                               content="那有關柯P的案件呢")
        query = conversation_query(conversation, "有沒有新一點的新聞")
        assert "柯P" in query
        assert "有沒有新一點的新聞" in query

    def test_脈絡去重(self, conversation):
        from apps.eventbuilder.service import conversation_query

        Message.objects.create(conversation=conversation, role=Role.USER,
                               content="京華城")
        query = conversation_query(conversation, "京華城")
        assert query.count("京華城") == 1

    def test_只取最近幾則不無限累積(self, conversation):
        from apps.eventbuilder.service import conversation_query

        for i in range(10):
            Message.objects.create(conversation=conversation, role=Role.USER,
                                   content=f"訊息{i}")
        query = conversation_query(conversation, "最新")
        assert "訊息0" not in query
        assert "訊息9" in query

    def test_送出時用的是脈絡查詢(self, conversation):
        Message.objects.create(conversation=conversation, role=Role.USER,
                               content="柯文哲京華城案")
        with patch("apps.eventbuilder.service.retrieve_candidates",
                   return_value=[]) as mocked:
            send_message(conversation, "有沒有新一點的",
                         provider=FakeProvider([TURN, "標題"]))
        used = mocked.call_args[0][0]
        assert "柯文哲京華城案" in used


@pytest.mark.medium
class TestResumeAfterRefresh:
    """刷新不會弄丟回覆：使用者訊息在呼叫模型前就寫入，伺服器端即使
    在瀏覽器斷線後也會跑完並存檔（實測斷線 45 秒後訊息確實在）。
    缺的只是頁面不知道有一則正在路上。"""

    def test_有問題沒回答時頁面標記等待中(self, client, user, conversation):
        client.force_login(user)
        Message.objects.create(conversation=conversation, role=Role.USER,
                               content="京華城進度？")
        html = client.get(f"/build/c/{conversation.pk}/").content.decode()
        assert 'data-awaiting="1"' in html

    def test_回答完成後不再標記等待(self, client, user, conversation):
        client.force_login(user)
        Message.objects.create(conversation=conversation, role=Role.USER,
                               content="問")
        Message.objects.create(conversation=conversation, role=Role.ASSISTANT,
                               content="答")
        html = client.get(f"/build/c/{conversation.pk}/").content.decode()
        assert 'data-awaiting="1"' not in html

    def test_輪詢端點在未完成時回ready_false(self, client, user, conversation):
        client.force_login(user)
        Message.objects.create(conversation=conversation, role=Role.USER,
                               content="問")
        r = client.get(f"/build/c/{conversation.pk}/pending/")
        assert r.json()["ready"] is False

    def test_輪詢端點在完成後回內容(self, client, user, conversation):
        client.force_login(user)
        Message.objects.create(conversation=conversation, role=Role.USER,
                               content="問")
        Message.objects.create(conversation=conversation, role=Role.ASSISTANT,
                               content="京華城案二審已開庭")
        d = client.get(f"/build/c/{conversation.pk}/pending/").json()
        assert d["ready"] is True
        assert d["reply"] == "京華城案二審已開庭"
        assert "suggestions_html" in d

    def test_輪詢端點看不到別人的對話(self, client, conversation,
                                      django_user_model):
        other = django_user_model.objects.create_user("nosy", password="x")
        client.force_login(other)
        r = client.get(f"/build/c/{conversation.pk}/pending/")
        assert r.status_code == 404
        assert r.json()["stale"] is True
