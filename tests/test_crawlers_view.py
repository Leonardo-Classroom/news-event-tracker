"""爬蟲排程頁測試——手動觸發與排程設定表單。

``dispatch_poll`` 一律 mock 掉：它呼叫 Celery 的 ``.delay()``，
即使沒有 worker 在跑也會真的把任務發布到 Redis broker，讓測試
留下不會被清除的殘留任務。這裡要驗證的是「按鈕按下去會呼叫
dispatch_poll」，不是 Celery 本身的行為。
"""
import datetime as dt
from unittest.mock import patch

import pytest

from apps.ingest.models import ExternalSession, Source, SourceType

UTC = dt.timezone.utc


@pytest.fixture
def crawl_source(db):
    return Source.objects.create(
        slug="test-source", name="測試來源", type=SourceType.NEWS_RSS,
        base_url="https://example.test", poll_interval_minutes=30,
    )


class TestCrawlersView:
    def test_列表顯示來源(self, admin_client, crawl_source):
        html = admin_client.get("/crawlers/").content.decode()
        assert "測試來源" in html
        assert "test-source" in html

    def test_立即爬取會派工(self, admin_client, crawl_source):
        with patch("apps.web.views.dispatch_poll") as mocked:
            response = admin_client.post(f"/crawlers/{crawl_source.slug}/run/")
        mocked.assert_called_once()
        assert mocked.call_args[0][0].pk == crawl_source.pk
        assert response.status_code == 302

    def test_立即爬取後顯示進行中(self, admin_client, crawl_source):
        with patch("apps.web.views.dispatch_poll"):
            admin_client.post(f"/crawlers/{crawl_source.slug}/run/")
        crawl_source.refresh_from_db()
        assert crawl_source.last_attempt_at is not None
        html = admin_client.get("/crawlers/").content.decode()
        assert "進行中" in html

    def test_立即爬取要求POST(self, admin_client, crawl_source):
        """GET 不該觸發爬取——按鈕誤被爬蟲或預抓取（prefetch）點擊
        不該產生副作用。"""
        with patch("apps.web.views.dispatch_poll") as mocked:
            response = admin_client.get(f"/crawlers/{crawl_source.slug}/run/")
        mocked.assert_not_called()
        assert response.status_code == 405

    def test_更新排程間隔(self, admin_client, crawl_source):
        admin_client.post(f"/crawlers/{crawl_source.slug}/update/", {
            "poll_interval_minutes": "15", "enabled": "on",
        })
        crawl_source.refresh_from_db()
        assert crawl_source.poll_interval_minutes == 15

    def test_更新可用時段(self, admin_client, crawl_source):
        admin_client.post(f"/crawlers/{crawl_source.slug}/update/", {
            "poll_interval_minutes": "30", "enabled": "on",
            "service_window_start_hour": "0", "service_window_end_hour": "6",
        })
        crawl_source.refresh_from_db()
        assert crawl_source.service_window_start_hour == 0
        assert crawl_source.service_window_end_hour == 6

    def test_清空時段欄位取消限制(self, admin_client, crawl_source):
        crawl_source.service_window_start_hour = 0
        crawl_source.service_window_end_hour = 6
        crawl_source.save()

        admin_client.post(f"/crawlers/{crawl_source.slug}/update/", {
            "poll_interval_minutes": "30", "enabled": "on",
        })
        crawl_source.refresh_from_db()
        assert crawl_source.service_window_start_hour is None
        assert crawl_source.service_window_end_hour is None

    def test_取消勾選啟用會停用來源(self, admin_client, crawl_source):
        admin_client.post(f"/crawlers/{crawl_source.slug}/update/", {
            "poll_interval_minutes": "30",  # 不帶 enabled 欄位模擬勾選被取消
        })
        crawl_source.refresh_from_db()
        assert crawl_source.enabled is False

    def test_無效間隔被拒絕且不寫入(self, admin_client, crawl_source):
        admin_client.post(f"/crawlers/{crawl_source.slug}/update/", {
            "poll_interval_minutes": "0", "enabled": "on",
        })
        crawl_source.refresh_from_db()
        assert crawl_source.poll_interval_minutes == 30, "無效值不該覆蓋原本設定"

    def test_無效時段被拒絕且不寫入(self, admin_client, crawl_source):
        admin_client.post(f"/crawlers/{crawl_source.slug}/update/", {
            "poll_interval_minutes": "30", "enabled": "on",
            "service_window_start_hour": "25", "service_window_end_hour": "6",
        })
        crawl_source.refresh_from_db()
        assert crawl_source.service_window_start_hour is None

    def test_列表顯示最舊最新日期與篇數(self, admin_client, crawl_source):
        from apps.ingest.models import ContentClass, Document

        Document.objects.create(
            source=crawl_source, url="https://example.test/old",
            title="舊", content_class=ContentClass.COPYRIGHTED,
            published_at=dt.datetime(2016, 7, 1, 12, tzinfo=UTC),
        )
        Document.objects.create(
            source=crawl_source, url="https://example.test/new",
            title="新", content_class=ContentClass.COPYRIGHTED,
            published_at=dt.datetime(2026, 8, 31, 4, tzinfo=UTC),
        )
        html = admin_client.get("/crawlers/").content.decode()
        assert "2016-07-01" in html
        assert "2026-08-31" in html
        assert "高級功能：從古至今回補" in html
        assert "最舊" in html and "最新" in html

    def test_歷史回補會派工(self, admin_client, db):
        source = Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
        )
        with patch("apps.web.views.dispatch_historical") as mocked:
            response = admin_client.post(f"/crawlers/{source.slug}/history/")
        mocked.assert_called_once()
        assert mocked.call_args[0][0].pk == source.pk
        assert response.status_code == 302
        source.refresh_from_db()
        assert source.historical_status == "running"
        html = admin_client.get("/crawlers/").content.decode()
        assert "已派工" in html

    def test_有進度cursor時顯示回補中(self, admin_client, db):
        Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
            historical_status="running",
            historical_cursor='{"date":"2016-07-15","page":1,"offset":0}',
            historical_updated_at=dt.datetime.now(UTC),
        )
        html = admin_client.get("/crawlers/").content.decode()
        assert "回補中" in html
        assert "2016-07-15" in html

    def test_歷史回補要求POST(self, admin_client, crawl_source):
        with patch("apps.web.views.dispatch_historical") as mocked:
            response = admin_client.get(f"/crawlers/{crawl_source.slug}/history/")
        mocked.assert_not_called()
        assert response.status_code == 405

    def test_進行中的歷史回補不重複派工(self, admin_client, db):
        source = Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
            historical_status="running",
            historical_updated_at=dt.datetime.now(UTC),
        )
        with patch("apps.web.views.dispatch_historical") as mocked:
            admin_client.post(f"/crawlers/{source.slug}/history/")
        mocked.assert_not_called()

    def test_全部回補略過司法來源(self, admin_client, db):
        Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
        )
        Source.objects.create(
            slug="judicial-search", name="司法院公開查詢",
            type=SourceType.JUDICIAL_API,
            base_url="https://judgment.judicial.gov.tw",
        )
        with patch("apps.web.views.dispatch_historical") as mocked:
            admin_client.post("/crawlers/history/all/")
        slugs = [c.args[0].slug for c in mocked.call_args_list]
        assert "udn" in slugs
        assert "judicial-search" not in slugs


