"""事件自動歸屬測試（任務 23，ADR-0005）。"""
import datetime as dt
import json

import pytest

from apps.events.assignment import (
    apply_assignments, auto_assign, identifier_match, judge_candidates,
)
from apps.events.models import AssignmentMethod, Event, EventDocument
from apps.llm.provider import FakeProvider

UTC = dt.timezone.utc


def make_doc(source, url, title, body="內文", case_numbers=None):
    from apps.ingest.models import Document

    doc = Document.objects.create(
        source=source, url=url, title=title, raw_body=body,
        content_class=source.content_class,
        published_at=dt.datetime(2026, 1, 1, tzinfo=UTC),
    )
    if case_numbers:
        doc.case_numbers = case_numbers
        doc.save(update_fields=["case_numbers"])
    return doc


class TestIdentifierMatch:
    def test_案號命中即歸屬(self, db):
        event = Event.objects.create(slug="e", title="事件",
                                     case_numbers=["113年度金訴字第51號"])
        doc = type("D", (), {"case_numbers": ["113年度金訴字第51號"]})()
        assert identifier_match(doc, [event]) is event

    def test_無案號不歸屬(self, db):
        event = Event.objects.create(slug="e", title="事件", case_numbers=["X"])
        doc = type("D", (), {"case_numbers": []})()
        assert identifier_match(doc, [event]) is None

    def test_命中多個事件時交由LLM階段處理(self, db):
        """兩個事件都登記了同一個案號（可能是合併前的殘留），
        規則層無法判斷該歸哪個，回傳 None 讓候選召回與 LLM 接手。"""
        e1 = Event.objects.create(slug="e1", title="事件一", case_numbers=["X"])
        e2 = Event.objects.create(slug="e2", title="事件二", case_numbers=["X"])
        doc = type("D", (), {"case_numbers": ["X"]})()
        assert identifier_match(doc, [e1, e2]) is None


@pytest.mark.medium
class TestJudgeCandidates:
    def test_LLM判定為屬於時產生結果(self, source, db):
        event = Event.objects.create(slug="e", title="京華城容積案")
        doc = make_doc(source, "https://t.test/1", "京華城案偵結")
        provider = FakeProvider(responses=[json.dumps({
            "assignments": [{"doc_id": doc.pk, "belongs": True, "reason": "主題相符"}],
        })])

        results = judge_candidates(event, [doc], provider=provider)

        assert len(results) == 1
        assert results[0].document_id == doc.pk
        assert results[0].method == AssignmentMethod.LLM

    def test_LLM判定為不屬於時無結果(self, source, db):
        event = Event.objects.create(slug="e", title="京華城容積案")
        doc = make_doc(source, "https://t.test/1", "大巨蛋演唱會")
        provider = FakeProvider(responses=[json.dumps({
            "assignments": [{"doc_id": doc.pk, "belongs": False, "reason": "無關"}],
        })])

        assert judge_candidates(event, [doc], provider=provider) == []

    def test_空候選不呼叫LLM(self, db):
        event = Event.objects.create(slug="e", title="事件")
        provider = FakeProvider()
        assert judge_candidates(event, [], provider=provider) == []
        assert provider.calls == []

    def test_LLM回應非法JSON時不崩潰(self, source, db):
        event = Event.objects.create(slug="e", title="事件")
        doc = make_doc(source, "https://t.test/1", "標題")
        provider = FakeProvider(responses=["這不是 JSON"])
        assert judge_candidates(event, [doc], provider=provider) == []

    def test_輸出被截斷時捨棄整批而非誤判為皆不屬於(self, source, db):
        """撞上 max_tokens 時 JSON 必然不完整——若不先檢查 finish_reason，
        截斷的 JSON 會以「解析失敗」的面貌出現，和『模型判斷這批都不屬於』
        無法區分，而兩者該有的後續動作完全不同。"""
        from apps.llm.provider import LlmResponse

        event = Event.objects.create(slug="e", title="事件")
        doc = make_doc(source, "https://t.test/1", "標題")

        class TruncatingProvider(FakeProvider):
            def _call(self, **kwargs):
                return LlmResponse(text='{"assignments": [{"doc_id":', model="x",
                                   finish_reason="length",
                                   cached_input_tokens=100, uncached_input_tokens=900,
                                   output_tokens=4096)

        assert judge_candidates(event, [doc], provider=TruncatingProvider()) == []

    def test_要求json輸出模式(self, source, db):
        """未指定 json_schema 時模型可能夾帶說明文字或 markdown，
        讓輸出更容易撞上 max_tokens——見任務 23 實測記錄。"""
        event = Event.objects.create(slug="e", title="事件")
        doc = make_doc(source, "https://t.test/1", "標題")
        provider = FakeProvider(responses=[json.dumps({"assignments": []})])
        judge_candidates(event, [doc], provider=provider)
        assert provider.calls[0]["json_schema"] is not None

    def test_呼叫記為event_linking用途(self, source, db):
        from apps.llm.models import LlmPurpose, LlmUsage

        event = Event.objects.create(slug="e", title="事件")
        doc = make_doc(source, "https://t.test/1", "標題")
        provider = FakeProvider(responses=[json.dumps({"assignments": []})])
        judge_candidates(event, [doc], provider=provider)
        assert LlmUsage.objects.filter(purpose=LlmPurpose.EVENT_LINKING).exists()


