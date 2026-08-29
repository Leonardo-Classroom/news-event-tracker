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
