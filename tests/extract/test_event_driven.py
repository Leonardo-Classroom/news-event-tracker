"""L1 改為事件驅動（只抽已歸屬的文件）。

原本規劃對全部「判定為相關」的文件抽取（實測 76,900 篇約 US$145），
但消費 Extraction 的三處——風險分級、措辭檢查的 is_final、L4 時間線
——都只作用在事件的文件上。改為事件驅動後同樣的 6 個事件只需 479 篇。
"""
from unittest.mock import patch

import pytest

from apps.events.assignment import AssignmentResult, apply_assignments
from apps.events.models import Event, EventStatus
from apps.ingest.models import ContentClass, Document, Source, SourceType


@pytest.fixture
def event(db):
    return Event.objects.create(slug="e", title="京華城容積案",
                                status=EventStatus.ACTIVE)


@pytest.fixture
def docs(db):
    source = Source.objects.create(slug="s", name="來源",
                                   type=SourceType.NEWS_SCRAPE,
                                   base_url="https://e.test")
    return [Document.objects.create(
        source=source, url=f"https://e.test/{i}", title=f"報導{i}",
        raw_body="京華城容積案內容" * 40,
        content_class=ContentClass.COPYRIGHTED, relevant=True)
        for i in range(3)]


@pytest.mark.medium
class TestEventDrivenExtraction:
    def test_歸屬後自動觸發抽取(self, event, docs):
        results = [AssignmentResult(event=event, document_id=d.pk,
                                    method="identifier", reason="案號命中")
                   for d in docs]
        with patch("apps.extract.tasks.extract_for_documents.delay") as task:
            apply_assignments(results)
        task.assert_called_once()
        assert set(task.call_args[0][0]) == {d.pk for d in docs}

    def test_重複歸屬不重複觸發(self, event, docs):
        """get_or_create 已存在的不算新歸屬，不該再花一次抽取的錢。"""
        results = [AssignmentResult(event=event, document_id=docs[0].pk,
                                    method="identifier", reason="")]
        with patch("apps.extract.tasks.extract_for_documents.delay"):
            apply_assignments(results)
        with patch("apps.extract.tasks.extract_for_documents.delay") as task:
            apply_assignments(results)
        task.assert_not_called()

    def test_沒有新歸屬時不觸發(self, event):
        with patch("apps.extract.tasks.extract_for_documents.delay") as task:
            apply_assignments([])
        task.assert_not_called()

    def test_已抽過的不重複付費(self, event, docs):
        """冪等：以 prompt 版本為條件，重跑不會重複呼叫 LLM。"""
        from apps.extract.models import PROMPT_VERSION, Extraction
        from apps.extract.tasks import extract_for_documents

        Extraction.objects.create(document=docs[0], schema_kind="judicial",
                                  model="m", prompt_version=PROMPT_VERSION,
                                  succeeded=True, payload={})
        with patch("apps.extract.service.extract_document") as ext:
            ext.return_value.succeeded = True
            extract_for_documents([d.pk for d in docs])
        called = {c.args[0].pk for c in ext.call_args_list}
        assert docs[0].pk not in called
        assert len(called) == 2

    def test_預算用罄時中止而非繼續打(self, event, docs):
        """LlmError 多半是預算閘門擋下——繼續呼叫只會累積失敗紀錄。"""
        from apps.extract.tasks import extract_for_documents
        from apps.llm.provider import LlmError

        with patch("apps.extract.service.extract_document",
                   side_effect=LlmError("預算已用罄")) as ext:
            out = extract_for_documents([d.pk for d in docs])
        assert ext.call_count == 1
        assert "預算" in out["aborted"]