@pytest.mark.medium
class TestAutoAssign:
    def test_識別碼命中免呼叫LLM(self, source, db):
        event = Event.objects.create(slug="e", title="京華城容積案",
                                     case_numbers=["113年度金訴字第51號"])
        doc = make_doc(source, "https://t.test/1", "京華城案偵結",
                       body="內文提及京華城與相關人士",
                       case_numbers=["113年度金訴字第51號"])
        provider = FakeProvider()  # 不預先放任何回應——若被呼叫會回傳 "{}"

        results = auto_assign([event], provider=provider)

        ids = {r.document_id for r in results}
        assert doc.pk in ids
        matched = [r for r in results if r.document_id == doc.pk][0]
        assert matched.method == AssignmentMethod.IDENTIFIER

    def test_已歸屬文件不重複判定(self, source, db):
        event = Event.objects.create(slug="e", title="京華城容積案")
        doc = make_doc(source, "https://t.test/1", "京華城案偵結")
        EventDocument.objects.create(event=event, document=doc,
                                     method=AssignmentMethod.MANUAL)
        provider = FakeProvider(responses=[json.dumps({
            "assignments": [{"doc_id": doc.pk, "belongs": True, "reason": "x"}],
        })])

        results = auto_assign([event], provider=provider)

        assert doc.pk not in {r.document_id for r in results}

    def test_限制LLM呼叫次數(self, source, db):
        event = Event.objects.create(slug="e", title="京華城容積案")
        for i in range(25):
            make_doc(source, f"https://t.test/{i}", f"京華城相關報導 {i}")
        provider = FakeProvider(responses=[json.dumps({"assignments": []})] * 10)

        auto_assign([event], provider=provider, max_llm_calls=1)

        assert len(provider.calls) <= 1


@pytest.mark.medium
class TestApplyAssignments:
    def test_寫入EventDocument並更新風險分級(self, source, db):
        from apps.events.assignment import AssignmentResult

        event = Event.objects.create(slug="e", title="事件")
        doc = make_doc(source, "https://t.test/1", "標題")
        results = [AssignmentResult(document_id=doc.pk, event=event,
                                    method=AssignmentMethod.LLM, reason="測試")]

        created = apply_assignments(results)

        assert created == 1
        assert EventDocument.objects.filter(event=event, document=doc).exists()

    def test_重複套用不重複寫入(self, source, db):
        from apps.events.assignment import AssignmentResult

        event = Event.objects.create(slug="e", title="事件")
        doc = make_doc(source, "https://t.test/1", "標題")
        results = [AssignmentResult(document_id=doc.pk, event=event,
                                    method=AssignmentMethod.LLM, reason="測試")]

        apply_assignments(results)
        created_second_time = apply_assignments(results)

        assert created_second_time == 0
        assert EventDocument.objects.filter(event=event, document=doc).count() == 1
