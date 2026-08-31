"""事件官方源檢查的排程執行層測試（任務 27+33 的接合處）。

``due_for_official_check`` 已在任務 27 測過，``download_archive``／
``find_case_in_archive`` 已在任務 33 測過——這裡只測「接起來之後」
才會出現的問題：session 過期時該不該記錄檢查時間、是否每個封存檔
只下載一次、依法不公開的案件是否被正確跳過但仍算查過。
"""
import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from django.utils import timezone

from apps.events.models import Event, EventStatus
from apps.events.official_check import check_due_events
from apps.events.tasks import run_official_check
from apps.ingest.judicial_opendata import MonthlyArchive, SessionExpired
from apps.ingest.models import ContentClass, ExternalSession, Source, SourceType

UTC = dt.timezone.utc

ARCHIVE = MonthlyArchive(dataset_id=1, title="202606裁判書", fileset_id=100,
                        published_at=dt.datetime(2026, 8, 16, tzinfo=UTC))

TRACKED_CASE_JSON = {
    "JID": "TPDM,113,金訴,51,20260326,1", "JYEAR": "113", "JCASE": "金訴", "JNO": "51",
    "JDATE": "20260326", "JTITLE": "違反貪污治罪條例等", "JFULL": "全文",
    "JPDF": "https://data.judicial.gov.tw/opendl/JDocFile/x.pdf",
}


@pytest.fixture
def logged_in_session(db):
    return ExternalSession.objects.create(
        slug="judicial-opendata", name="司法院資料開放平台",
        login_url="https://opendata.judicial.gov.tw/member/login",
        cookie_header="valid=1",
    )


@pytest.fixture
def due_event(db):
    return Event.objects.create(
        slug="core-pacific-test", title="京華城容積案", status=EventStatus.DORMANT,
        case_numbers=["113年度金訴字第51號"],
        last_progress_at=dt.datetime(2025, 1, 1, tzinfo=UTC),
    )


