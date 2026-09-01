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
        assert "歷史回補" in html          # 連到獨立頁面的連結
        assert "最舊" in html and "最新" in html

    def test_歷史回補獨立頁顯示執行狀態(self, admin_client, crawl_source):
        """任務拆解 61：歷史回補搬到獨立頁面，仍要看得到執行狀態。"""
        html = admin_client.get("/crawlers/history/").content.decode()
        assert crawl_source.name in html
        assert "執行狀態" in html

    def test_站台無法得知範圍時退回資料庫內最早日期(self, admin_client, db):
        """中央社的 WNewsList 沒有日期參數，站台可回溯永遠未知，
        但資料庫裡已經有的文章日期仍是有用資訊，不該只顯示「—」。"""
        from apps.ingest.models import ContentClass, Document

        source = Source.objects.create(
            slug="cna-society", name="中央社－社會", type=SourceType.NEWS_SCRAPE,
            base_url="https://www.cna.com.tw",
        )
        Document.objects.create(
            source=source, url="https://www.cna.com.tw/news/asoc/old.aspx",
            title="舊聞", content_class=ContentClass.COPYRIGHTED,
            published_at=dt.datetime(2020, 3, 5, tzinfo=UTC),
        )
        html = admin_client.get("/crawlers/history/").content.decode()
        assert "2020-03-05" in html
        assert "資料庫內最早" in html

    def test_歷史回補會派工且不跳轉(self, admin_client, db):
        """按鈕留在原頁——回傳這一列的狀態片段，不是整頁 redirect。"""
        source = Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
        )
        with patch("apps.web.views.dispatch_historical") as mocked:
            response = admin_client.post(f"/crawlers/{source.slug}/history/")
        mocked.assert_called_once()
        assert mocked.call_args[0][0].pk == source.pk
        assert response.status_code == 200
        source.refresh_from_db()
        assert source.historical_status == "running"
        data = response.json()
        assert "已派工" in data["status_html"]
        assert data["polling"] is True

    def test_歷史回補頁顯示篇數且輪詢會更新(self, admin_client, db):
        """回補進行中篇數會一直增加，只更新狀態文字看不出有沒有在動。"""
        from apps.ingest.models import ContentClass, Document

        source = Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
        )
        for i in range(3):
            Document.objects.create(
                source=source, url=f"https://udn.com/news/story/1/{i}",
                title=f"文章{i}", content_class=ContentClass.COPYRIGHTED,
            )
        assert "history-count" in admin_client.get("/crawlers/history/").content.decode()

        data = admin_client.get(f"/crawlers/{source.slug}/history/status/").json()
        assert data["doc_count"] == 3

    def test_輪詢端點回傳目前狀態(self, admin_client, db):
        """純讀取，不觸發新的回補。"""
        source = Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
            historical_status="done",
            historical_finished_at=dt.datetime.now(UTC),
        )
        with patch("apps.web.views.dispatch_historical") as mocked:
            response = admin_client.get(f"/crawlers/{source.slug}/history/status/")
        mocked.assert_not_called()
        assert response.status_code == 200
        data = response.json()
        assert "已完成" in data["status_html"]
        assert data["polling"] is False

    def test_有進度cursor時顯示回補中(self, admin_client, db):
        Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
            historical_status="running",
            historical_cursor='{"date":"2016-07-15","page":1,"offset":0}',
            historical_updated_at=dt.datetime.now(UTC),
        )
        html = admin_client.get("/crawlers/history/").content.decode()
        assert "回補中" in html
        assert "2016-07-15" in html

    def test_歷史回補要求POST(self, admin_client, crawl_source):
        with patch("apps.web.views.dispatch_historical") as mocked:
            response = admin_client.get(f"/crawlers/{crawl_source.slug}/history/")
        mocked.assert_not_called()
        assert response.status_code == 405

    def test_進行中的歷史回補不重複派工(self, admin_client, db):
        """又點一次不是錯誤——只是沒有新效果，仍回傳目前狀態片段。"""
        source = Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
            historical_status="running",
            historical_updated_at=dt.datetime.now(UTC),
        )
        with patch("apps.web.views.dispatch_historical") as mocked:
            response = admin_client.post(f"/crawlers/{source.slug}/history/")
        mocked.assert_not_called()
        assert response.status_code == 200
        assert response.json()["polling"] is True

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
