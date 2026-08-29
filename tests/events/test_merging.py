"""事件合併測試（任務 26）。需要真實資料庫——牽涉多張表的交易一致性。"""
import datetime as dt

import pytest

from apps.events.merging import merge_events
from apps.events.models import (
    AssignmentMethod, Event, EventAlias, EventDocument, EventMergeLog,
)

UTC = dt.timezone.utc


@pytest.fixture
def two_events(db):
    a = Event.objects.create(slug="event-a", title="事件甲",
                             core_terms=["柯文哲"], case_numbers=["113年度金訴字第51號"])
    b = Event.objects.create(slug="event-b", title="事件乙",
                             core_terms=["沈慶京"], case_numbers=[])
    return a, b


@pytest.fixture
def make_doc(source):
    def _make(url):
        from apps.ingest.models import Document
        return Document.objects.create(
            source=source, url=url, title=url, raw_body="內文",
            content_class=source.content_class,
        )
    return _make


@pytest.mark.medium
class TestMergeEvents:
    def test_不能併入自己(self, two_events):
        a, _ = two_events
        with pytest.raises(ValueError):
            merge_events(a, a)

    def test_文件轉移到目標事件(self, two_events, make_doc):
        source, target = two_events
        doc = make_doc("https://t.test/1")
        EventDocument.objects.create(event=source, document=doc, method=AssignmentMethod.MANUAL)

        merge_events(source, target)

        assert EventDocument.objects.filter(event=target, document=doc).exists()
        assert not EventDocument.objects.filter(event_id=source.pk).exists()

    def test_重複歸屬的文件不產生重複列(self, two_events, make_doc):
        """同一篇文件若兩邊都已歸屬，合併後只保留一筆，不因合併而重複。"""
        source, target = two_events
        doc = make_doc("https://t.test/dup")
        EventDocument.objects.create(event=source, document=doc, method=AssignmentMethod.MANUAL)
        EventDocument.objects.create(event=target, document=doc, method=AssignmentMethod.LLM)

        merge_events(source, target)

        assert EventDocument.objects.filter(event=target, document=doc).count() == 1

    def test_來源slug成為目標事件的別名供301導向(self, two_events):
        source, target = two_events
        merge_events(source, target)

        alias = EventAlias.objects.get(event=target, name="event-a")
        assert alias.is_former_slug

    def test_來源既有別名一併轉移(self, two_events):
        source, target = two_events
        EventAlias.objects.create(event=source, name="舊俗稱")
        merge_events(source, target)
        assert EventAlias.objects.filter(event=target, name="舊俗稱").exists()

    def test_別名撞名時保留目標既有的(self, two_events):
        source, target = two_events
        EventAlias.objects.create(event=source, name="共用別名")
        EventAlias.objects.create(event=target, name="共用別名")
        merge_events(source, target)   # 不應因 unique_together 衝突而失敗
        assert EventAlias.objects.filter(event=target, name="共用別名").count() == 1

    def test_核心詞與案號取聯集(self, two_events):
        source, target = two_events
        merge_events(source, target)
        target.refresh_from_db()
        assert set(target.core_terms) == {"柯文哲", "沈慶京"}
        assert target.case_numbers == ["113年度金訴字第51號"]

    def test_進展時間取較新者(self, two_events):
        source, target = two_events
        source.last_progress_at = dt.datetime(2026, 6, 1, tzinfo=UTC)
        source.save(update_fields=["last_progress_at"])
        target.last_progress_at = dt.datetime(2026, 1, 1, tzinfo=UTC)
        target.save(update_fields=["last_progress_at"])

        merge_events(source, target)
        target.refresh_from_db()
        assert target.last_progress_at == dt.datetime(2026, 6, 1, tzinfo=UTC)

    def test_來源事件被刪除(self, two_events):
        source, target = two_events
        source_id = source.pk
        merge_events(source, target)
        assert not Event.objects.filter(pk=source_id).exists()

    def test_合併紀錄可回溯即使來源已刪除(self, two_events, make_doc):
        source, target = two_events
        doc = make_doc("https://t.test/2")
        EventDocument.objects.create(event=source, document=doc, method=AssignmentMethod.MANUAL)

        log = merge_events(source, target, reason="同案不同時期報導未被歸屬")

        log.refresh_from_db()
        assert log.source_event is None, "來源事件已刪除，外鍵應為 NULL"
        assert log.source_slug == "event-a"
        assert log.source_title == "事件甲"
        assert log.document_count == 1
        assert log.reason == "同案不同時期報導未被歸屬"

    def test_目標事件刪除時合併紀錄一併刪除(self, two_events):
        """target_event 用 CASCADE：目標事件都不存在了，這筆合併紀錄
        也失去意義——與 source_event 用 SET_NULL 是不同的取捨，
        因為 target 是「現在還活著、可以被連到」的事件。"""
        source, target = two_events
        log = merge_events(source, target)
        target.delete()
        assert not EventMergeLog.objects.filter(pk=log.pk).exists()
