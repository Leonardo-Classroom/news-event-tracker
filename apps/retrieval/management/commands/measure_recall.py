"""量測混合召回的召回率（任務 19 的驗收）。

**這個量測有一個必須說清楚的偏誤，否則數字是假的。**

標註集（fixtures/label_candidates.tsv）的候選來自
``BigramFtsBackend.search(Document.objects.relevant(), keyword)``。
也就是說，每一篇正例在被標註之前，就已經：

    1. 通過了相關性過濾器，且
    2. 被關鍵字通道撈出來過

因此「關鍵字通道能不能撈到這些正例」是循環論證，答案必然接近 100%，
而那個 100% 不代表任何事。本會話稍早已經犯過一次同樣的錯：先量到
過濾器召回率 100%，才發現候選本身就來自已過濾的文件；改在被拒絕的
文件上量測，新竹棒球場案立刻出現 40 篇中漏 15 篇。

**可信的與不可信的，界線在「集合」與「順序」之間：**

    集合（誰進得來）  循環。關鍵字通道的 recall 必然虛高，
                      向量通道的 recall 則是乾淨的——標註集的產生
                      過程完全沒有用到向量。
    順序（誰排前面）  不循環。標註集告訴我們這批關鍵字可及的文件裡
                      哪些真的屬於該事件；能否把它們排在負例之前，
                      是真實的量測。

所以本指令的主要指標是 **precision@k 與 MRR**，recall 僅對向量通道
與整體 top-k 截斷有意義。輸出會逐項標示哪些數字帶偏誤。
"""
import collections
import csv

from django.core.management.base import BaseCommand

from apps.events.models import Event
from apps.events.terms import build_event_query
from apps.ingest.models import Document
from apps.retrieval.hybrid import HybridRetriever
from apps.retrieval.keyword import BigramFtsBackend

#: 事件核心詞。取自 L1 抽取的實體（見任務 21 的失敗紀錄：
#: 詞頻 n-gram、TF-IDF 對比、子字串包含三種自動抽取法都失敗，
#: TF-IDF 甚至系統性地獎勵無意義的斷詞碎片，因為碎片最罕見）。
FALLBACK_QUERIES = {
    "hsinchu-stadium": "新竹棒球場 林智堅 高虹安 球場 驗收",
    "core-pacific": "京華城 柯文哲 沈慶京 容積 圖利 應曉薇",
    "taipei-dome": "大巨蛋 遠雄 趙藤雄 圖利",
    "nanfangao": "南方澳 斷橋 運安會 吊索",
    "chaosi-eggs": "超思 進口蛋 陳吉仲 農業部",
    "chengxin-solar": "誠新綠能 背信 侵占 挪用",
}

CUTOFFS = (10, 25, 50, 100)


