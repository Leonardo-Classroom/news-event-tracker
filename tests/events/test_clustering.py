"""HDBSCAN 叢集測試（任務 24）。以合成向量覆蓋演算法邏輯——
不需要真實 embedding 或資料庫，叢集本身是純數值運算。"""
import numpy as np
import pytest

from apps.events.clustering import cluster_documents


def _blob(center: list[float], n: int, *, spread: float = 0.02, seed: int = 0):
    rng = np.random.default_rng(seed)
    vectors = rng.normal(loc=center, scale=spread, size=(n, len(center)))
    # 正規化成單位向量，比照 BGE-M3 的實際輸出（ADR-0004）
    return [list(v / np.linalg.norm(v)) for v in vectors]


class TestClusterDocuments:
    def test_兩個分離的密集群各自成簇(self):
        cluster_a = _blob([1.0, 0.0, 0.0], 8, seed=1)
        cluster_b = _blob([0.0, 1.0, 0.0], 8, seed=2)
        embeddings = cluster_a + cluster_b
        ids = list(range(16))

        result = cluster_documents(ids, embeddings, min_cluster_size=5)

        assert len(result) == 2
        sizes = sorted(len(c.document_ids) for c in result)
        assert sizes == [8, 8]

    def test_孤立的單一簇混雜噪聲時已知會被判為noise(self):
        """已知且接受的限制，記在 cluster_documents 的說明裡。

        原本以為這種情況該用 allow_single_cluster=True 解決，但拿
        143 篇人工標註集（6 個真實事件＋74 篇無關負例）回測後發現
        True 在真實的多事件混合資料上表現差得多（全部塌縮成一個
        巨大的簇，ARI 約 -0.05）；False 反而正確切出 6 個乾淨的簇
        （ARI 0.43）。正式環境的輸入是同時含多個事件的批次，不是
        「一個事件＋純噪聲」，因此選 False，代價是這裡示範的情況——
        min_cluster_size 篇緊密文件若在批次中完全孤立，會被判為
        noise。這是可接受的代價：留待下次批次，同案後續報導出現後
        會與其他事件一起被找到。"""
        cluster_a = _blob([1.0, 0.0, 0.0], 8, seed=1)
        rng = np.random.default_rng(99)
        scattered = [list(v / np.linalg.norm(v))
                    for v in rng.normal(size=(5, 3))]
        embeddings = cluster_a + scattered
        ids = list(range(13))

        result = cluster_documents(ids, embeddings, min_cluster_size=5)

        assert result == []

    def test_純噪聲不會被硬湊成一簇(self):
        rng = np.random.default_rng(99)
        scattered = [list(v / np.linalg.norm(v))
                    for v in rng.normal(size=(5, 3))]
        result = cluster_documents(list(range(5)), scattered, min_cluster_size=5)
        assert result == []

    def test_文件數小於門檻直接回傳空清單(self):
        embeddings = _blob([1.0, 0.0, 0.0], 3, seed=1)
        assert cluster_documents([1, 2, 3], embeddings, min_cluster_size=5) == []

    def test_id與向量長度不一致時報錯(self):
        with pytest.raises(ValueError):
            cluster_documents([1, 2, 3], _blob([1.0, 0.0], 2))

    def test_簇標籤由小到大排序且結果穩定(self):
        cluster_a = _blob([1.0, 0.0, 0.0], 6, seed=1)
        cluster_b = _blob([0.0, 1.0, 0.0], 6, seed=2)
        ids = list(range(12))
        r1 = cluster_documents(ids, cluster_a + cluster_b, min_cluster_size=5)
        r2 = cluster_documents(ids, cluster_a + cluster_b, min_cluster_size=5)
        assert [c.label for c in r1] == [c.label for c in r2]
        assert [c.document_ids for c in r1] == [c.document_ids for c in r2]

    def test_空輸入不報錯(self):
        assert cluster_documents([], []) == []
