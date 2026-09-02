"""對已歸屬到追蹤事件的文件執行 L1 抽取。

新歸屬的文件由 ``apply_assignments`` 自動觸發；這個指令用於補跑
在該機制建立之前就已歸屬的文件。
"""
from django.core.management.base import BaseCommand

from apps.extract.models import PROMPT_VERSION, Extraction
from apps.extract.service import extract_document
from apps.ingest.models import Document
from apps.llm.provider import LlmError


class Command(BaseCommand):
    help = "對已歸屬事件、但尚未抽取的文件執行 L1 抽取"

    def add_arguments(self, parser):
        parser.add_argument("--event", default="", metavar="SLUG",
                            help="只處理單一事件")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true",
                            help="只顯示會抽幾篇與預估成本")

    def handle(self, *args, **options):
        queryset = (Document.objects.filter(event_documents__isnull=False)
                    .exclude(raw_body="").distinct())
        if options["event"]:
            queryset = queryset.filter(event_documents__event__slug=options["event"])

        done = set(Extraction.objects
                   .filter(prompt_version=PROMPT_VERSION, succeeded=True)
                   .values_list("document_id", flat=True))
        todo = [d for d in queryset if d.pk not in done]
        if options["limit"]:
            todo = todo[:options["limit"]]

        if not todo:
            self.stdout.write("沒有待抽取的文件")
            return

        # 單篇實測成本，用於事前預估——L1 是唯一會大量花錢的步驟，
        # 讓人在按下去之前就知道要花多少。
        estimate = len(todo) * 0.00189
        self.stdout.write(f"待抽取 {len(todo):,} 篇，預估 US${estimate:.2f}")
        if options["dry_run"]:
            return

        ok = failed = 0
        for i, doc in enumerate(todo, 1):
            try:
                extraction = extract_document(doc, task_name="event_linked")
            except LlmError as exc:
                self.stderr.write(self.style.ERROR(f"第 {i} 篇後中止：{exc}"))
                break
            ok += extraction.succeeded
            failed += not extraction.succeeded
            if i % 50 == 0:
                self.stdout.write(f"  {i}/{len(todo)}　成功 {ok}、失敗 {failed}")

        self.stdout.write(self.style.SUCCESS(
            f"完成：成功 {ok}、失敗 {failed}"))