class Command(BaseCommand):
    help = "以人工標註集量測混合召回的品質"

    def add_arguments(self, parser):
        parser.add_argument("--labels", default="fixtures/label_candidates.tsv")
        parser.add_argument("--top-k", type=int, default=100)
        parser.add_argument("--channels", default="keyword,vector",
                            help="以逗號分隔：keyword、vector")

    def handle(self, *args, **options):
        channels = tuple(c.strip() for c in options["channels"].split(",") if c.strip())
        self._freq_cache: dict[str, float] = {}
        self._corpus_size = Document.objects.filter(canonical_of__isnull=True).count()
        labels = self._load(options["labels"])
        if not labels:
            self.stderr.write("標註集沒有已填 label 的資料")
            return

        vectorised = Document.objects.exclude(embedding=None).count()
        total = Document.objects.count()
        self.stdout.write(
            f"語料 {total:,} 篇，已向量化 {vectorised:,} 篇"
            f"（{vectorised / total * 100:.1f}%）")
        if "vector" in channels and vectorised < total * 0.9:
            self.stdout.write(self.style.WARNING(
                "  向量化未完成——向量通道的召回率會被低估，"
                "尚未向量化的正例在該通道不存在。"))
        self.stdout.write("")

        retriever = HybridRetriever()
        summary = []

        for slug, positives in sorted(labels.items()):
            query = self._query_for(slug)
            found = retriever.search(query, top_k=options["top_k"], channels=channels)
            ranked = [c.document_id for c in found]
            positions = {doc_id: i for i, doc_id in enumerate(ranked, start=1)}

            hits = [positions[p] for p in positives if p in positions]
            recall = len(hits) / len(positives)
            mrr = 1 / min(hits) if hits else 0.0

            row = {"slug": slug, "n": len(positives), "recall": recall, "mrr": mrr}
            for k in CUTOFFS:
                if k > options["top_k"]:
                    continue
                at_k = sum(1 for h in hits if h <= k)
                row[f"p@{k}"] = at_k / k
                row[f"r@{k}"] = at_k / len(positives)
            summary.append(row)

            missed = [p for p in positives if p not in positions]
            self.stdout.write(f"── {slug}　正例 {len(positives)} 篇 ──")
            self.stdout.write(f"   查詢：{query}")
            self.stdout.write(
                f"   召回 {len(hits)}/{len(positives)}（{recall:.0%}）"
                f"　MRR {mrr:.3f}"
                f"　最佳名次 {min(hits) if hits else '—'}")
            for k in CUTOFFS:
                if f"p@{k}" in row:
                    self.stdout.write(
                        f"     @{k:<4}precision {row[f'p@{k}']:.0%}"
                        f"　recall {row[f'r@{k}']:.0%}")
            if missed:
                self._show_missed(missed)
            self.stdout.write("")

        self._show_summary(summary, channels)

    # ------------------------------------------------------------ 輔助

    def _load(self, path) -> dict[str, list[int]]:
        positives = collections.defaultdict(list)
        with open(path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                if row.get("label") == "1":
                    positives[row["event_slug"]].append(int(row["doc_id"]))
        return dict(positives)

    def _query_for(self, slug: str) -> str:
        """優先用事件登記的核心詞，沒有才用備援查詢。"""
        event = Event.objects.filter(slug=slug).first()
        if event:
            query = build_event_query(event, document_frequency=self._frequency)
            if query:
                return query
        from apps.events.terms import EventQuery
        return EventQuery(identity=FALLBACK_QUERIES.get(slug, slug))

    def _frequency(self, term: str) -> float:
        """該詞的文件頻率。查過的快取起來——同一個詞常跨事件出現，
        而每次查詢都是一次全表 FTS 掃描。"""
        if term not in self._freq_cache:
            base = Document.objects.filter(canonical_of__isnull=True)
            matched = BigramFtsBackend().search(base, term, mode="all").count()
            self._freq_cache[term] = matched / max(self._corpus_size, 1)
        return self._freq_cache[term]

    def _show_missed(self, missed: list[int]):
        self.stdout.write(self.style.WARNING(f"   漏掉 {len(missed)} 篇："))
        docs = Document.objects.filter(pk__in=missed[:5]).select_related("source")
        for doc in docs:
            date = doc.published_at.strftime("%Y-%m-%d") if doc.published_at else "無日期"
            has_vector = "有向量" if doc.embedding is not None else "無向量"
            self.stdout.write(f"     [{doc.pk}] {date} {has_vector}　{doc.title[:40]}")
        if len(missed) > 5:
            self.stdout.write(f"     …另 {len(missed) - 5} 篇")

    def _show_summary(self, summary, channels):
        n = sum(r["n"] for r in summary)
        # 以正例數加權——事件的正例數差異很大（京華城 30 篇、大巨蛋 1 篇），
        # 未加權的平均會讓只有 1 篇正例的事件與 30 篇的等重
        weighted = sum(r["recall"] * r["n"] for r in summary) / n
        macro = sum(r["recall"] for r in summary) / len(summary)

        self.stdout.write("═" * 58)
        self.stdout.write(f"通道：{'+'.join(channels)}　正例合計 {n} 篇")
        self.stdout.write(f"  加權召回率 {weighted:.1%}　未加權 {macro:.1%}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("偏誤說明（數字要這樣讀）："))
        if "keyword" in channels:
            self.stdout.write(
                "  · 關鍵字通道的召回率**虛高**。標註集的候選就是用關鍵字\n"
                "    搜出來的，正例必然關鍵字可及。這個數字只能證明查詢\n"
                "    沒寫錯，不能證明召回品質。")
        if "vector" in channels:
            self.stdout.write(
                "  · 向量通道的召回率**乾淨**。標註集的產生完全沒用到向量，\n"
                "    向量能撈到多少正例是真實的量測。")
        self.stdout.write(
            "  · precision@k 與 MRR 兩者都**不受此偏誤影響**——標註集\n"
            "    同時含正例與負例，能否把正例排在負例之前是真實的。")
        self.stdout.write(
            "  · 對「關鍵字撈不到、只有向量撈得到」的文件，本標註集\n"
            "    無法量測——那類文件從未進入候選。要量測需另建一組\n"
            "    以向量為種子的標註集（列為任務 20 的後續）。")
