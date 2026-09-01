"""爬取任務的路由邏輯（ADR-0007 雙佇列）。

``dispatch_poll`` 從 ``poll_due_sources`` 抽出，供 web UI 的「立即
爬取」按鈕重用同一份路由判斷——兩處若各寫一份，新增來源型別時
容易只改到其中一處而分岔。這裡直接 mock 底層任務的 ``.delay``，
不需要真的連 Celery broker。
"""
from unittest.mock import patch

import pytest

from apps.ingest.models import Source, SourceType
from apps.ingest.tasks import dispatch_poll, poll_due_sources, poll_source


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

    def test_司法院來源走官方源檢查而非RSS(self, official_source):
        """judgment.judicial.gov.tw 回的是查詢頁 HTML，當 RSS 解析
        會得到 not well-formed，連續失敗計數只會一直往上加。"""
        with patch("apps.ingest.tasks.poll_source") as mocked_poll, \
             patch("apps.events.tasks.check_official_records") as mocked_check:
            dispatch_poll(official_source)
        mocked_poll.delay.assert_not_called()
        mocked_check.delay.assert_called_once_with(force=True)

    def test_poll_source收到司法院來源也不走RSS(self, official_source):
        """舊 Beat 仍可能把司法來源丟進 poll_source（預設 rss）。"""
        with patch("apps.ingest.tasks.ingest_source") as ingest, \
             patch("apps.events.tasks.run_official_check",
                   return_value={"session_expired": False}) as check:
            poll_source(official_source.pk)
        ingest.assert_not_called()
        check.assert_called_once_with(force=True)

    def test_有archive_spec的來源走spec而非adapter_slug(self, db):
        """任務 61 之後，即時輪詢改沿用歷史回補的 ArchiveSpec 抓最新，
        不再靠 RSS——即使呼叫端仍傳了 adapter_slug（dispatch_poll
        對 NEWS_SCRAPE 來源固定會傳），有 spec 的來源要以 spec 為準。
        """
        source = Source.objects.create(
            slug="ltn", name="自由時報", type=SourceType.NEWS_SCRAPE,
            base_url="https://news.ltn.com.tw",
        )
        with patch("apps.ingest.tasks.ingest_source") as mocked_ingest:
            mocked_ingest.return_value.source_slug = "ltn"
            mocked_ingest.return_value.fetched = 0
            mocked_ingest.return_value.created = 0
            mocked_ingest.return_value.updated = 0
            mocked_ingest.return_value.error = ""
            poll_source(source.pk, adapter_slug="rss")
        _, kwargs = mocked_ingest.call_args
        assert kwargs.get("archive_spec") is not None
        assert "adapter_slug" not in kwargs

    def test_沒有可用路徑的spec不當成可用(self, db):
        """spec 存在但 url_template 是空的（尚未找到可爬路徑），
        不能被誤判成「有 spec 可用」而送出空網址的請求。"""
        from apps.ingest.archives import ARCHIVE_SPECS, _spec

        source = Source.objects.create(
            slug="placeholder-source", name="佔位來源",
            type=SourceType.NEWS_RSS, base_url="https://example.test",
            feed_url="https://example.test/rss.xml",
        )
        ARCHIVE_SPECS["placeholder-source"] = _spec(
            slug="placeholder-source", kind="page", url_template="",
            note="尚未找到可用路徑", max_units=0)
        try:
            with patch("apps.ingest.tasks.ingest_source") as mocked_ingest:
                mocked_ingest.return_value.source_slug = "placeholder-source"
                mocked_ingest.return_value.fetched = 0
                mocked_ingest.return_value.created = 0
                mocked_ingest.return_value.updated = 0
                mocked_ingest.return_value.error = ""
                poll_source(source.pk)
        finally:
            del ARCHIVE_SPECS["placeholder-source"]
        _, kwargs = mocked_ingest.call_args
        assert kwargs.get("archive_spec") is None
        assert kwargs.get("adapter_slug") == "rss"

    def test_定期輪詢略過司法院來源(self, official_source):
        official_source.enabled = True
        official_source.save()
        with patch("apps.ingest.tasks.dispatch_poll") as mocked:
            poll_due_sources()
        slugs = [c.args[0].slug for c in mocked.call_args_list]
        assert official_source.slug not in slugs
