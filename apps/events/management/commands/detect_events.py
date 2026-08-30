"""事件偵測：把未歸屬文件的叢集轉成候選事件（任務 24）。

ADR-0005 第二階段。輸入是「有向量、相關、但尚未歸屬任何事件」的文件——
第一階段（任務 23 的 ``auto_assign``）應先跑過，才能確保這裡處理的
真的是「未歸屬」而非「還沒被嘗試歸屬」。

**不呼叫 LLM 生成標題或摘要。** 候選事件的標題只是給人工審核用的
辨識線索（取最早一篇文件的標題），正式標題由審核者在確認事件時填寫。
在偵測階段就花錢生成標題，等於為每一個可能被駁回的候選都預先付費——
而候選被駁回是預期中的常態，不是例外。
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand
from django.utils.text import slugify

from apps.events.clustering import cluster_documents
from apps.events.models import (
    AssignmentMethod, Event, EventDocument, EventStatus,
)
from apps.extract.relevance import is_corroborated
from apps.ingest.models import Document

MIN_CLUSTER_SIZE = 5  # 任務 25 回測校準：ARI 0.43，優於 3（過度分裂）與 8+（塌縮成單簇）


class Command(BaseCommand):
    help = "叢集未歸屬文件，建立候選事件供人工審核"

    def add_arguments(self, parser):
        parser.add_argument("--min-cluster-size", type=int, default=MIN_CLUSTER_SIZE)
        parser.add_argument("--dry-run", action="store_true",
                            help="只顯示會產生的候選事件，不寫入資料庫")

    def handle(self, *args, **options):
        already_assigned = set(
            EventDocument.objects.values_list("document_id", flat=True))
        loosely_relevant = list(
            Document.objects.relevant()
            .exclude(embedding=None)
            .exclude(pk__in=already_assigned)
            .filter(canonical_of__isnull=True)
            .only("id", "title", "embedding", "published_at", "relevance_signals")
        )

        # **這一步是實測後補上的，不是預設就有的。** 相關性過濾（任務
        # 13.5）刻意偏向召回，單一類別命中就通過——這對「該不該做 L1
        # 抽取」是對的，但直接拿來做叢集候選池會讓「大法官人事任命」
        # 「軍公教年金訴訟」這類政治爭議新聞（透過 judicial_body 類別
        # 命中「法官」「檢方」等機構稱謂通過過濾）也被分群成看似完整
        # 的候選事件。實測：跑一次全量偵測，343 個候選裡 245 個
        # （71%）標題完全不含任何司法弊案核心詞。見
        # apps.extract.relevance.is_corroborated 的完整說明。
        candidates = [d for d in loosely_relevant if is_corroborated(d.relevance_signals or {})]
        excluded = len(loosely_relevant) - len(candidates)

        if not candidates:
            self.stdout.write("沒有待偵測的未歸屬文件")
            return

        self.stdout.write(f"候選池：{len(candidates):,} 篇未歸屬文件"
                          f"（另有 {excluded:,} 篇僅鬆散相關，已排除不進叢集）")

        ids = [d.pk for d in candidates]
        embeddings = [list(d.embedding) for d in candidates]
        by_id = {d.pk: d for d in candidates}

        clusters = cluster_documents(ids, embeddings, min_cluster_size=options["min_cluster_size"])
        noise_count = len(ids) - sum(len(c.document_ids) for c in clusters)
        self.stdout.write(f"找到 {len(clusters)} 個叢集，{noise_count} 篇維持未歸屬"
                          f"（noise，保留待下次批次）")

        for cluster in clusters:
            docs = [by_id[i] for i in cluster.document_ids]
            docs.sort(key=lambda d: d.published_at or dt.datetime.max.replace(
                tzinfo=dt.timezone.utc))
            earliest = docs[0]
            title = f"待命名事件：{earliest.title[:40]}"
            self.stdout.write(f"\n候選（{len(docs)} 篇）：{title}")
            for d in docs[:5]:
                self.stdout.write(f"    {d.title[:56]}")
            if len(docs) > 5:
                self.stdout.write(f"    …另 {len(docs) - 5} 篇")

            if options["dry_run"]:
                continue

            slug = f"candidate-{earliest.pk}"
            event = Event.objects.create(
                slug=slug, title=title, status=EventStatus.CANDIDATE,
                first_seen_at=earliest.published_at,
                last_progress_at=docs[-1].published_at,
            )
            EventDocument.objects.bulk_create([
                EventDocument(event=event, document=d, method=AssignmentMethod.CLUSTER,
                              reason=f"HDBSCAN 叢集（min_cluster_size={options['min_cluster_size']}）")
                for d in docs
            ])
            event.recompute_risk_tier(save=True)

        if not options["dry_run"] and clusters:
            self.stdout.write(self.style.SUCCESS(
                f"\n已建立 {len(clusters)} 個候選事件，待審核（/review/）"))
