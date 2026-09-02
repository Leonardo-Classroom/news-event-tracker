"""採集流程的測試。

排程判斷（should_poll、effective_interval_minutes）是純邏輯，
以 small 測試涵蓋；實際寫入需要真實 PostgreSQL，屬 medium。
"""
import datetime as dt
import json

import pytest

from apps.core.clock import FixedClock
from apps.ingest.archives import get_archive_spec, tagged_post_url
from apps.ingest.fetchers import FakeFetcher
from apps.ingest.models import Document, Source, SourceType
from apps.ingest.services import (
    BACKOFF_MULTIPLIER,
    FAILURE_THRESHOLD,
    IN_PROGRESS_STALE,
    effective_interval_minutes,
    fill_missing_published_at,
    ingest_source,
    poll_in_progress,
    should_poll,
)
from tests.ingest.test_rss_adapter import RSS_SAMPLE

UTC = dt.timezone.utc


def make_source(**kw):
    """未存檔的 Source，供 small 測試使用（不碰資料庫）。"""
    defaults = dict(
        slug="s", name="來源", type=SourceType.NEWS_RSS,
        base_url="https://example.test", poll_interval_minutes=60,
        enabled=True, consecutive_failures=0,
    )
    return Source(**{**defaults, **kw})


class TestEffectiveInterval:
    def test_健康時使用原間隔(self):
        assert effective_interval_minutes(make_source(poll_interval_minutes=30)) == 30

    def test_連續失敗達門檻後降頻(self):
        src = make_source(poll_interval_minutes=30, consecutive_failures=FAILURE_THRESHOLD)
        assert effective_interval_minutes(src) == 30 * BACKOFF_MULTIPLIER

    def test_未達門檻不降頻(self):
        src = make_source(poll_interval_minutes=30,
                          consecutive_failures=FAILURE_THRESHOLD - 1)
        assert effective_interval_minutes(src) == 30


class TestPollInProgress:
    def test_剛派工且尚未成功視為進行中(self):
        now = dt.datetime(2026, 8, 31, 12, tzinfo=UTC)
        src = make_source(last_attempt_at=now, last_success_at=None)
        assert poll_in_progress(src, now) is True

    def test_成功後不再顯示進行中(self):
        now = dt.datetime(2026, 8, 31, 12, tzinfo=UTC)
        src = make_source(last_attempt_at=now, last_success_at=now)
        assert poll_in_progress(src, now) is False

    def test_失敗不顯示進行中(self):
        now = dt.datetime(2026, 8, 31, 12, tzinfo=UTC)
        src = make_source(last_attempt_at=now, last_success_at=None,
                          consecutive_failures=1)
        assert poll_in_progress(src, now) is False

    def test_逾時視為不是進行中(self):
        now = dt.datetime(2026, 8, 31, 12, tzinfo=UTC)
        src = make_source(last_attempt_at=now - IN_PROGRESS_STALE - dt.timedelta(minutes=1),
                          last_success_at=None)
        assert poll_in_progress(src, now) is False


class TestShouldPoll:
    """時間一律走注入的 clock——不需要 sleep，也不需要等真實時間。"""

    def test_從未抓取過則立即抓取(self):
        clock = FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=UTC))
        assert should_poll(make_source(last_attempt_at=None), clock) is True

    def test_未達間隔則不抓(self):
        clock = FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=UTC))
        src = make_source(last_attempt_at=dt.datetime(2026, 3, 5, 11, 30, tzinfo=UTC))
        assert should_poll(src, clock) is False        # 才過 30 分，間隔 60

    def test_達到間隔則抓取(self):
        clock = FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=UTC))
        src = make_source(last_attempt_at=dt.datetime(2026, 3, 5, 11, 0, tzinfo=UTC))
        assert should_poll(src, clock) is True

    def test_停用的來源不抓(self):
        clock = FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=UTC))
        assert should_poll(make_source(enabled=False), clock) is False

    def test_失敗降頻後需等更久(self):
        clock = FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=UTC))
        src = make_source(
            last_attempt_at=dt.datetime(2026, 3, 5, 11, 0, tzinfo=UTC),
            consecutive_failures=FAILURE_THRESHOLD,
        )
        assert should_poll(src, clock) is False        # 需 360 分鐘
        clock.advance(hours=6)
        assert should_poll(src, clock) is True

    def test_服務時段外不抓(self):
        """司法院 API 僅 00:00–06:00 開放（規格 R6）。"""
        src = make_source(service_window_start_hour=0, service_window_end_hour=6)
        tz = dt.timezone(dt.timedelta(hours=8))        # Asia/Taipei
        assert should_poll(src, FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=tz))) is False
        assert should_poll(src, FixedClock(dt.datetime(2026, 3, 5, 3, tzinfo=tz))) is True


