"""裁判書入庫與接回事件測試（任務34、36），含任務37的核心價值驗收。

用真實裁判書全文（fixtures/html/judgment_sample.html）而非合成資料——
這正是 Scope 0 任務 2 人工穿刺驗證時取得的那份文件，已知能乾淨對應
回京華城案。
"""
import datetime as dt
from pathlib import Path

import pytest

from apps.events.models import Event, EventStatus
from apps.ingest.adapters.judicial import parse_judgment_html
from apps.ingest.judicial import ingest_judgment
from apps.ingest.models import ContentClass, SourceType

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "html" / "judgment_sample.html"
UTC = dt.timezone.utc


@pytest.fixture(scope="module")
def real_judgment_html():
    return FIXTURE.read_text(encoding="utf-8", errors="ignore")


@pytest.fixture
def judicial_source(db):
    from apps.ingest.models import Source

    return Source.objects.create(
        slug="judicial-search", name="司法院公開查詢介面",
        type=SourceType.JUDICIAL_API, base_url="https://judgment.judicial.gov.tw",
        content_class=ContentClass.PUBLIC_RECORD,
    )


@pytest.mark.medium
class TestIngestJudgment:
    def test_裁判書以公文身分入庫可全文呈現(self, real_judgment_html, judicial_source):
        """任務34：依著作權法第9條，裁判書不受著作權保護，
        content_class 須為 public_record 而非新聞的 copyrighted。"""
        result = ingest_judgment(
            real_judgment_html, url="https://judgment.judicial.gov.tw/x1",
            source=judicial_source, candidate_events=[],
        )
        assert result.created
        assert result.document.content_class == ContentClass.PUBLIC_RECORD
        assert "柯文哲" in result.document.raw_body

    def test_案號比對接回既有事件(self, real_judgment_html, judicial_source):
        """任務36：以案號精確比對優先，無需 LLM 判斷。"""
        event = Event.objects.create(
            slug="core-pacific-test", title="京華城容積案",
            status=EventStatus.ACTIVE,
            case_numbers=["113年度金訴字第51號"],
        )
        result = ingest_judgment(
            real_judgment_html, url="https://judgment.judicial.gov.tw/x2",
            source=judicial_source, candidate_events=[event],
        )
        assert result.matched_event == event
        assert event.event_documents.filter(document=result.document).exists()

    def test_案號不符者不歸屬(self, real_judgment_html, judicial_source):
        unrelated = Event.objects.create(
            slug="unrelated", title="無關事件", status=EventStatus.ACTIVE,
            case_numbers=["999年度他字第1號"],
        )
        result = ingest_judgment(
            real_judgment_html, url="https://judgment.judicial.gov.tw/x3",
            source=judicial_source, candidate_events=[unrelated],
        )
        assert result.matched_event is None

    def test_解析失敗的頁面不入庫(self, judicial_source):
        result = ingest_judgment(
            "<html>查無資料</html>", url="https://judgment.judicial.gov.tw/x4",
            source=judicial_source,
        )
        assert result.document is None
        assert result.error


@pytest.mark.medium
class TestCoreValueAcceptance:
    """任務37：dormant 事件被官方進展喚醒——整個系統存在意義的驗收點。

    規格 G4：「事件不因新聞停報而停追，dormant 事件官方源檢查頻率
    不降」。這裡示範的正是使用者最初的動機：「許多重大案件在時間久後
    大家會忘記，我要做的就是持續追蹤進度，並讓大家想起來」——新聞
    停了 138 天（模擬沉寂），裁判書一出現，事件立刻恢復追蹤。
    """

    def test_沉寂事件因裁判書喚醒為追蹤中(self, real_judgment_html, judicial_source):
        # 用固定日期而非「now - N 天」——裁判書的宣示日期（2026-03-26）
        # 是這份 fixture 固定寫死的事實，若拿相對於系統當下時間的
        # 偏移量去對照，日期算術會隨著「今天」是哪一天而改變兩者的
        # 先後關係。曾在這裡踩到：138 天前落在宣示日期之後，
        # 導致 last_progress_at 判斷「沒有更新的進展」而不更新。
        long_silent = dt.datetime(2025, 11, 1, tzinfo=UTC)
        event = Event.objects.create(
            slug="core-pacific-dormant-demo", title="京華城容積案",
            status=EventStatus.DORMANT,
            case_numbers=["113年度金訴字第51號"],
            last_progress_at=long_silent,
        )
        silent_days = (dt.date(2026, 3, 26) - long_silent.date()).days
        assert silent_days > 90, "確認情境：新聞已沉寂超過 3 個月才等到裁判書"

        result = ingest_judgment(
            real_judgment_html, url="https://judgment.judicial.gov.tw/wake",
            source=judicial_source, candidate_events=[event],
        )

        event.refresh_from_db()
        assert result.woke_dormant_event is True
        assert event.status == EventStatus.ACTIVE, "裁判書出現後事件應恢復為 active"
        assert event.last_progress_at == result.document.published_at, (
            "進展時間應更新為裁判書的宣示日期（2026-03-26），"
            "而非模糊地設為『現在』"
        )

    def test_喚醒後該篇裁判書可作為時間線節點的出處(self, real_judgment_html, judicial_source):
        """驗證與 Scope 4（M5：每個時間線節點都有可點擊出處）的銜接：
        喚醒用的這篇文件本身就能直接餵給 create_timeline_node。"""
        from apps.timeline.graph import create_timeline_node

        event = Event.objects.create(
            slug="core-pacific-node-demo", title="京華城容積案",
            status=EventStatus.DORMANT,
            case_numbers=["113年度金訴字第51號"],
            last_progress_at=dt.datetime(2025, 9, 1, tzinfo=UTC),
        )
        result = ingest_judgment(
            real_judgment_html, url="https://judgment.judicial.gov.tw/node",
            source=judicial_source, candidate_events=[event],
        )

        node = create_timeline_node(
            event=event,
            summary="臺灣臺北地方法院就京華城容積案一審宣判",
            citation_document=result.document,
            occurred_on=result.document.published_at.date(),
            is_final=False,  # 一審判決可上訴，尚未定讞
        )
        assert node.citation_document == result.document

    def test_官方源檢查時間亦被記錄(self, real_judgment_html, judicial_source):
        """與任務27的排程銜接：即使這次檢查有結果（喚醒），
        last_official_check_at 仍該更新，下次排程判斷才不會誤判。"""
        event = Event.objects.create(
            slug="core-pacific-check-demo", title="京華城容積案",
            status=EventStatus.DORMANT,
            case_numbers=["113年度金訴字第51號"],
            last_progress_at=dt.datetime(2025, 10, 1, tzinfo=UTC),
        )
        ingest_judgment(
            real_judgment_html, url="https://judgment.judicial.gov.tw/check",
            source=judicial_source, candidate_events=[event],
        )
        event.refresh_from_db()
        assert event.last_official_check_at is not None
