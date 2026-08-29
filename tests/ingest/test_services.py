"""採集流程的測試。

排程判斷（should_poll、effective_interval_minutes）是純邏輯，
以 small 測試涵蓋；實際寫入需要真實 PostgreSQL，屬 medium。
"""
import datetime as dt

import pytest

from apps.core.clock import FixedClock
from apps.ingest.fetchers import FakeFetcher
from apps.ingest.models import Document, Source, SourceType
from apps.ingest.services import (
    BACKOFF_MULTIPLIER,
    FAILURE_THRESHOLD,
    effective_interval_minutes,
    ingest_source,
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