@pytest.mark.medium
class TestIngestSource:
    def test_寫入文件並更新健康狀態(self, source):
        fetcher = FakeFetcher()
        fetcher.register(source.base_url, RSS_SAMPLE)
        clock = FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=UTC))

        result = ingest_source(source, fetcher=fetcher, clock=clock)

        assert result.ok
        assert result.fetched == 2 and result.created == 2
        assert Document.objects.count() == 2
        source.refresh_from_db()
        assert source.consecutive_failures == 0
        assert source.last_success_at == clock.now()

    def test_重跑為冪等(self, source):
        """acks_late 下被硬殺的任務會重跑，不得產生重複文件。"""
        fetcher = FakeFetcher({source.base_url: RSS_SAMPLE})
        clock = FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=UTC))

        first = ingest_source(source, fetcher=fetcher, clock=clock)
        second = ingest_source(source, fetcher=fetcher, clock=clock)

        assert first.created == 2
        assert second.created == 0 and second.updated == 2
        assert Document.objects.count() == 2

    def test_文件繼承來源的_content_class(self, source):
        fetcher = FakeFetcher({source.base_url: RSS_SAMPLE})
        ingest_source(source, fetcher=fetcher,
                      clock=FixedClock(dt.datetime(2026, 3, 5, tzinfo=UTC)))
        assert Document.objects.news().count() == 2

    def test_抓取失敗時累計失敗數且不寫入(self, source):
        fetcher = FakeFetcher()                        # 未登記任何 URL → 拋 FetchError
        clock = FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=UTC))

        result = ingest_source(source, fetcher=fetcher, clock=clock)

        assert not result.ok
        assert Document.objects.count() == 0
        source.refresh_from_db()
        assert source.consecutive_failures == 1
        assert source.last_error
        assert source.last_success_at is None

    def test_解析失敗也算失敗(self, source):
        """回傳 200 但內容不是 RSS——網站改版的典型徵狀。"""
        fetcher = FakeFetcher({source.base_url: "<html>維護中</html>"})
        result = ingest_source(source, fetcher=fetcher,
                               clock=FixedClock(dt.datetime(2026, 3, 5, tzinfo=UTC)))
        assert not result.ok
        source.refresh_from_db()
        assert source.consecutive_failures == 1

    def test_成功後失敗計數歸零(self, source):
        clock = FixedClock(dt.datetime(2026, 3, 5, 12, tzinfo=UTC))
        ingest_source(source, fetcher=FakeFetcher(), clock=clock)      # 失敗
        source.refresh_from_db()
        assert source.consecutive_failures == 1

        ingest_source(source, fetcher=FakeFetcher({source.base_url: RSS_SAMPLE}),
                      clock=clock)                                     # 成功
        source.refresh_from_db()
        assert source.consecutive_failures == 0

    def test_優先使用_feed_url(self, source):
        source.feed_url = "https://example.test/rss.xml"
        source.save()
        fetcher = FakeFetcher({source.feed_url: RSS_SAMPLE})
        result = ingest_source(source, fetcher=fetcher,
                               clock=FixedClock(dt.datetime(2026, 3, 5, tzinfo=UTC)))
        assert result.ok
        assert fetcher.calls == [source.feed_url]

    def test_清單沒給時間不覆蓋已有published_at(self, source):
        """歷史回補與即時輪詢會重跑同一 URL，None 不可把內頁補上的時間抹掉。"""
        from apps.ingest.models import ContentClass, Document

        kept = dt.datetime(2016, 7, 1, 8, 30, tzinfo=UTC)
        Document.objects.create(
            source=source, url="https://example.test/news/nodate",
            title="舊", content_class=ContentClass.COPYRIGHTED,
            published_at=kept,
        )
        rss = """<?xml version="1.0"?><rss version="2.0"><channel>
          <item><title>新標題</title>
          <link>https://example.test/news/nodate</link></item>
        </channel></rss>"""
        ingest_source(
            source, fetcher=FakeFetcher({source.base_url: rss}),
            clock=FixedClock(dt.datetime(2026, 8, 31, tzinfo=UTC)),
        )
        doc = Document.objects.get(url="https://example.test/news/nodate")
        assert doc.published_at == kept
        assert doc.title == "新標題"


