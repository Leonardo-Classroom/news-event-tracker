"""爬取任務的路由邏輯（ADR-0007 雙佇列）。

``dispatch_poll`` 從 ``poll_due_sources`` 抽出，供 web UI 的「立即
爬取」按鈕重用同一份路由判斷——兩處若各寫一份，新增來源型別時
容易只改到其中一處而分岔。這裡直接 mock 底層任務的 ``.delay``，
不需要真的連 Celery broker。
"""
from unittest.mock import patch

import pytest

from apps.ingest.models import Source, SourceType
from apps.ingest.tasks import dispatch_poll


@pytest.mark.medium
class TestDispatchPoll:
    def test_一般新聞來源走fetch佇列的poll_source(self, db):
        source = Source.objects.create(
            slug="rss-source", name="RSS 來源", type=SourceType.NEWS_RSS,
            base_url="https://example.test",
        )
        with patch("apps.ingest.tasks.poll_source") as mocked_poll, \
             patch("apps.ingest.tasks.browser_poll_source") as mocked_browser:
            dispatch_poll(source)
        mocked_poll.delay.assert_called_once_with(source.pk)
        mocked_browser.delay.assert_not_called()

    def test_無RSS的爬蟲來源走browser佇列(self, db):
        """實測聯合新聞網 RSS 內容全空、中時新聞網 RSS 全部 404，
        這兩家是既有語料最大的來源，必須走 Playwright（ADR-0007）。"""
        source = Source.objects.create(
            slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
            base_url="https://udn.com",
        )
        with patch("apps.ingest.tasks.poll_source") as mocked_poll, \
             patch("apps.ingest.tasks.browser_poll_source") as mocked_browser:
            dispatch_poll(source)
        mocked_browser.delay.assert_called_once_with(source.pk)
        mocked_poll.delay.assert_not_called()

    def test_工商時報走fetch佇列(self, db):
        """/livenews 首頁被 Cloudflare 擋，分類頁可純 HTTP，不必佔 browser 佇列。"""
        source = Source.objects.create(
            slug="ctee", name="工商時報", type=SourceType.NEWS_SCRAPE,
            base_url="https://www.ctee.com.tw",
            feed_url="https://www.ctee.com.tw/livenews/ctee",
        )
        with patch("apps.ingest.tasks.poll_source") as mocked_poll, \
             patch("apps.ingest.tasks.browser_poll_source") as mocked_browser:
            dispatch_poll(source)
        mocked_poll.delay.assert_called_once_with(source.pk, adapter_slug="ctee")
        mocked_browser.delay.assert_not_called()

    def test_司法院api型別走fetch佇列(self, official_source):
        with patch("apps.ingest.tasks.poll_source") as mocked_poll:
            dispatch_poll(official_source)
        mocked_poll.delay.assert_called_once_with(official_source.pk)
