"""Celery 應用設定。

雙佇列（ADR-0007）：
  fetch   — RSS/sitemap 輪詢、官方 API、靜態頁抓取。高併發、輕量。
  browser — Playwright 抓取。低併發、重記憶體，且需 max-tasks-per-child
            定期重生子程序以復刻既有爬蟲「每記者重開瀏覽器」的防劣化設計。
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("newstrack")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.task_routes = {
    "apps.ingest.tasks.browser_*": {"queue": "browser"},
}

# ---------------------------------------------------------------- 排程
# Beat 只派發，不執行——單一來源的失敗或緩慢不應影響其他來源。
# 實際的輪詢間隔由 Source.poll_interval_minutes 與健康度降頻決定
# （見 apps.ingest.services.should_poll），Beat 只需夠頻繁地檢查誰到期。
app.conf.beat_schedule = {
    "poll-due-sources": {
        "task": "apps.ingest.tasks.poll_due_sources",
        "schedule": 300.0,          # 每 5 分鐘檢查一次有誰到期
        "options": {"queue": "fetch"},
    },
    "fill-missing-bodies": {
        "task": "apps.ingest.tasks.fill_bodies",
        "schedule": 600.0,          # 每 10 分鐘補齊缺內文的文件
        "options": {"queue": "fetch"},
    },
    "dedupe-recent": {
        "task": "apps.ingest.tasks.dedupe_recent_documents",
        "schedule": 3600.0,         # 每小時歸併轉載
        "options": {"queue": "fetch"},
    },
    # 採購網是查詢驅動（拿事件去問），不是訂閱最新，因此頻率可低。
    # 標案公告的變動遠慢於新聞。
    "check-procurement": {
        "task": "apps.events.tasks.check_procurement",
        "schedule": 86400.0,        # 每日一次
        "options": {"queue": "fetch"},
    },
    "check-official-records": {
        "task": "apps.events.tasks.check_official_records",
        # 任務 27：active／dormant 事件的官方源檢查頻率皆為每日一次，
        # 沉寂事件刻意不降頻（新聞沉寂期正是判決出爐的時期）。
        # Beat 本身排更密（4 小時）只是「檢查誰到期」，實際頻率仍由
        # Event.due_for_official_check() 決定，同一天內只會真的執行一次。
        "schedule": 14400.0,
        "options": {"queue": "fetch"},
    },
}
