"""以規則抽取識別碼並回填（任務 15、ADR-0001）。

**不呼叫 LLM。** 案號與統編有明確格式，規則抽取既免費又能立刻覆蓋
全部語料——不必等 L1 抽取完成。這一點對 ADR-0005 很重要：
它把識別碼精確比對列為事件歸屬的第一優先，若要等 LLM 才有識別碼，
歸屬就得跟著等。

只寫入實際變動的列——先前為了迭代規則而反覆全表更新，曾讓表由
3.4GB 膨脹至 8.2GB 並造成鎖車陣。
"""
import time

from django.core.management.base import BaseCommand

from apps.core.identifiers import extract_case_numbers, extract_tax_ids
from apps.ingest.models import Document


class Command(BaseCommand):
    help = "以規則抽取案號與統編（不呼叫 LLM）"

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--batch", type=int, default=500)

    def handle(self, *args, **options):
        queryset = Document.objects.exclude(raw_body="")
        if not options["all"]:
            queryset = queryset.filter(case_numbers=[], tax_ids=[])
        queryset = queryset.only(
            "id", "title", "raw_body", "case_numbers", "tax_ids"
        ).order_by("id")

        total = queryset.count()
        self.stdout.write(f"待處理 {total:,} 篇")
        started, processed, changed = time.time(), 0, 0
        with_case = with_tax = 0
        batch = []

        for doc in queryset.iterator(chunk_size=options["batch"]):
            text = f"{doc.title}\n{doc.raw_body}"
            cases = extract_case_numbers(text)
            taxes = extract_tax_ids(text)
            processed += 1
            with_case += bool(cases)
            with_tax += bool(taxes)

            if doc.case_numbers != cases or doc.tax_ids != taxes:
                doc.case_numbers, doc.tax_ids = cases, taxes
                batch.append(doc)
                changed += 1

            if len(batch) >= options["batch"]:
                Document.objects.bulk_update(batch, ["case_numbers", "tax_ids"])
                batch = []
            if processed % 10000 == 0:
                self.stdout.write(
                    f"  {processed:>7,}/{total:,}  有案號 {with_case:>6,}  "
                    f"有統編 {with_tax:>6,}  {processed/(time.time()-started):>5.0f} 篇/秒",
                    ending="\r")

        if batch:
            Document.objects.bulk_update(batch, ["case_numbers", "tax_ids"])

        self.stdout.write(self.style.SUCCESS(
            f"\n完成：處理 {processed:,}、寫入 {changed:,}、"
            f"有案號 {with_case:,}（{with_case/processed*100:.2f}%）、"
            f"有統編 {with_tax:,}（{with_tax/processed*100:.2f}%）、"
            f"{(time.time()-started)/60:.1f} 分鐘"))
