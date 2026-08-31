"""回填缺發布時間的文件（清單頁入庫只有標題）。

中時、聯合的 HTML 清單沒有可靠時間，時間在內頁 JSON-LD 或
``article:published_time``。已有內文的列不會再走 ``fill_missing_bodies``，
所以缺日期的要另補一次。
"""
from django.core.management.base import BaseCommand

from apps.ingest.services import fill_missing_published_at


class Command(BaseCommand):
    help = "抓內頁補 published_at（只填缺值）"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                            help="最多處理幾篇，0 表示全部")
        parser.add_argument("--source", default="",
                            help="只處理此 source slug")

    def handle(self, *args, **options):
        result = fill_missing_published_at(
            limit=options["limit"], source_slug=options["source"])
        self.stdout.write(
            f"嘗試 {result.attempted}、補上 {result.filled}、失敗 {result.failed}"
        )
        for err in result.errors[:20]:
            self.stdout.write(self.style.WARNING(f"  {err}"))
        if len(result.errors) > 20:
            self.stdout.write(f"  …另有 {len(result.errors) - 20} 筆失敗")
