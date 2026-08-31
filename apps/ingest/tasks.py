"""Celery 任務。

雙佇列（ADR-0007）：
  fetch    — 輕量 HTTP，高併發
  browser  — Playwright，低併發 + max-tasks-per-child

任務只負責排程、重試與逾時；實際邏輯在 services.py，
以便用 medium 測試直接驗證而不需要跑起 worker。

所有任務必須冪等：``acks_late=True`` 表示被硬殺的任務會重新入列並重跑。
這取代了既有爬蟲的 done_urls.json 續爬機制——檔案狀態在多 worker
下有競爭條件，資料庫的唯一約束沒有。
"""
from __future__ import annotations

import logging

from celery import shared_task

from apps.ingest.browser import RenderOptions
from apps.ingest.models import Source, SourceType
from apps.ingest.services import ingest_source, mark_poll_started, should_poll

#: 清單輪詢只適用於新聞來源。司法院公開查詢介面不是 feed，
#: 拿 HTML 當 RSS 解析會得到「not well-formed」然後連續失敗——
#: 那條路已被月封存檔（``check_official_records``）取代。
NEWS_POLL_TYPES = frozenset({SourceType.NEWS_RSS, SourceType.NEWS_SCRAPE})

logger = logging.getLogger(__name__)

#: 各需渲染站台的行為差異。放設定而非程式分支，新增站台只需加一筆。
BROWSER_SOURCES: dict[str, tuple[str, str, RenderOptions]] = {
    # slug: (adapter, 清單頁網址, 渲染選項)
    "udn": (
        "udn",
        "https://udn.com/news/breaknews/1",
        RenderOptions(scrolls=3, wait_for_selector="a[href*='/news/story/']"),
    ),
    "chinatimes": (
        "chinatimes",
        "https://www.chinatimes.com/realtimenews/?chdtv",
        RenderOptions(cloudflare=True, wait_for_selector="h3 a"),
    ),
}


