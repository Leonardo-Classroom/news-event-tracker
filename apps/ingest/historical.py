"""從古至今回補：走歷史清單、冪等寫入，不碰即時輪詢的健康度。

切成短任務由 Celery 自再入列，是因為日期走訪（聯合 2016 起每天一頁、
ETtoday 2012 起每天一頁）單次可達數千 HTTP，不能放進一個 worker 時限。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from django.db.models import Count, Max, Min
from django.utils import timezone

from apps.core.clock import Clock, SystemClock
from apps.ingest.archives import (
    KIND_DATE, KIND_OFFSET, KIND_PAGE, TAIPEI, ArchiveSpec, Cursor,
    advance_cursor, apply_archive_date, build_request, fetch_archive_page,
    fingerprint_urls, get_archive_spec, initial_cursor,
    parse_archive_documents,
)
from apps.ingest.fetchers import FetchError, Fetcher, HttpFetcher
from apps.ingest.models import Document, Source
from apps.ingest.services import upsert_parsed_documents

logger = logging.getLogger(__name__)

HISTORICAL_STALE = dt.timedelta(minutes=30)
#: 派工後一直沒有 cursor，多半是 worker 還在跑舊程式、不認得新任務。
HISTORICAL_QUEUED_STALE = dt.timedelta(minutes=3)
#: 連續抓不到幾頁就視為站台真的有問題、停止回補。
#: 單頁失敗要往前走（可能只是暫時性錯誤），但不能無限往前走——
#: 站台整個掛掉時，否則會一路空抓到 max_units。
MAX_CONSECUTIVE_FETCH_FAILURES = 3
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass
class ArchiveResult:
    source_slug: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    units: int = 0
    done: bool = False
    error: str = ""
    cursor: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def source_coverage() -> dict[int, dict]:
    """各來源庫內最舊／最新 ``published_at`` 與篇數。一次聚合，不當迴圈查。"""
    rows = (Document.objects.values("source_id")
            .annotate(oldest=Min("published_at"),
                      newest=Max("published_at"),
                      n=Count("id")))
    return {row["source_id"]: row for row in rows}


def historical_in_progress(source: Source, now: dt.datetime | None = None) -> bool:
    if source.historical_status != STATUS_RUNNING:
        return False
    now = now or timezone.now()
    heartbeat = source.historical_updated_at or source.historical_started_at
    if heartbeat is None:
        return True
    # 有 cursor 代表 worker 真的在跑；沒有則派工後很快就該出現。
    limit = HISTORICAL_STALE if source.historical_cursor else HISTORICAL_QUEUED_STALE
    return now - heartbeat <= limit


def cursor_label(source: Source) -> str:
    """給 UI 看的進度文字。"""
    if not source.historical_cursor:
        return ""
    cursor = Cursor.from_json(source.historical_cursor)
    if cursor.date:
        return cursor.date.isoformat()
    if cursor.offset:
        return f"offset {cursor.offset}"
    if cursor.page:
        return f"第 {cursor.page} 頁"
    return ""


def mark_historical_started(source: Source, clock: Clock | None = None) -> None:
    """每次手動觸發都從頭走——寫入是 upsert，重跑只是再掃一次。"""
    now = (clock or SystemClock()).now()
    spec = get_archive_spec(source.slug)
    source.historical_status = STATUS_RUNNING
    source.historical_cursor = ""
    source.historical_started_at = now
    source.historical_updated_at = now
    source.historical_finished_at = None
    source.historical_error = ""
    if spec and spec.earliest and source.archive_earliest_on_site is None:
        source.archive_earliest_on_site = spec.earliest
    source.save(update_fields=[
        "historical_status", "historical_cursor", "historical_started_at",
        "historical_updated_at", "historical_finished_at", "historical_error",
        "archive_earliest_on_site", "updated_at",
    ])


def _finish(source: Source, now: dt.datetime, status: str, error: str = "") -> None:
    source.historical_status = status
    source.historical_updated_at = now
    source.historical_finished_at = now
    source.historical_error = error[:2000]
    source.save(update_fields=[
        "historical_status", "historical_updated_at", "historical_finished_at",
        "historical_error", "updated_at",
    ])


def _save_cursor(source: Source, cursor: Cursor, now: dt.datetime) -> None:
    source.historical_status = STATUS_RUNNING
    source.historical_cursor = cursor.to_json()
    source.historical_updated_at = now
    source.save(update_fields=[
        "historical_status", "historical_cursor", "historical_updated_at",
        "updated_at",
    ])


def ingest_archive_chunk(
    source: Source,
    *,
    fetcher: Fetcher | None = None,
    clock: Clock | None = None,
    max_units: int | None = None,
) -> ArchiveResult:
    """處理一小段歷史清單。未走完時 ``done=False``，呼叫端再入列。"""
    spec = get_archive_spec(source.slug)
    result = ArchiveResult(source_slug=source.slug)
    if spec is None or not spec.url_template or spec.max_units == 0:
        now = (clock or SystemClock()).now()
        result.error = (spec.note if spec else "沒有歷史清單路徑")
        result.done = True
        _finish(source, now, STATUS_ERROR, result.error)
        return result

    clock = clock or SystemClock()
    owns_fetcher = fetcher is None
    fetcher = fetcher or HttpFetcher()
    now = clock.now()
    today = now.astimezone(TAIPEI).date()
    limit = max_units if max_units is not None else spec.chunk_size

    cursor = (Cursor.from_json(source.historical_cursor)
              if source.historical_cursor else initial_cursor(spec))
    if spec.kind == KIND_DATE and cursor.date is None:
        cursor = initial_cursor(spec)
    # 只在單次任務內累計：跨任務的短暫失敗不該累加成「站台掛了」。
    consecutive_failures = 0

    try:
        while result.units < limit:
            if spec.kind == KIND_DATE and cursor.date is not None and cursor.date > today:
                result.done = True
                break
            if cursor.units_done >= spec.max_units:
                result.done = True
                break

            request = build_request(spec, cursor)
            fetch_failed = False
            try:
                raw = fetch_archive_page(fetcher, spec, request)
                docs = parse_archive_documents(
                    spec, raw, base_url=source.base_url)
            except FetchError as exc:
                # 單日／單頁失敗往前走，不中斷整段回補。
                logger.warning("歷史清單 %s %s：%s", source.slug, request.url, exc)
                docs = []
                fetch_failed = True

            if fetch_failed:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FETCH_FAILURES:
                    # 記為失敗而非完成：我們並不知道是否真的走到盡頭，
                    # 標成「已完成」會謊稱這個來源的歷史已經回補完畢。
                    result.error = (
                        f"連續 {consecutive_failures} 次抓取失敗，中止回補"
                        f"（最後嘗試 {request.url}）"
                    )
                    _finish(source, clock.now(), STATUS_ERROR, result.error)
                    return result
            else:
                consecutive_failures = 0

            docs = apply_archive_date(docs, spec, cursor)
            urls = [d.url for d in docs if d.url]
            fp = fingerprint_urls(urls) if urls else ""

            # 抓取失敗與「真的沒有更多內容」必須分開：兩者都給空清單，
            # 但只有後者代表走到盡頭。混為一談會讓一次暫時性的網路錯誤
            # 把整段回補截斷，而且還標成綠色的「已完成」。
            if spec.kind in (KIND_PAGE, KIND_OFFSET) and not urls and not fetch_failed:
                result.done = True
                break
            if (spec.kind == KIND_PAGE and cursor.page > 1
                    and fp and fp == cursor.first_fp):
                result.done = True
                break

            if urls:
                created, updated, skipped = upsert_parsed_documents(
                    source, docs, now)
                result.fetched += len(docs)
                result.created += created
                result.updated += updated
                result.skipped += skipped
                if spec.kind == KIND_PAGE and cursor.page == 1 and not cursor.first_fp:
                    cursor.first_fp = fp

            cursor = advance_cursor(spec, cursor)
            result.units += 1
            result.cursor = cursor.to_json()
            _save_cursor(source, cursor, clock.now())

        if spec.kind == KIND_DATE and cursor.date is not None and cursor.date > today:
            result.done = True
        if cursor.units_done >= spec.max_units:
            result.done = True
    except Exception as exc:
        result.error = str(exc)[:2000]
        _finish(source, clock.now(), STATUS_ERROR, result.error)
        logger.exception("歷史回補 %s 失敗", source.slug)
        return result
    finally:
        if owns_fetcher and hasattr(fetcher, "close"):
            fetcher.close()

    now = clock.now()
    if result.done:
        source.historical_cursor = cursor.to_json()
        _finish(source, now, STATUS_DONE)
    else:
        _save_cursor(source, cursor, now)
    return result
