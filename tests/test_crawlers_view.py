"""爬蟲排程頁測試——手動觸發與排程設定表單。

``dispatch_poll`` 一律 mock 掉：它呼叫 Celery 的 ``.delay()``，
即使沒有 worker 在跑也會真的把任務發布到 Redis broker，讓測試
留下不會被清除的殘留任務。這裡要驗證的是「按鈕按下去會呼叫
dispatch_poll」，不是 Celery 本身的行為。
"""
from unittest.mock import patch

import pytest

from apps.ingest.models import Source, SourceType


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