@pytest.mark.medium
class TestCheckDueEvents:
    def test_沒有到期事件時不查任何東西(self, logged_in_session, db):
        with patch("apps.events.official_check.list_monthly_archives") as mocked:
            summary = check_due_events()
        mocked.assert_not_called()
        assert summary.events_checked == 0

    def test_手動force會檢查尚未到期的有案號事件(self, logged_in_session, db):
        """立即爬取不能走排程的到期判斷，否則剛查過的事件會被跳過。"""
        Event.objects.create(
            slug="just-checked", title="剛查過", status=EventStatus.ACTIVE,
            case_numbers=["113年度金訴字第51號"],
            last_official_check_at=timezone.now(),
        )
        with patch("apps.events.official_check.list_monthly_archives",
                   return_value=[ARCHIVE]) as mocked_list, \
             patch("apps.events.official_check.download_archive",
                   return_value=b"fake"), \
             patch("apps.events.official_check.find_case_in_archive",
                   return_value=None):
            skipped = check_due_events()
            forced = check_due_events(force=True)
        mocked_list.assert_called_once()  # 非 force 因無到期事件不該查
        assert skipped.events_checked == 0
        assert forced.events_checked == 1

    def test_無事件案號的事件不列入待查(self, logged_in_session, db):
        Event.objects.create(slug="no-case", title="無案號事件",
                             status=EventStatus.ACTIVE, case_numbers=[])
        with patch("apps.events.official_check.list_monthly_archives", return_value=[]):
            summary = check_due_events()
        assert summary.events_checked == 0

    def test_未設定session時仍嘗試下載公開檔(self, due_event, db):
        """公開檔不需 cookie；會員限定檔才會在 download 時拋 SessionExpired。"""
        with patch("apps.events.official_check.list_monthly_archives",
                   return_value=[ARCHIVE]), \
             patch("apps.events.official_check.download_archive",
                   side_effect=SessionExpired("需登入")) as mocked_dl:
            summary = check_due_events()
        mocked_dl.assert_called_once()
        assert summary.session_expired is True
        assert summary.events_checked == 0

    def test_命中案號成功入庫並喚醒(self, logged_in_session, due_event):
        with patch("apps.events.official_check.list_monthly_archives",
                   return_value=[ARCHIVE]), \
             patch("apps.events.official_check.download_archive", return_value=b"fake"), \
             patch("apps.events.official_check.find_case_in_archive",
                   return_value=TRACKED_CASE_JSON):
            summary = check_due_events()

        assert len(summary.results) == 1
        assert summary.results[0].matched_event == due_event
        assert summary.results[0].woke_dormant_event is True
        due_event.refresh_from_db()
        assert due_event.status == EventStatus.ACTIVE

    def test_每個封存檔只下載一次不論待查案號有幾個(self, logged_in_session, due_event):
        other_event = Event.objects.create(
            slug="other", title="另一事件", status=EventStatus.ACTIVE,
            case_numbers=["114年度訴字第1號"],
        )
        with patch("apps.events.official_check.list_monthly_archives",
                   return_value=[ARCHIVE]), \
             patch("apps.events.official_check.download_archive",
                   return_value=b"fake") as mocked_download, \
             patch("apps.events.official_check.find_case_in_archive", return_value=None):
            check_due_events()

        mocked_download.assert_called_once()

    def test_命中即不再檢查更早的封存檔(self, logged_in_session, due_event):
        older_archive = MonthlyArchive(dataset_id=2, title="202605裁判書", fileset_id=99,
                                       published_at=dt.datetime(2026, 7, 1, tzinfo=UTC))
        with patch("apps.events.official_check.list_monthly_archives",
                   return_value=[ARCHIVE, older_archive]), \
             patch("apps.events.official_check.download_archive",
                   return_value=b"fake") as mocked_download, \
             patch("apps.events.official_check.find_case_in_archive",
                   return_value=TRACKED_CASE_JSON):
            check_due_events()

        mocked_download.assert_called_once()  # 第二份封存檔不該被下載

    def test_依法不公開的案件不入庫但仍記為已查(self, logged_in_session, due_event):
        excluded_case = {**TRACKED_CASE_JSON, "JTITLE": "聲請核發通常保護令", "JCASE": "家護"}
        with patch("apps.events.official_check.list_monthly_archives",
                   return_value=[ARCHIVE]), \
             patch("apps.events.official_check.download_archive", return_value=b"fake"), \
             patch("apps.events.official_check.find_case_in_archive",
                   return_value=excluded_case):
            summary = check_due_events()

        assert summary.results == []
        due_event.refresh_from_db()
        assert due_event.last_official_check_at is not None

    def test_session過期時不記錄檢查時間(self, logged_in_session, due_event):
        """session 過期代表這批事件根本沒被真的檢查過，若仍記錄檢查
        時間，會讓事件被誤判為『最近查過、還沒到期』。"""
        with patch("apps.events.official_check.list_monthly_archives",
                   return_value=[ARCHIVE]), \
             patch("apps.events.official_check.download_archive",
                   side_effect=SessionExpired("過期")):
            summary = check_due_events()

        assert summary.session_expired is True
        due_event.refresh_from_db()
        assert due_event.last_official_check_at is None

    def test_dry_run不實際入庫也不留下任何副作用(self, logged_in_session, due_event):
        """dry-run 的語意是『完全不改動狀態』，包含排程用的檢查時間——
        否則預覽一次就會讓下次真正的排程誤判為『剛查過』而延後。"""
        with patch("apps.events.official_check.list_monthly_archives",
                   return_value=[ARCHIVE]), \
             patch("apps.events.official_check.download_archive", return_value=b"fake"), \
             patch("apps.events.official_check.find_case_in_archive",
                   return_value=TRACKED_CASE_JSON):
            summary = check_due_events(dry_run=True)

        assert summary.results == []
        from apps.ingest.models import Document
        assert not Document.objects.exists()
        due_event.refresh_from_db()
        assert due_event.last_official_check_at is None

    def test_找不到任何符合的案件不入庫(self, logged_in_session, due_event):
        with patch("apps.events.official_check.list_monthly_archives",
                   return_value=[ARCHIVE]), \
             patch("apps.events.official_check.download_archive", return_value=b"fake"), \
             patch("apps.events.official_check.find_case_in_archive", return_value=None):
            summary = check_due_events()
        assert summary.results == []


@pytest.mark.medium
class TestRunOfficialCheck:
    def test_完成後爬蟲頁的司法來源標記成功(self, db):
        source = Source.objects.create(
            slug="judicial-search", name="司法院公開查詢介面",
            type=SourceType.JUDICIAL_API,
            base_url="https://judgment.judicial.gov.tw",
            content_class=ContentClass.PUBLIC_RECORD,
        )
        summary = MagicMock(
            session_expired=False, events_checked=0,
            archives_downloaded=0, results=[],
        )
        with patch("apps.events.official_check.check_due_events", return_value=summary) as mocked:
            run_official_check(force=True)
        mocked.assert_called_once_with(force=True)
        source.refresh_from_db()
        assert source.last_success_at is not None
        assert source.consecutive_failures == 0
        assert source.last_error == ""
