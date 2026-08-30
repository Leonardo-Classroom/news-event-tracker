"""批次執行 L1 結構化抽取（任務 14）。

**預設只處理已歸屬事件的文件，不是全庫。** 全庫 36,399 篇通過相關性
過濾的文件全量抽取估計 US$48–96（見 docs/02-任務拆解.md），超出本專案
的 LLM 預算硬上限（US$5）。已歸屬事件的文件是「已知道屬於哪個事件、
需要時間線與因果子圖」的那一批——先把預算花在這裡才有實際產出，
全庫抽取留待預算另外核准。

**逐篇即時寫入，不是蒐集完才寫。** `extract_document()` 內部本來就是
呼叫一次就存一筆——這正是任務 23 自動歸屬撞過的教訓（一次逾時的執行
花了錢卻沒寫入任何結果）在這裡不會重演的原因，因為寫入不是本命令自己
做的，是 service 函式的既有設計。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.events.models import EventDocument
from apps.extract.models import Extraction
from apps.extract.service import extract_document
from apps.ingest.models import Document
from apps.llm.budget import BudgetExceeded, remaining_usd, spent_usd


class Command(BaseCommand):
    help = "批次執行 L1 結構化抽取"

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true",
                            help="處理全庫通過相關性過濾者，而非只處理已歸屬事件的文件")
        parser.add_argument("--event", default="",
                            help="只處理指定 slug 事件的文件")
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, **options):
        already = Extraction.objects.values_list("document_id", flat=True)

        if options["event"]:
            queryset = Document.objects.filter(
                event_documents__event__slug=options["event"])
        elif options["all"]:
            queryset = Document.objects.relevant()
        else:
            queryset = Document.objects.filter(
                pk__in=EventDocument.objects.values_list("document_id", flat=True))

        queryset = (queryset.exclude(pk__in=already)
                    .exclude(raw_body="")
                    .order_by("id").distinct())

        if options["limit"]:
            queryset = queryset[:options["limit"]]

        total = queryset.count()
        if not total:
            self.stdout.write("沒有待抽取的文件")
            return

        self.stdout.write(f"待抽取 {total:,} 篇（預算剩餘 US${remaining_usd():.4f}）")

        before = spent_usd()
        done = 0
        for doc in queryset.iterator(chunk_size=50):
            try:
                extract_document(doc, task_name="run_extraction")
            except BudgetExceeded:
                self.stdout.write(self.style.WARNING(
                    f"已達 LLM 預算上限，停止於第 {done}/{total} 篇"))
                break
            done += 1
            if done % 25 == 0:
                self.stdout.write(
                    f"  {done}/{total}　已花費 US${spent_usd() - before:.4f}",
                    ending="\r")

        self.stdout.write(self.style.SUCCESS(
            f"\n完成 {done}/{total} 篇，本次花費 US${spent_usd() - before:.4f}"
            f"，剩餘 US${remaining_usd():.4f}"))