class TestExternalSession:
    def test_列表頁自動建立登記過的外部session(self, admin_client, db):
        """判準見 EXTERNAL_SESSIONS 註冊表——即使資料庫是空的，
        列表頁也該自動補上這筆紀錄，不必另外跑 migration 塞資料。"""
        admin_client.get("/crawlers/")
        assert ExternalSession.objects.filter(slug="judicial-opendata").exists()

    def test_未設定時顯示尚未設定(self, admin_client, db):
        html = admin_client.get("/crawlers/").content.decode()
        assert "尚未設定" in html

    def test_儲存cookie(self, admin_client, db):
        admin_client.get("/crawlers/")  # 觸發自動建立
        response = admin_client.post(
            "/crawlers/sessions/judicial-opendata/save/",
            {"cookie_header": ".AspNetCore.Cookies=abc123; cf_clearance=xyz"},
        )
        assert response.status_code == 302
        session = ExternalSession.objects.get(slug="judicial-opendata")
        assert session.cookie_header == ".AspNetCore.Cookies=abc123; cf_clearance=xyz"
        assert session.captured_at is not None

    def test_空白cookie不寫入(self, admin_client, db):
        admin_client.get("/crawlers/")
        admin_client.post("/crawlers/sessions/judicial-opendata/save/", {"cookie_header": "  "})
        session = ExternalSession.objects.get(slug="judicial-opendata")
        assert session.cookie_header == ""

    def test_設定後顯示已設定與時間(self, admin_client, db):
        admin_client.get("/crawlers/")
        admin_client.post(
            "/crawlers/sessions/judicial-opendata/save/",
            {"cookie_header": "test=1"},
        )
        html = admin_client.get("/crawlers/").content.decode()
        assert "已設定" in html

    def test_不存在的slug回404(self, admin_client, db):
        response = admin_client.post(
            "/crawlers/sessions/nonexistent/save/", {"cookie_header": "x"})
        assert response.status_code == 404


class TestExternalSessionModel:
    def test_未設定時is_set為否(self, db):
        session = ExternalSession.objects.create(
            slug="t", name="測試", login_url="https://example.test/login")
        assert session.is_set is False

    def test_有cookie時is_set為是(self, db):
        session = ExternalSession.objects.create(
            slug="t", name="測試", login_url="https://example.test/login",
            cookie_header="a=1",
        )
        assert session.is_set is True

    def test_從未設定過視為已過期(self, db):
        session = ExternalSession.objects.create(
            slug="t", name="測試", login_url="https://example.test/login")
        assert session.likely_expired is True

    def test_剛設定不算過期(self, db):
        from django.utils import timezone

        session = ExternalSession.objects.create(
            slug="t", name="測試", login_url="https://example.test/login",
            cookie_header="a=1", captured_at=timezone.now(),
        )
        assert session.likely_expired is False

    def test_超過預期有效期視為過期(self, db):
        session = ExternalSession.objects.create(
            slug="t", name="測試", login_url="https://example.test/login",
            cookie_header="a=1", expected_valid_hours=24,
            captured_at=dt.datetime.now(UTC) - dt.timedelta(hours=25),
        )
        assert session.likely_expired is True