@pytest.mark.medium
class TestIngestSourceArchiveSpec:
    """有 ``ArchiveSpec`` 的來源：即時輪詢與歷史回補共用同一份規格，
    但即時輪詢要抓「最新」（見 ``live_cursor``），不是歷史回補的
    起點。這裡驗證即時路徑真的算出最新頁／今天，而不是不小心
    沿用了 ``initial_cursor`` 的最舊起點。"""

    def test_page走訪抓第一頁而非最舊(self, source):
        spec = get_archive_spec("cna-society")
        body = json.dumps({"ResultData": {"Items": [
            {"PageUrl": "/news/asoc/202608310001.aspx", "HeadLine": "測試新聞",
             "CreateTime": "2026/08/31 12:00:00", "Id": "202608310001"},
        ]}})
        url = tagged_post_url(spec.url_template, {
            "action": "0", "category": "asoc", "pageidx": 1,
            "pagesize": spec.pagesize,
        })
        fetcher = FakeFetcher()
        fetcher.register(url, body)
        clock = FixedClock(dt.datetime(2026, 8, 31, 12, tzinfo=UTC))

        result = ingest_source(source, fetcher=fetcher, clock=clock,
                               archive_spec=spec)

        assert result.ok and result.created == 1
        assert Document.objects.get().title == "測試新聞"

    def test_date走訪抓今天而非站台最早日期(self, source):
        spec = get_archive_spec("ettoday")
        # UTC 03:00 = 台北 11:00，同一天——若誤用 UTC 日期會在
        # 台北午夜前後跨日算錯。
        clock = FixedClock(dt.datetime(2026, 8, 31, 3, tzinfo=UTC))
        url = "https://www.ettoday.net/news/news-list-2026-08-31-0.htm"
        html = ('<div class="part_list_2"><h3>'
                '<a href="https://www.ettoday.net/news/123456.htm">測試</a>'
                '</h3></div>')
        fetcher = FakeFetcher({url: html})

        result = ingest_source(source, fetcher=fetcher, clock=clock,
                               archive_spec=spec)

        assert result.ok
        assert fetcher.calls == [url]      # 不是 spec.earliest 的 2012 年

    def test_失敗仍記為來源失敗(self, source):
        spec = get_archive_spec("ltn")
        result = ingest_source(
            source, fetcher=FakeFetcher(),
            clock=FixedClock(dt.datetime(2026, 8, 31, tzinfo=UTC)),
            archive_spec=spec)

        assert not result.ok
        source.refresh_from_db()
        assert source.consecutive_failures == 1

    def test_分頁走訪遇到已入庫文章就停止翻頁(self, source):
        """兩次輪詢間發布量若超過單頁筆數，第一頁看不到的文章不能
        永遠漏掉——要主動往回翻，翻到看過的為止。"""
        spec = get_archive_spec("ltn")
        Document.objects.create(
            source=source, url="https://news.ltn.com.tw/news/breakingnews/old",
            title="舊聞", content_class="copyrighted",
        )
        page1_url = "https://news.ltn.com.tw/ajax/breakingnews/all/1"
        page2_url = "https://news.ltn.com.tw/ajax/breakingnews/all/2"
        page3_url = "https://news.ltn.com.tw/ajax/breakingnews/all/3"
        page1 = json.dumps({"data": {"1": {
            "url": "https://news.ltn.com.tw/news/breakingnews/new1",
            "title": "新聞1", "time": "2026/08/31 12:00", "no": "1"}}})
        page2 = json.dumps({"data": {"1": {
            "url": "https://news.ltn.com.tw/news/breakingnews/old",
            "title": "舊聞", "time": "2026/08/31 11:00", "no": "old"}}})
        fetcher = FakeFetcher({page1_url: page1, page2_url: page2,
                               page3_url: "不該被抓到"})
        clock = FixedClock(dt.datetime(2026, 8, 31, 12, tzinfo=UTC))

        result = ingest_source(source, fetcher=fetcher, clock=clock,
                               archive_spec=spec)

        assert result.ok
        assert page3_url not in fetcher.calls   # 第 2 頁遇到舊聞，第 3 頁不該被抓
        assert Document.objects.filter(
            url="https://news.ltn.com.tw/news/breakingnews/new1").exists()

    def test_分頁走訪整頁都是新的才繼續翻頁(self, source):
        spec = get_archive_spec("ltn")
        page1_url = "https://news.ltn.com.tw/ajax/breakingnews/all/1"
        page2_url = "https://news.ltn.com.tw/ajax/breakingnews/all/2"
        page1 = json.dumps({"data": {"1": {
            "url": "https://news.ltn.com.tw/news/breakingnews/a",
            "title": "A", "time": "2026/08/31 12:00", "no": "a"}}})
        page2 = json.dumps({"data": {"1": {
            "url": "https://news.ltn.com.tw/news/breakingnews/b",
            "title": "B", "time": "2026/08/31 11:50", "no": "b"}}})
        fetcher = FakeFetcher({page1_url: page1, page2_url: page2})
        clock = FixedClock(dt.datetime(2026, 8, 31, 12, tzinfo=UTC))

        result = ingest_source(source, fetcher=fetcher, clock=clock,
                               archive_spec=spec)

        assert result.ok
        assert page2_url in fetcher.calls
        assert Document.objects.count() == 2

    def test_分頁走訪有安全頁數上限(self, source):
        """來源第一次輪詢、資料庫完全沒有既有 URL 可比對時，不能
        無限翻頁把整站當歷史來爬。"""
        spec = get_archive_spec("ltn")
        fetcher = FakeFetcher()
        for page in range(1, 8):
            url = f"https://news.ltn.com.tw/ajax/breakingnews/all/{page}"
            fetcher.register(url, json.dumps({"data": {"1": {
                "url": f"https://news.ltn.com.tw/news/breakingnews/p{page}",
                "title": f"p{page}", "time": "2026/08/31 12:00", "no": str(page),
            }}}))
        clock = FixedClock(dt.datetime(2026, 8, 31, 12, tzinfo=UTC))

        result = ingest_source(source, fetcher=fetcher, clock=clock,
                               archive_spec=spec)

        assert result.ok
        assert len(fetcher.calls) == 5          # LIVE_WALK_MAX_PAGES
        assert Document.objects.count() == 5

    def test_日期走訪不翻頁(self, source):
        """ETtoday／公視這類日期走訪每次都整頁重抓當天全部清單，
        不是固定筆數快照，沒有「翻頁」的概念。"""
        spec = get_archive_spec("ettoday")
        clock = FixedClock(dt.datetime(2026, 8, 31, 3, tzinfo=UTC))
        url = "https://www.ettoday.net/news/news-list-2026-08-31-0.htm"
        html = ('<div class="part_list_2"><h3>'
                '<a href="https://www.ettoday.net/news/123456.htm">測試</a>'
                '</h3></div>')
        fetcher = FakeFetcher({url: html})

        result = ingest_source(source, fetcher=fetcher, clock=clock,
                               archive_spec=spec)

        assert result.ok
        assert fetcher.calls == [url]


