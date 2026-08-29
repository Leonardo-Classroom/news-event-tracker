"""批次產生文件向量（任務 17）。

**規模考量**：全庫 238,324 篇、實測 2.43 篇/秒，全量約 27 小時。
因此設計為可續跑、可分批、可依條件限縮——不是一次性腳本。

實務上不需要全量：
- 事件歸屬只需比對**相關**文件（36,399 篇，約 4.2 小時）
- 更進一步，可只對「有事件的期間」的文件向量化

預設只處理通過相關性過濾者。``--all`` 才處理全庫。
"""
import time

from django.core.management.base import BaseCommand

from apps.ingest.models import Document
from apps.retrieval.embedding import (
    EMBEDDING_MODEL_VERSION, build_embedding_text, embed_texts,
)


class Command(BaseCommand):
    help = "產生文件向量（BGE-M3，CPU）"

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true",
                            help="處理全庫（預設只處理通過相關性過濾者）")
        parser.add_argument("--redo", action="store_true",
                            help="重算已有向量者（換模型後使用）")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--batch", type=int, default=64,
                            help="每批送入模型的文件數")
        parser.add_argument("--ids", default="",
                            help="只處理指定 id（逗號分隔）。用於優先向量化"
                                 "特定文件（如標註集），不必等全量批次跑到它們——"
                                 "全量以 id 遞增順序處理，較新的文件（通常 id 較大）"
                                 "要等數小時才輪到。")

    def handle(self, *args, **options):
        queryset = Document.objects.exclude(raw_body="")
        if options["ids"]:
            ids = [int(x) for x in options["ids"].split(",") if x.strip()]
            queryset = queryset.filter(pk__in=ids)
        elif not options["all"]:
            queryset = queryset.relevant()
        if not options["redo"]:
            # 版本不符者也要重算——換模型後舊向量不可與新向量比對
            queryset = queryset.exclude(embedding_version=EMBEDDING_MODEL_VERSION)
        queryset = queryset.only("id", "title", "raw_body").order_by("id")

        total = queryset.count()
        if not total:
            self.stdout.write("沒有待處理的文件")
            return
        self.stdout.write(f"待向量化 {total:,} 篇（模型版本 {EMBEDDING_MODEL_VERSION}）")

        started, done = time.time(), 0
        limit, batch_size = options["limit"], options["batch"]
        buffer: list[Document] = []

        for doc in queryset.iterator(chunk_size=batch_size):
            buffer.append(doc)
            if len(buffer) >= batch_size:
                done += self._flush(buffer)
                buffer = []
                self._progress(done, total, started)
            if limit and done >= limit:
                break

        if buffer:
            done += self._flush(buffer)
        self._progress(done, total, started, final=True)

    def _flush(self, docs) -> int:
        texts = [build_embedding_text(d.title, d.raw_body) for d in docs]
        vectors = embed_texts(texts)
        for doc, vector in zip(docs, vectors):
            doc.embedding = vector
            doc.embedding_version = EMBEDDING_MODEL_VERSION
        # 只更新這兩個欄位——不觸發 save() 的衍生欄位重算，
        # 那會白白重跑 bigram 與 SimHash，並製造大量死元組。
        Document.objects.bulk_update(docs, ["embedding", "embedding_version"])
        return len(docs)

    def _progress(self, done, total, started, final=False):
        elapsed = time.time() - started
        rate = done / elapsed if elapsed else 0
        eta = (total - done) / rate / 3600 if rate else 0
        message = (f"  {done:>7,}/{total:,}  {rate:>5.2f} 篇/秒  "
                   f"已耗時 {elapsed/60:>5.1f} 分  剩餘約 {eta:>4.1f} 小時")
        if final:
            self.stdout.write(self.style.SUCCESS(
                f"\n完成 {done:,} 篇，耗時 {elapsed/60:.1f} 分鐘（{rate:.2f} 篇/秒）"))
        else:
            self.stdout.write(message, ending="\r")
