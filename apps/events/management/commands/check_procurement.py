"""對追蹤中的事件查政府採購網（任務 55）。"""
from django.core.management.base import BaseCommand

from apps.events.procurement_check import check_events


class Command(BaseCommand):
    help = "對追蹤中的事件查詢政府採購網並入庫命中的標案（冪等）"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="只查詢與統計，不寫入")
        parser.add_argument("--event", default="", metavar="SLUG",
                            help="只檢查單一事件")

    def handle(self, *args, **options):
        summary = check_events(dry_run=options["dry_run"],
                               only_slug=options["event"])
        self.stdout.write(
            f"事件 {summary.events_checked}　查詢 {summary.queries} 次　"
            f"命中 {summary.hits} 筆　新增 {summary.created}　更新 {summary.updated}"
        )
