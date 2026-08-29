"""HDBSCAN 門檻回測（任務 25）。

用任務 20 的人工標註集校準 ``min_cluster_size``。標註集裡的正例是
「已知屬於某事件」的文件，負例是「曾被關鍵字搜出但人工判定不屬於」——
後者不是單一類別，只是「非此事件」，因此以單一「noise」類別看待，
不追究它們彼此是否該被分成不同事件。

評估三件事：
1. **同事件的正例是否被分進同一簇**（homogeneity 的反面——不該被拆散）
2. **不同事件的正例是否被誤併成同一簇**（純度／homogeneity）
3. **負例是否被誤含入某個事件簇**（噪聲污染率）

第 3 點最重要：污染的代價是把無關文件混進事件時間線，這正是規格
R4「自動叢集品質差」要防的事。
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from sklearn.metrics import adjusted_rand_score, homogeneity_completeness_v_measure

from apps.events.clustering import cluster_documents
from apps.ingest.models import Document

CANDIDATES = (3, 5, 8, 10, 15)


class Command(BaseCommand):
    help = "以標註測試集回測 HDBSCAN 的 min_cluster_size"

    def add_arguments(self, parser):
        parser.add_argument("--labels", default="fixtures/label_candidates.tsv")

    def handle(self, *args, **options):
        rows = list(csv.DictReader(open(options["labels"], encoding="utf-8"), delimiter="\t"))
        true_label: dict[int, str] = {}
        for row in rows:
            doc_id = int(row["doc_id"])
            true_label[doc_id] = row["event_slug"] if row["label"] == "1" else "noise"

        docs = list(Document.objects.filter(pk__in=true_label).exclude(embedding=None))
        missing = set(true_label) - {d.pk for d in docs}
        if missing:
            self.stdout.write(self.style.WARNING(
                f"{len(missing)} 篇尚無向量，本次回測排除"))

        ids = [d.pk for d in docs]
        embeddings = [list(d.embedding) for d in docs]
        truth = [true_label[i] for i in ids]

        self.stdout.write(f"回測樣本：{len(ids)} 篇"
                          f"（{sum(1 for t in truth if t != 'noise')} 正例、"
                          f"{sum(1 for t in truth if t == 'noise')} 負例）\n")

        for min_size in CANDIDATES:
            self._evaluate(min_size, ids, embeddings, truth)

    def _evaluate(self, min_size, ids, embeddings, truth):
        clusters = cluster_documents(ids, embeddings, min_cluster_size=min_size)
        cluster_of: dict[int, int] = {}
        for c in clusters:
            for doc_id in c.document_ids:
                cluster_of[doc_id] = c.label

        predicted = [cluster_of.get(i, -1) for i in ids]

        # 污染率：某個簇裡混了多少比例的負例（noise 標籤但被分進了某個簇）
        contaminated = 0
        cluster_members: dict[int, list[str]] = defaultdict(list)
        for label, t in zip(predicted, truth):
            if label != -1:
                cluster_members[label].append(t)
        for members in cluster_members.values():
            contaminated += sum(1 for m in members if m == "noise")
        clustered_total = sum(len(m) for m in cluster_members.values())
        contamination_rate = contaminated / clustered_total if clustered_total else 0.0

        # ARI／homogeneity 只在有非 noise 的簇時有意義
        has_signal = any(l != -1 for l in predicted)
        if has_signal:
            ari = adjusted_rand_score(truth, predicted)
            homogeneity, completeness, _ = homogeneity_completeness_v_measure(truth, predicted)
        else:
            ari = homogeneity = completeness = float("nan")

        n_clusters = len(cluster_members)
        n_noise = predicted.count(-1)

        self.stdout.write(
            f"min_cluster_size={min_size:<3} 簇數={n_clusters:<2} "
            f"noise={n_noise:<3} 污染率={contamination_rate:.0%} "
            f"ARI={ari:.2f} homogeneity={homogeneity:.2f} completeness={completeness:.2f}"
        )
