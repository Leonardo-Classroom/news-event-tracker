"""採集的核心邏輯，與 Celery 解耦。

放在 service 而非 task 裡，是為了讓它能以 medium 測試直接驗證，
不需要跑起 Celery worker。task 只負責排程、重試與逾時。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.core.clock import Clock, SystemClock
from apps.ingest.adapters import get_adapter
from apps.ingest.archives import (
    TAIPEI, ArchiveSpec, build_request, fetch_archive_page, live_cursor,
    parse_archive_documents,
)
from apps.ingest.fetchers import FetchError, Fetcher, HttpFetcher
from apps.ingest.models import Document, Source

logger = logging.getLogger(__name__)

#: 連續失敗超過此數即降頻，避免對已失效的來源持續施壓（ADR-0007）
FAILURE_THRESHOLD = 5
#: 降頻倍率
BACKOFF_MULTIPLIER = 6

#: 派工後、尚未寫入成功／失敗前，視為「進行中」。超過此時限仍沒
#: 結果，多半是 worker 死了而不是還在跑——官方源下載 270MB 實測
#: 1–2 分鐘，browser 任務 soft limit 10 分鐘，20 分鐘夠把正常執行
#: 與卡死分開。
IN_PROGRESS_STALE = dt.timedelta(minutes=20)


@dataclass
class IngestResult:
    source_slug: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def effective_interval_minutes(source: Source) -> int:
    """考慮連續失敗後的實際輪詢間隔。

    來源失效時仍以原頻率猛打，只會浪費資源並可能被封鎖。
    """
    base = source.poll_interval_minutes
    if source.consecutive_failures >= FAILURE_THRESHOLD:
        return base * BACKOFF_MULTIPLIER
    return base


def should_poll(source: Source, clock: Clock | None = None) -> bool:
    """判斷來源是否該被輪詢。時間一律走注入的 clock（測試策略接縫 3）。"""
    if not source.enabled:
        return False

    now = (clock or SystemClock()).now()
    if not source.is_within_service_window(now):
        return False

    if source.last_attempt_at is None:
        return True

    elapsed_minutes = (now - source.last_attempt_at).total_seconds() / 60
    return elapsed_minutes >= effective_interval_minutes(source)


def _fetch(fetcher, url: str, render_options=None):
    """呼叫 fetcher。BrowserFetcher 需要額外的渲染選項，HttpFetcher 不需要。

    以參數是否被接受來區分，而非 isinstance 檢查——後者會讓測試替身
    必須繼承特定類別，破壞 Fetcher 只是一個 Protocol 的設計。
    """
    if render_options is not None:
        try:
            return fetcher.get(url, options=render_options)
        except TypeError:
            pass          # 此 fetcher 不支援渲染選項，退回一般呼叫
    return fetcher.get(url)


def ingest_source(
    source: Source,
    *,
    fetcher: Fetcher | None = None,
    clock: Clock | None = None,
    adapter_slug: str = "rss",
    render_options=None,
    archive_spec: ArchiveSpec | None = None,
) -> IngestResult:
    """抓取單一來源並冪等地寫入文件。

    冪等性是硬性要求：ADR-0007 的 ``acks_late=True`` 表示被硬殺的任務
    會重新入列並重跑，因此本函式必須可以安全地重複執行。

    ``archive_spec`` 給有 ``apps.ingest.archives.ArchiveSpec`` 的來源
    （原本只用於歷史回補）：即時輪詢改抓「最新」而非「最舊」
    （見 ``live_cursor``），沿用同一份 URL／POST body／解析邏輯，
    不必為即時路徑另外維護一份 RSS 以外的請求組法。
    """
    clock = clock or SystemClock()
    fetcher = fetcher or HttpFetcher()
    result = IngestResult(source_slug=source.slug)
    now = clock.now()

    try:
        if archive_spec is not None:
            cursor = live_cursor(archive_spec, now.astimezone(TAIPEI).date())
            request = build_request(archive_spec, cursor)
            raw = fetch_archive_page(fetcher, archive_spec, request)
            parsed = parse_archive_documents(
                archive_spec, raw, base_url=source.base_url)
        else:
            url = source.feed_url or source.base_url
            response = _fetch(fetcher, url, render_options)
            if not response.ok:
                raise FetchError(f"HTTP {response.status_code}")
            parsed = get_adapter(adapter_slug).list_documents(
                response.text, base_url=source.base_url
            )
    except (FetchError, ValueError, KeyError) as exc:
        _record_failure(source, now, str(exc))
        result.error = str(exc)
        logger.warning("來源 %s 抓取失敗：%s", source.slug, exc)
        return result

    result.fetched = len(parsed)
    created, updated, skipped = upsert_parsed_documents(source, parsed, now)
    result.created = created
    result.updated = updated
    result.skipped = skipped

    _record_success(source, now)
    return result


def upsert_parsed_documents(source: Source, parsed, now) -> tuple[int, int, int]:
    """冪等寫入。清單沒給時間時不覆蓋已有的 ``published_at``——

    歷史回補與即時輪詢會重跑同一 URL。若這裡用 None 覆寫，內頁補上的
    時間會在下一次清單抓取被抹掉，最舊／最新日期就永遠不穩。
    """
    created = updated = skipped = 0
    for doc in parsed:
        if not doc.url:
            skipped += 1
            continue
        defaults = {
            "source": source,
            "title": doc.title[:512],
            "author": doc.author,
            "external_id": doc.external_id,
            "content_class": source.content_class,
            "fetched_at": now,
        }
        if doc.published_at:
            defaults["published_at"] = doc.published_at
        with transaction.atomic():
            _, was_created = Document.objects.update_or_create(
                url=doc.url, defaults=defaults)
        if was_created:
            created += 1
        else:
            updated += 1
    return created, updated, skipped


def mark_poll_started(source: Source, clock: Clock | None = None) -> None:
    """派工當下就寫 last_attempt_at，讓 UI 在 worker 接手前能顯示「進行中」。

    成功／失敗紀錄在任務結束時覆寫；若只在結束時才動 last_attempt，
    點「立即爬取」之後刷新仍會看到「從未成功」，像是沒開始。
    """
    source.last_attempt_at = (clock or SystemClock()).now()
    source.save(update_fields=["last_attempt_at", "updated_at"])


def poll_in_progress(source: Source, now: dt.datetime | None = None) -> bool:
    """已派工、尚未成功或失敗、且未超過逾時。"""
    if source.consecutive_failures:
        return False
    if source.last_attempt_at is None:
        return False
    now = now or timezone.now()
    if now - source.last_attempt_at > IN_PROGRESS_STALE:
        return False
    if source.last_success_at is None:
        return True
    return source.last_attempt_at > source.last_success_at


def _record_success(source: Source, now) -> None:
    source.last_attempt_at = now
    source.last_success_at = now
    source.consecutive_failures = 0
    source.last_error = ""
    source.save(update_fields=[
        "last_attempt_at", "last_success_at", "consecutive_failures",
        "last_error", "updated_at",
    ])


def _record_failure(source: Source, now, message: str) -> None:
    """記錄失敗。連續失敗計數是來源健康度監測的基礎——

    fixture 測試偵測不到「網站改版了」，只有這個計數能在生產環境發現。
    """
    source.last_attempt_at = now
    source.consecutive_failures += 1
    source.last_error = message[:2000]
    source.save(update_fields=[
        "last_attempt_at", "consecutive_failures", "last_error", "updated_at",
    ])
    if source.consecutive_failures == FAILURE_THRESHOLD:
        logger.error(
            "來源 %s 連續失敗 %d 次，已降頻至 %d 分鐘。可能是網站改版或被封鎖。",
            source.slug, source.consecutive_failures,
            effective_interval_minutes(source),
        )


# ---------------------------------------------------------------- 內頁全文


@dataclass
class BodyFetchResult:
    attempted: int = 0
    filled: int = 0
    failed: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def fetch_document_body(
    document: Document,
    *,
    fetcher: Fetcher | None = None,
    clock: Clock | None = None,
) -> bool:
    """抓取單一文件的內頁全文並寫回。已有內文者直接跳過（冪等）。

    回傳是否成功填入內文。
    """
    from apps.ingest.article import extract_article

    if document.raw_body:
        return True

    fetcher = fetcher or HttpFetcher()
    clock = clock or SystemClock()

    try:
        response = fetcher.get(document.url)
        if not response.ok:
            raise FetchError(f"HTTP {response.status_code}")
        article = extract_article(response.text, source_slug=document.source.slug)
    except (FetchError, ValueError) as exc:
        logger.warning("內頁抓取失敗 %s：%s", document.url, exc)
        return False

    document.raw_body = article.body
    if article.author and not document.author:
        document.author = article.author
    # 內頁的 JSON-LD 日期通常比清單頁可靠；僅在原本缺值時補上
    if article.published_at and not document.published_at:
        document.published_at = article.published_at
    # raw_body 屬於 DERIVED_SOURCES，save() 會自動重算 simhash 與 search_text
    document.save(update_fields=["raw_body", "author", "published_at"])
    return True


def fill_missing_published_at(
    *,
    limit: int = 0,
    source_slug: str = "",
    fetcher: Fetcher | None = None,
) -> BodyFetchResult:
    """回填缺 ``published_at`` 的文件。清單頁入庫只有標題，時間在內頁。

    已有內文者 ``fill_missing_bodies`` 會直接跳過，所以缺日期的列
    不會被那條路徑補上。這裡只抓時間，不重寫內文。
    """
    from apps.ingest.article import NEEDS_BROWSER, extract_published_at

    result = BodyFetchResult()
    queryset = (Document.objects.filter(published_at__isnull=True)
                .select_related("source")
                .only("id", "url", "published_at", "source__slug")
                .order_by("-fetched_at"))
    if source_slug:
        queryset = queryset.filter(source__slug=source_slug)
    if limit:
        queryset = queryset[:limit]

    owns_fetcher = fetcher is None
    fetcher = fetcher or HttpFetcher()
    try:
        for document in queryset.iterator():
            if document.source.slug in NEEDS_BROWSER:
                result.failed += 1
                continue
            result.attempted += 1
            try:
                response = fetcher.get(document.url)
                if not response.ok:
                    raise FetchError(f"HTTP {response.status_code}")
                published = extract_published_at(response.text)
                if published is None:
                    raise ValueError("內頁沒有可解析的發布時間")
            except (FetchError, ValueError) as exc:
                result.failed += 1
                result.errors.append(f"{document.url}: {exc}")
                logger.warning("補日期失敗 %s：%s", document.url, exc)
                continue
            document.published_at = published
            document.save(update_fields=["published_at"])
            result.filled += 1
    finally:
        if owns_fetcher and hasattr(fetcher, "close"):
            fetcher.close()
    return result


def fill_missing_bodies(
    *,
    limit: int = 50,
    source_slug: str = "",
    fetcher: Fetcher | None = None,
) -> BodyFetchResult:
    """批次補齊缺內文的文件。

    全文是 L1 抽取、向量檢索、SimHash 去重與時間線的共同前提——
    清單頁只給標題，實測標題僅約 21 個 bigram，SimHash 幾乎全是雜訊。
    """
    from apps.ingest.article import NEEDS_BROWSER

    queryset = (Document.objects.select_related("source")
                .filter(raw_body="")
                .exclude(source__slug__in=NEEDS_BROWSER)
                .order_by("-published_at"))
    if source_slug:
        queryset = queryset.filter(source__slug=source_slug)

    fetcher = fetcher or HttpFetcher()
    result = BodyFetchResult()

    for document in queryset[:limit]:
        result.attempted += 1
        if fetch_document_body(document, fetcher=fetcher):
            result.filled += 1
        else:
            result.failed += 1
            result.errors.append(document.url)

    logger.info("內頁補齊：嘗試 %d、成功 %d、失敗 %d",
                result.attempted, result.filled, result.failed)
    return result