@shared_task(
    name="apps.ingest.tasks.poll_source",
    queue="fetch",
    acks_late=True,
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def poll_source(source_id: int, adapter_slug: str = "rss") -> dict:
    """抓取單一來源。冪等——重跑只會 upsert 既有文件。

    Beat／舊 worker 若仍把司法院來源丟進這條任務（預設 rss adapter），
    會去抓 judgment.judicial.gov.tw 的 HTML 當 feed，寫下
    「not well-formed」。這裡擋住，改走月封存檔檢查。
    """
    source = Source.objects.get(pk=source_id)
    if source.type == SourceType.JUDICIAL_API:
        from apps.events.tasks import run_official_check
        return run_official_check(force=True)
    result = ingest_source(source, adapter_slug=adapter_slug)
    logger.info(
        "來源 %s：取得 %d、新增 %d、更新 %d、略過 %d%s",
        result.source_slug, result.fetched, result.created,
        result.updated, result.skipped,
        f"、錯誤 {result.error}" if result.error else "",
    )
    return {
        "source": result.source_slug,
        "fetched": result.fetched,
        "created": result.created,
        "updated": result.updated,
        "error": result.error,
    }


def dispatch_poll(source: Source):
    """依來源型別派往對應佇列。

    抽成獨立函式而非寫在 ``poll_due_sources`` 裡，是因為手動觸發的
    「立即爬取」按鈕（web UI）需要**完全相同的路由邏輯**——
    Playwright 任務的記憶體與時間特性與輕量 HTTP 差異極大，混在
    同一佇列會互相拖累（ADR-0007）。若各寫一份，兩處遲早會分岔
    （例如新增一種來源型別時只改到其中一處）。

    工商時報（ctee）雖然也是 NEWS_SCRAPE（無 RSS），但清單頁
    ``/livenews/ctee`` 實測純 HTTP 可過 Cloudflare，不必佔用
    browser 佇列。未列入 BROWSER_SOURCES 的 scrape 來源改走
    fetch + 以 slug 註冊的 adapter。

    司法院不是清單輪詢：公開查詢介面沒有 feed，任務 33 已改走
    月封存檔。按「立即爬取」應觸發官方源檢查，而不是再拿 HTML
    去餵 RSS parser。
    """
    if source.slug in BROWSER_SOURCES:
        return browser_poll_source.delay(source.pk)
    if source.type == SourceType.NEWS_SCRAPE:
        return poll_source.delay(source.pk, adapter_slug=source.slug)
    if source.type == SourceType.NEWS_RSS:
        return poll_source.delay(source.pk)
    if source.type == SourceType.JUDICIAL_API:
        from apps.events.tasks import check_official_records
        return check_official_records.delay(force=True)
    raise ValueError(f"來源型別 {source.type} 尚無輪詢路徑")


@shared_task(
    name="apps.ingest.tasks.poll_due_sources",
    queue="fetch",
    acks_late=True,
    soft_time_limit=60,
)
def poll_due_sources() -> dict:
    """派發所有到期的來源。由 Celery Beat 定期呼叫。

    只做派發不做抓取，讓單一來源的失敗或緩慢不影響其他來源。
    """
    dispatched = []
    for source in Source.objects.filter(enabled=True, type__in=NEWS_POLL_TYPES):
        if not should_poll(source):
            continue
        mark_poll_started(source)
        dispatch_poll(source)
        dispatched.append(source.slug)
    logger.info("派發 %d 個到期來源：%s", len(dispatched), ", ".join(dispatched) or "無")
    return {"dispatched": dispatched}


@shared_task(
    name="apps.ingest.tasks.browser_poll_source",
    queue="browser",
    acks_late=True,
    soft_time_limit=600,
    time_limit=720,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def browser_poll_source(source_id: int) -> dict:
    """以 Playwright 渲染後抓取清單頁。

    給沒有可用 RSS 的站台使用——實測聯合新聞網的 RSS 內容全空、
    中時新聞網的 RSS 全部 404，而它們是既有語料最大的兩個來源。

    瀏覽器由 browser_pool 管理，其生命週期綁在 Celery 子程序上；
    worker 需以 ``--max-tasks-per-child=20`` 啟動，讓子程序定期重生
    以避免瀏覽器累積劣化（ADR-0007）。

    逾時設定較 fetch 佇列寬鬆：Cloudflare 挑戰最長需 35 秒，
    加上滾動與渲染，單次可達數分鐘。
    """
    from apps.ingest.browser_pool import get_browser

    source = Source.objects.get(pk=source_id)
    if source.slug not in BROWSER_SOURCES:
        raise ValueError(f"來源 {source.slug} 未登記於 BROWSER_SOURCES")

    adapter_slug, list_url, options = BROWSER_SOURCES[source.slug]

    # 清單頁網址與 base_url 不同，暫存到 feed_url 供 ingest_source 使用
    original_feed_url = source.feed_url
    source.feed_url = list_url
    try:
        result = ingest_source(
            source,
            fetcher=get_browser(),
            adapter_slug=adapter_slug,
            render_options=options,
        )
    finally:
        source.feed_url = original_feed_url

    logger.info(
        "來源 %s（瀏覽器）：取得 %d、新增 %d、更新 %d%s",
        result.source_slug, result.fetched, result.created, result.updated,
        f"、錯誤 {result.error}" if result.error else "",
    )
    return {
        "source": result.source_slug,
        "fetched": result.fetched,
        "created": result.created,
        "updated": result.updated,
        "error": result.error,
    }


@shared_task(
    name="apps.ingest.tasks.fill_bodies",
    queue="fetch",
    acks_late=True,
    soft_time_limit=600,
    time_limit=660,
)
def fill_bodies(limit: int = 100) -> dict:
    """補齊缺內文的文件。

    全文是 L1 抽取、向量檢索、轉載歸併與時間線的共同前提——
    清單頁只給標題，實測標題僅約 21 個 bigram，不足以支撐任何後續處理。
    冪等：已有內文者直接跳過。
    """
    from apps.ingest.services import fill_missing_bodies

    result = fill_missing_bodies(limit=limit)
    return {"attempted": result.attempted, "filled": result.filled,
            "failed": result.failed}


@shared_task(
    name="apps.ingest.tasks.dedupe_recent_documents",
    queue="fetch",
    acks_late=True,
    soft_time_limit=900,
    time_limit=960,
)
def dedupe_recent_documents(hours: int = 72) -> dict:
    """歸併近期的轉載（ADR-0012）。冪等：已歸併者會被跳過。"""
    from apps.ingest.dedup import dedupe_recent

    result = dedupe_recent(hours=hours)
    return {"examined": result.examined, "linked": result.linked}


@shared_task(
    bind=True,
    name="apps.ingest.tasks.historical_backfill_source",
    queue="fetch",
    acks_late=True,
    soft_time_limit=600,
    time_limit=660,
)
def historical_backfill_source(self, source_id: int,
                               max_units: int | None = None) -> dict:
    """從古至今回補一小段，未走完則再入列。

    不走 browser 佇列：歷史清單實測皆可純 HTTP（聯合日檔、ETtoday
    日清單、報導者 API、公視分頁、中時近況分頁）。即時輪詢仍用
    Playwright 的站台，歷史路徑與即時路徑本來就不同。
    """
    from apps.ingest.historical import ingest_archive_chunk

    source = Source.objects.get(pk=source_id)
    result = ingest_archive_chunk(source, max_units=max_units)
    logger.info(
        "歷史回補 %s：取得 %d、新增 %d、更新 %d、單位 %d%s",
        result.source_slug, result.fetched, result.created, result.updated,
        result.units,
        "、完成" if result.done else "、續跑",
    )
    if result.ok and not result.done:
        historical_backfill_source.delay(source_id)
    return {
        "source": result.source_slug,
        "fetched": result.fetched,
        "created": result.created,
        "updated": result.updated,
        "units": result.units,
        "done": result.done,
        "error": result.error,
    }


def dispatch_historical(source: Source):
    """派發單一來源的歷史回補。司法來源沒有新聞清單，拒絕。"""
    from apps.ingest.archives import get_archive_spec

    spec = get_archive_spec(source.slug)
    if spec is None:
        raise ValueError(f"「{source.name}」沒有歷史清單路徑")
    if source.type not in NEWS_POLL_TYPES:
        raise ValueError(f"「{source.name}」不是新聞來源，不走歷史新聞回補")
    return historical_backfill_source.delay(source.pk)
