"""批次執行相關性過濾（ADR-0009）。

純規則、不呼叫 LLM、不花錢。冪等：已評估者預設跳過。
"""
import time

from django.core.management.base import BaseCommand

from apps.extract.relevance import assess_relevance
from apps.ingest.models import Document


class Command(BaseCommand):
    help = "以規則評估文件的相關性（純規則，不呼叫 LLM）"

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true",
                            help="重新評估全部（預設只評估尚未評估者）")
        parser.add_argument("--batch", type=int, default=2000)
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, **options):
        queryset = (Document.objects.all() if options["all"]
                    else Document.objects.pending_relevance())
        queryset = queryset.only("id", "title", "raw_body").order_by("id")

        total = queryset.count()
        if not total:
            self.stdout.write("沒有待評估的文件")
            return
        self.stdout.write(f"待評估 {total:,} 篇")

        started, processed, relevant = time.time(), 0, 0
        batch, batch_size = [], options["batch"]
        limit = options["limit"]

        for doc in queryset.iterator(chunk_size=batch_size):
            result = assess_relevance(doc.title, doc.raw_body)
            doc.relevant = result.relevant
            doc.relevance_signals = {
                "categories": result.categories,
                "hits": result.signals,
                "has_identifier": result.has_identifier,
            }
            batch.append(doc)
            processed += 1
            relevant += result.relevant

            if len(batch) >= batch_size:
                Document.objects.bulk_update(batch, ["relevant", "relevance_signals"])
                batch = []
                elapsed = time.time() - started
                self.stdout.write(
                    f"  {processed:>7,}/{total:,}  相關 {relevant:>6,}"
                    f" ({relevant/processed*100:>4.1f}%)  "
                    f"{processed/elapsed:>6.0f} 篇/秒", ending="\r")
            if limit and processed >= limit:
                break

        if batch:
            Document.objects.bulk_update(batch, ["relevant", "relevance_signals"])

        elapsed = time.time() - started
        self.stdout.write(self.style.SUCCESS(
            f"\n完成：評估 {processed:,}、相關 {relevant:,}"
            f"（{relevant/processed*100:.1f}%）、{elapsed/60:.1f} 分鐘"))
