"""歷史回補的 medium 測試：真實 PostgreSQL，網路走 FakeFetcher。"""
import datetime as dt
from unittest.mock import patch

import pytest

from apps.core.clock import FixedClock
from apps.ingest.archives import TAIPEI
from apps.ingest.fetchers import FakeFetcher
from apps.ingest.historical import (
    STATUS_DONE, STATUS_ERROR, STATUS_RUNNING, historical_in_progress,
    ingest_archive_chunk, mark_historical_started,
)
from apps.ingest.models import Document, Source, SourceType

UTC = dt.timezone.utc


class TestHistoricalInProgress:
    def test_有cursor且心跳新鮮視為進行中(self):
        now = dt.datetime(2026, 8, 31, 12, tzinfo=UTC)
        src = Source(
            historical_status=STATUS_RUNNING,
            historical_cursor='{"date":"2016-07-02"}',
            historical_updated_at=now - dt.timedelta(minutes=2),
        )
        assert historical_in_progress(src, now) is True

    def test_派工後三分鐘仍無cursor視為worker沒接手(self):
        now = dt.datetime(2026, 8, 31, 12, tzinfo=UTC)
        src = Source(
            historical_status=STATUS_RUNNING,
            historical_cursor="",
            historical_started_at=now - dt.timedelta(minutes=5),
            historical_updated_at=now - dt.timedelta(minutes=5),
        )
        assert historical_in_progress(src, now) is False

UDN_DAY = """
<div class="story-list__text">
  <a href="/news/story/1/{sid}">聯合標題 {sid}</a>
</div>
"""


@pytest.fixture
def udn_source(db):
    return Source.objects.create(
        slug="udn", name="聯合新聞網", type=SourceType.NEWS_SCRAPE,
        base_url="https://udn.com",
    )


@pytest.fixture
def pts_source(db):
    return Source.objects.create(
        slug="pts", name="公視新聞", type=SourceType.NEWS_RSS,
        base_url="https://news.pts.org.tw",
        feed_url="https://news.pts.org.tw/xml/newsfeed.xml",
    )


@pytest.mark.medium
class TestIngestArchiveChunk:
    def test_日期走訪寫入並前進cursor(self, udn_source):
        clock = FixedClock(dt.datetime(2016, 7, 2, 15, tzinfo=TAIPEI))
        fetcher = FakeFetcher({
            "https://udn.com/news/archive?date=20160701": UDN_DAY.format(sid=100),
            "https://udn.com/news/archive?date=20160702": UDN_DAY.format(sid=101),
        })
        mark_historical_started(udn_source, clock=clock)
        result = ingest_archive_chunk(
            udn_source, fetcher=fetcher, clock=clock, max_units=2)

        assert result.ok and result.done
        assert result.created == 2
        assert Document.objects.filter(source=udn_source).count() == 2
        udn_source.refresh_from_db()
        assert udn_source.historical_status == STATUS_DONE
        assert udn_source.consecutive_failures == 0
        assert udn_source.last_success_at is None
        assert udn_source.archive_earliest_on_site == dt.date(2016, 7, 1)

    def test_分塊未走完保持running(self, udn_source):
        clock = FixedClock(dt.datetime(2016, 7, 5, 12, tzinfo=TAIPEI))
        fetcher = FakeFetcher({
            "https://udn.com/news/archive?date=20160701": UDN_DAY.format(sid=1),
            "https://udn.com/news/archive?date=20160702": UDN_DAY.format(sid=2),
        })
        mark_historical_started(udn_source, clock=clock)
        result = ingest_archive_chunk(
            udn_source, fetcher=fetcher, clock=clock, max_units=1)
        assert result.ok and not result.done
        udn_source.refresh_from_db()
        assert udn_source.historical_status == STATUS_RUNNING
        assert Document.objects.count() == 1

        result2 = ingest_archive_chunk(
            udn_source, fetcher=fetcher, clock=clock, max_units=1)
        assert result2.created == 1
        assert Document.objects.count() == 2

    def test_重跑同一日不產生重複文件(self, udn_source):
        clock = FixedClock(dt.datetime(2016, 7, 1, 18, tzinfo=TAIPEI))
        fetcher = FakeFetcher({
            "https://udn.com/news/archive?date=20160701": UDN_DAY.format(sid=100),
        })
        mark_historical_started(udn_source, clock=clock)
        ingest_archive_chunk(udn_source, fetcher=fetcher, clock=clock, max_units=1)
        mark_historical_started(udn_source, clock=clock)
        second = ingest_archive_chunk(
            udn_source, fetcher=fetcher, clock=clock, max_units=1)
        assert Document.objects.count() == 1
        assert second.created == 0 and second.updated == 1

    def test_分頁繞回首頁即停止(self, pts_source):
        page = """
        <a href="/article/1">德國南部飛機對撞</a>
        <a href="/article/2">第二則</a>
        """
        clock = FixedClock(dt.datetime(2026, 8, 31, 12, tzinfo=TAIPEI))
        fetcher = FakeFetcher({
            "https://news.pts.org.tw/dailynews?page=1": page,
            "https://news.pts.org.tw/dailynews?page=2": page,  # 繞回
        })
        mark_historical_started(pts_source, clock=clock)
        result = ingest_archive_chunk(
            pts_source, fetcher=fetcher, clock=clock, max_units=5)
        assert result.done
        assert Document.objects.filter(source=pts_source).count() == 2
        pts_source.refresh_from_db()
        assert pts_source.historical_status == STATUS_DONE

    def test_無歷史路徑記為error且不寫入(self, db):
        source = Source.objects.create(
            slug="judicial-search", name="司法院", type=SourceType.JUDICIAL_API,
            base_url="https://judgment.judicial.gov.tw",
        )
        clock = FixedClock(dt.datetime(2026, 8, 31, 12, tzinfo=UTC))
        result = ingest_archive_chunk(source, fetcher=FakeFetcher(), clock=clock)
        assert result.done and result.error
        source.refresh_from_db()
        assert source.historical_status == STATUS_ERROR
        assert Document.objects.count() == 0
        assert source.consecutive_failures == 0


@pytest.mark.medium
class TestDispatchHistorical:
    def test_新聞來源可派工(self, udn_source):
        from apps.ingest.tasks import dispatch_historical
        with patch("apps.ingest.tasks.historical_backfill_source") as mocked:
            dispatch_historical(udn_source)
        mocked.delay.assert_called_once_with(udn_source.pk)

    def test_司法來源拒絕(self, db):
        from apps.ingest.tasks import dispatch_historical
        source = Source.objects.create(
            slug="judicial-search", name="司法院", type=SourceType.JUDICIAL_API,
            base_url="https://judgment.judicial.gov.tw",
        )
        with pytest.raises(ValueError, match="沒有歷史清單"):
            dispatch_historical(source)
