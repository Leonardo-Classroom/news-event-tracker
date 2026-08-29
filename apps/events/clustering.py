"""事件偵測：HDBSCAN 叢集未歸屬文件（任務 24，ADR-0005 第二階段）。

ADR-0005 的兩階段偵測：先嘗試歸屬既有事件（任務 23 的 ``assignment``
模組），未歸屬者才叢集出新事件候選。本模組只做第二階段。

用 scikit-learn 內建的 ``HDBSCAN``（1.3+ 版本內建，不需要額外安裝
``hdbscan`` 套件），理由單純是少一個相依套件。

## noise 點不產生事件，且保留待下次批次

HDBSCAN 的 ``label == -1`` 代表「不屬於任何密度夠高的簇」。這不等於
「與任何事件都無關」——同案的第二篇報導可能要等幾天才出現，屆時
再跑一次，這篇文件就會被含入某個簇。因此 noise 點的正確處理方式是
「這次不處理」，不是「判定為無關」，呼叫端不該把它們標記為已檢視。

## 為何直接用歐氏距離，不轉算餘弦距離矩陣

BGE-M3 的輸出已正規化為單位向量（ADR-0004）。對單位向量而言，
歐氏距離的平方等於 `2 - 2*cosine`，兩者是嚴格遞增的關係——用歐氏
距離排出的鄰居順序與用餘弦距離排出的完全相同。sklearn 的 HDBSCAN
對歐氏距離有較快的樹狀索引可用，餘弦距離則退化成暴力法，
在正規化向量上沒有理由選較慢的那個。
"""
from __future__ import annotations

import dataclasses

import numpy as np
from sklearn.cluster import HDBSCAN

__all__ = ["ClusterResult", "cluster_documents"]


@dataclasses.dataclass(frozen=True)
class ClusterResult:
    label: int
    document_ids: tuple[int, ...]


def cluster_documents(
    document_ids: list[int], embeddings: list[list[float]], *,
    min_cluster_size: int = 5, min_samples: int | None = None,
) -> list[ClusterResult]:
    """對一批文件的向量做 HDBSCAN 叢集，回傳非 noise 的簇。

    Args:
        min_cluster_size: 一個簇最少要有幾篇文件才成立事件候選。
            規格暫訂事件門檻「3 家媒體、5 篇」，這裡先用 5 作為預設，
            實際數值待任務 25 以歷史語料回測校準。
    """
    if len(document_ids) != len(embeddings):
        raise ValueError("document_ids 與 embeddings 長度不一致")
    if len(document_ids) < min_cluster_size:
        return []

    matrix = np.asarray(embeddings, dtype=np.float32)
    clusterer = HDBSCAN(
        min_cluster_size=min_cluster_size, min_samples=min_samples, metric="euclidean",
        # **這個參數的正確值不是靠推理得出的，是靠任務 25 的回測反轉的。**
        #
        # 最初以為該設 True：sklearn 預設 False 時，若候選池裡只有一個
        # 真正密集的群、其餘全是噪聲，演算法找不到「該分成兩支」的理由，
        # 會把整批（包含那個真實的簇）都判為 noise——用 8 篇緊密文件混
        # 5 篇不相干文件的合成資料驗證過這個失效模式，設 True 後能抓出
        # 其中 6 篇。
        #
        # 但拿 143 篇人工標註集（6 個真實事件 + 74 篇無關負例）回測後
        # 發現 True 在真實混合資料上表現差得多：不分 min_cluster_size
        # 3／5／8，全都把 143 篇裡的大多數合併成單一個巨大的簇
        # （ARI 約 -0.05 至 -0.07，等同亂猜）。改回 False、
        # min_cluster_size=5 反而正確切出 6 個乾淨的簇，其中 3 個
        # 分別對應到新竹棒球場（20/21 正確）、超思進口蛋（13/14）、
        # 京華城（分裂成兩個子簇共 24/30）——ARI 0.43。
        #
        # 原因：正式環境的輸入是**同時含有多個真實事件的批次**（全庫
        # 未歸屬文件），不是「一個事件＋純噪聲」——後者是我最初設想的
        # 情境，但不是實際會發生的輸入形狀。True 為了保住那個少見情境
        # 付出的代價，是在真正常見的多事件情境下完全失效。
        #
        # 代價（已知且接受）：min_cluster_size 篇緊密文件若在批次中
        # 完全孤立、沒有其他候選事件同時存在，會被判為 noise 而非新
        # 事件候選。留待下次批次——同案後續報導出現後，重跑就會與其他
        # 事件一起被找到。
        allow_single_cluster=False,
    )
    labels = clusterer.fit_predict(matrix)

    clusters: dict[int, list[int]] = {}
    for doc_id, label in zip(document_ids, labels):
        if label == -1:
            continue
        clusters.setdefault(int(label), []).append(doc_id)

    return [ClusterResult(label=label, document_ids=tuple(ids))
            for label, ids in sorted(clusters.items())]
