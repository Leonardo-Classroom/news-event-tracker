"""批次執行相關性過濾（ADR-0009）。

純規則、不呼叫 LLM、不花錢。冪等：已評估者預設跳過。

**只寫入真正變動的列。** 迭代規則時會反覆執行 ``--all``，而
PostgreSQL 的 MVCC 對每次 UPDATE 都會保留舊版本作為死元組。
實測：對 238,324 列做三次全表更新後，表由 3.4GB 膨脹至 8.2GB、
死元組 427,584（膨脹率 180%），單一批次的 UPDATE 耗時超過 850 秒，
並在資料庫形成鎖車陣，連 Celery 的正常寫入都被卡住。

規則調整通常只影響少數列，因此比對後只更新有變動者，
可讓重跑的寫入量從「全部」降到「差異」。
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
        # 批次不宜過大：bulk_update 會產生含 N 個分支的 CASE 陳述式，
        # 2000 列時單一 UPDATE 實測超過 850 秒。
        parser.add_argument("--batch", type=int, default=500)
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, **options):
        queryset = (Document.objects.all() if options["all"]
                    else Document.objects.pending_relevance())
        queryset = queryset.only(
            "id", "title", "raw_body", "relevant", "relevance_signals"
        ).order_by("id")

        total = queryset.count()
        if not total:
            self.stdout.write("沒有待評估的文件")
            return
        self.stdout.write(f"待評估 {total:,} 篇")

        started, processed, relevant, changed = time.time(), 0, 0, 0
        batch, batch_size = [], options["batch"]
        limit = options["limit"]

        for doc in queryset.iterator(chunk_size=batch_size):
            result = assess_relevance(doc.title, doc.raw_body)
            signals = {
                "categories": result.categories,
                "hits": result.signals,
                "has_identifier": result.has_identifier,
            }
            processed += 1
            relevant += result.relevant

            # 只有實際變動才寫入。規則調整通常只影響少數列，
            # 全部改寫會製造大量死元組並拖垮資料庫（見模組說明）。
            if doc.relevant != result.relevant or doc.relevance_signals != signals:
                doc.relevant = result.relevant
                doc.relevance_signals = signals
                batch.append(doc)
                changed += 1

            if len(batch) >= batch_size:
                Document.objects.bulk_update(batch, ["relevant", "relevance_signals"])
                batch = []

            if processed % 5000 == 0:
                elapsed = time.time() - started
                self.stdout.write(
                    f"  {processed:>7,}/{total:,}  相關 {relevant:>6,}"
                    f" ({relevant/processed*100:>4.1f}%)  變動 {changed:>6,}  "
                    f"{processed/elapsed:>6.0f} 篇/秒", ending="\r")
            if limit and processed >= limit:
                break

        if batch:
            Document.objects.bulk_update(batch, ["relevant", "relevance_signals"])

        elapsed = time.time() - started
        self.stdout.write(self.style.SUCCESS(
            f"\n完成：評估 {processed:,}、相關 {relevant:,}"
            f"（{relevant/processed*100:.1f}%）、實際寫入 {changed:,} 列、"
            f"{elapsed/60:.1f} 分鐘"))