@pytest.mark.medium
class TestFillMissingPublishedAt:
    def test_從內頁meta補上日期(self, source):
        from apps.ingest.models import ContentClass, Document

        doc = Document.objects.create(
            source=source, url="https://example.test/no-date",
            title="缺日期", raw_body="已有內文" * 20,
            content_class=ContentClass.COPYRIGHTED,
        )
        html = (
            "<html><head>"
            '<meta property="article:published_time" '
            'content="2026-08-31T08:35:27+08:00">'
            "</head></html>"
        )
        result = fill_missing_published_at(
            fetcher=FakeFetcher({doc.url: html}))
        doc.refresh_from_db()
        assert result.filled == 1
        assert result.failed == 0
        assert doc.published_at == dt.datetime(
            2026, 8, 31, 0, 35, 27, tzinfo=dt.timezone.utc)


@pytest.mark.medium
class TestBodyAttempts:
    """抓不到的文件必須退出佇列，否則補內文永遠卡在隊首那批死文件。

    實測背景：取件順序固定為「缺內文、依 -published_at」，失敗的不會
    離開，結果同一個網址被重抓了 430 次，後面的文件永遠輪不到。
    """

    def test_失敗會累計次數(self, source):
        from apps.ingest.models import ContentClass, Document
        from apps.ingest.services import fetch_document_body

        doc = Document.objects.create(
            source=source, url="https://example.test/dead",
            title="抓不到", content_class=ContentClass.COPYRIGHTED,
        )
        assert fetch_document_body(doc, fetcher=FakeFetcher()) is False
        doc.refresh_from_db()
        assert doc.body_attempts == 1

    def test_達上限後不再被取出(self, source):
        from apps.ingest.models import ContentClass, Document
        from apps.ingest.services import MAX_BODY_ATTEMPTS, fill_missing_bodies

        Document.objects.create(
            source=source, url="https://example.test/dead",
            title="抓不到", content_class=ContentClass.COPYRIGHTED,
            body_attempts=MAX_BODY_ATTEMPTS,
        )
        result = fill_missing_bodies(limit=10, fetcher=FakeFetcher())
        assert result.attempted == 0

    def test_未達上限仍會重試(self, source):
        from apps.ingest.models import ContentClass, Document
        from apps.ingest.services import MAX_BODY_ATTEMPTS, fill_missing_bodies

        Document.objects.create(
            source=source, url="https://example.test/maybe",
            title="暫時失敗", content_class=ContentClass.COPYRIGHTED,
            body_attempts=MAX_BODY_ATTEMPTS - 1,
        )
        result = fill_missing_bodies(limit=10, fetcher=FakeFetcher())
        assert result.attempted == 1

    def test_成功後不留下失敗計數(self, source):
        from apps.ingest.models import ContentClass, Document
        from apps.ingest.services import fetch_document_body

        doc = Document.objects.create(
            source=source, url="https://example.test/ok",
            title="正常", content_class=ContentClass.COPYRIGHTED,
        )
        html = ('<html><head><script type="application/ld+json">'
                '{"@type":"NewsArticle","articleBody":"' + "內文" * 60 + '"}'
                '</script></head><body></body></html>')
        assert fetch_document_body(doc, fetcher=FakeFetcher({doc.url: html})) is True
        doc.refresh_from_db()
        assert doc.raw_body and doc.body_attempts == 0
