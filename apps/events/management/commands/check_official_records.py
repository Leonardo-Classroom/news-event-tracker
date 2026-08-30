"""執行官方源檢查（任務 27+33）。由 Celery Beat 排程呼叫，也可手動執行。"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.events.official_check import check_due_events


class Command(BaseCommand):
    help = "對到期的追蹤中事件，檢查司法院月封存檔有無新進展"

    def add_arguments(self, parser):
        parser.add_argument("--months-back", type=int, default=2,
                            help="往回查幾個月的封存檔")
        parser.add_argument("--dry-run", action="store_true",
                            help="只顯示會命中什麼，不實際入庫")

    def handle(self, *args, **options):
        summary = check_due_events(
            months_back=options["months_back"], dry_run=options["dry_run"])

        if summary.session_expired:
            self.stderr.write(self.style.WARNING(
                "司法院資料開放平台登入 session 已過期或尚未設定，"
                "請至 /crawlers/ 重新登入後再試"))
            return

        if summary.events_checked == 0:
            self.stdout.write("沒有到期的事件需要檢查")
            return

        self.stdout.write(
            f"檢查了 {summary.events_checked} 個事件，"
            f"下載 {summary.archives_downloaded} 份封存檔，"
            f"命中 {len(summary.results)} 筆新進展")
        for result in summary.results:
            woke = "（喚醒 dormant → active）" if result.woke_dormant_event else ""
            self.stdout.write(
                f"  事件 {result.matched_event.slug}：{result.document.title[:50]}{woke}")
