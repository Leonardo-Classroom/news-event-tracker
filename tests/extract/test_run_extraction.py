"""L1 抽取批次命令測試（任務 14）。用 ``FakeProvider`` 避免真實 API
呼叫——這個命令本身要驗證的是「選對文件」與「預算耗盡時能收手」，
不是抽取品質本身（那由 ``apps.extract.service`` 的既有機制負責）。
"""
import datetime as dt
import json

import pytest
from django.core.management import call_command

from apps.events.models import AssignmentMethod, Event, EventDocument
from apps.extract.models import Extraction
from apps.llm.provider import FakeProvider, set_provider

UTC = dt.timezone.utc


def make_doc(source, url, title="標題", body="檢方就圖利罪嫌起訴被告", **kw):
    from apps.ingest.models import Document

    return Document.objects.create(
        source=source, url=url, title=title, raw_body=body,
        content_class=source.content_class,
        published_at=dt.datetime(2026, 1, 1, tzinfo=UTC), **kw,
    )


EXTRACTION_JSON = json.dumps({
    "case_numbers": [], "defendants": [], "court": "", "prosecutor_office": "",
    "instance": "", "charges": [], "stage": "起訴", "sentence": "",
    "sentence_details": [], "is_final": False, "occurred_on": "", "summary": "檢方起訴",
})


@pytest.fixture(autouse=True)
def fake_llm():
    set_provider(FakeProvider(responses=[EXTRACTION_JSON] * 100))
    yield
    set_provider(None)


@pytest.mark.medium
class TestRunExtraction:
    def test_預設只處理已歸屬事件的文件(self, source, db):
        event = Event.objects.create(slug="e", title="事件")
        linked = make_doc(source, "https://t.test/linked")
        unlinked = make_doc(source, "https://t.test/unlinked")
        EventDocument.objects.create(event=event, document=linked,
                                     method=AssignmentMethod.MANUAL)

        call_command("run_extraction")

        assert Extraction.objects.filter(document=linked).exists()
        assert not Extraction.objects.filter(document=unlinked).exists()

    def test_all選項處理全庫相關文件(self, source, db):
        event = Event.objects.create(slug="e", title="事件")
        linked = make_doc(source, "https://t.test/linked")
        unlinked = make_doc(source, "https://t.test/unlinked", relevant=True)
        EventDocument.objects.create(event=event, document=linked,
                                     method=AssignmentMethod.MANUAL)

        call_command("run_extraction", "--all")

        assert Extraction.objects.filter(document=unlinked).exists()

    def test_event選項只處理指定事件(self, source, db):
        e1 = Event.objects.create(slug="e1", title="事件一")
        e2 = Event.objects.create(slug="e2", title="事件二")
        doc1 = make_doc(source, "https://t.test/1")
        doc2 = make_doc(source, "https://t.test/2")
        EventDocument.objects.create(event=e1, document=doc1, method=AssignmentMethod.MANUAL)
        EventDocument.objects.create(event=e2, document=doc2, method=AssignmentMethod.MANUAL)

        call_command("run_extraction", "--event", "e1")

        assert Extraction.objects.filter(document=doc1).exists()
        assert not Extraction.objects.filter(document=doc2).exists()

    def test_已抽取者不重複抽取(self, source, db):
        event = Event.objects.create(slug="e", title="事件")
        doc = make_doc(source, "https://t.test/1")
        EventDocument.objects.create(event=event, document=doc, method=AssignmentMethod.MANUAL)
        Extraction.objects.create(document=doc, schema_kind="judicial", payload={},
                                  prompt_version="x")

        call_command("run_extraction")

        assert Extraction.objects.filter(document=doc).count() == 1

    def test_無內文的文件不送抽取(self, source, db):
        event = Event.objects.create(slug="e", title="事件")
        doc = make_doc(source, "https://t.test/1", body="")
        EventDocument.objects.create(event=event, document=doc, method=AssignmentMethod.MANUAL)

        call_command("run_extraction")

        assert not Extraction.objects.filter(document=doc).exists()

    def test_limit限制處理筆數(self, source, db):
        event = Event.objects.create(slug="e", title="事件")
        for i in range(5):
            doc = make_doc(source, f"https://t.test/{i}")
            EventDocument.objects.create(event=event, document=doc,
                                         method=AssignmentMethod.MANUAL)

        call_command("run_extraction", "--limit", "2")

        assert Extraction.objects.count() == 2

    def test_預算耗盡時中止而非崩潰(self, source, db, settings):
        """任務 21 就定下的規則：每次只要超過預算上限就停下——
        這裡驗證的是「停下」，不是「詢問是否繼續」（那是使用者互動層，
        命令本身只需安全中止並回報進度）。"""
        from decimal import Decimal

        from apps.llm.budget import grant_budget
        from apps.llm.models import LlmUsage

        grant_budget(Decimal("0"))  # 已核准額度歸零，任何呼叫都會立即超額
        event = Event.objects.create(slug="e", title="事件")
        for i in range(3):
            doc = make_doc(source, f"https://t.test/budget{i}")
            EventDocument.objects.create(event=event, document=doc,
                                         method=AssignmentMethod.MANUAL)

        call_command("run_extraction")  # 不應拋出例外

        assert Extraction.objects.count() == 0
        assert not LlmUsage.objects.exists(), "超額的呼叫不該留下用量紀錄"

    def test_無待處理文件時不報錯(self, db):
        call_command("run_extraction")
