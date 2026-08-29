"""混合召回（任務 19、ADR-0005 第一階段）。

三個通道各自有它單獨做不到的事：

    識別碼   案號、統一編號。命中即確定，但多數新聞不寫案號。
    關鍵字   人名、公司名、專有名詞的精確比對。改寫過的措辭會漏。
    向量     語意相近。「北檢起訴前市長」與「柯文哲遭訴」能對上，
             但同時也把「其他前市長的其他案子」拉進來。

**召回率的量測優先於準確率（ADR-0005）。** 這不是偏好問題，是流程的
不對稱：本階段的輸出交給第二階段的 LLM 歸屬，LLM 可以否決一個錯誤的
候選，卻永遠救不回一篇根本沒被撈出來的文件。漏掉就是永久漏掉。

融合用 Reciprocal Rank Fusion 而非分數正規化——見 ``fuse`` 的說明。
"""
from __future__ import annotations

import dataclasses
from typing import Iterable, Sequence

from django.db.models import Q, QuerySet
from pgvector.django import CosineDistance

from apps.ingest.models import Document
from apps.retrieval.embedding import build_embedding_text, embed_texts
from apps.retrieval.keyword import BigramFtsBackend

__all__ = ["Candidate", "HybridRetriever", "fuse", "RRF_K"]

#: RRF 的平滑常數。60 是原論文（Cormack et al., 2009）的建議值，
#: 作用是讓第 1 名與第 2 名的差距不至於壓過其他通道的意見——
#: k 越小越信任單一通道的頭部，越大越接近各通道等權投票。
RRF_K = 60


@dataclasses.dataclass
class Candidate:
    """一個召回候選。

    ``channels`` 保留是哪些通道撈到的，不只是為了除錯：第二階段的
    LLM 歸屬提示會用到它——「三個通道都撈到」與「只有向量撈到」
    是不同強度的證據，值得讓 LLM 知道。
    """

    document_id: int
    score: float
    channels: dict[str, int] = dataclasses.field(default_factory=dict)
    pinned: bool = False

    @property
    def channel_names(self) -> str:
        return "+".join(sorted(self.channels))


def fuse(rankings: dict[str, Sequence[int]], *, k: int = RRF_K) -> list[Candidate]:
    """Reciprocal Rank Fusion：score = Σ 1/(k + rank)。

    **為什麼不把各通道的分數正規化後加權平均。** ts_rank 與 cosine
    distance 不只是尺度不同，連分布形狀都不同，而且同一個通道在不同
    查詢下的分數分布也不一樣——「案號查詢」的 ts_rank 全都很高，
    「人名查詢」的普遍偏低。要正規化就得先知道每個查詢的分數分布，
    那是比融合本身更難的問題。RRF 只看名次，繞開整個問題。

    代價是丟掉了分數的絕對強度：一篇 cosine 0.95 與一篇 0.55 若同為
    向量通道第 1 名，融合後無法區分。這個代價可以接受，因為本階段
    只需產生候選集，強度判斷交給第二階段。
    """
    merged: dict[int, Candidate] = {}
    for channel, ids in rankings.items():
        for rank, doc_id in enumerate(ids, start=1):
            candidate = merged.get(doc_id)
            if candidate is None:
                candidate = merged[doc_id] = Candidate(document_id=doc_id, score=0.0)
            candidate.score += 1.0 / (k + rank)
            candidate.channels[channel] = rank
    # pinned 優先，其次分數，最後以 id 讓排序穩定（相同分數時結果可重現）
    return sorted(merged.values(), key=lambda c: (-c.pinned, -c.score, c.document_id))


class HybridRetriever:
    """對單一查詢做三通道召回並融合。

    查詢可以是一段自由文字（事件標題、核心詞），也可以附帶已知的
    識別碼。識別碼走精確比對，不進 RRF——見 ``search``。
    """

    def __init__(self, *, keyword_backend=None, per_channel: int = 500):
        self.keyword = keyword_backend or BigramFtsBackend()
        #: 每個通道各取多少候選送進融合。
        #:
        #: **這個數字與 ``top_k`` 是兩件事，不該一起調。** 初版設 100，
        #: 因為誤以為它控制第二階段的成本。實測京華城案：910 篇命中
        #: 「京華城」，30 篇正例**全部在命中集合內**，名次散布在
        #: 1 到 440——第 100 名截斷只撈得到 12 篇，召回率 13%。
        #:
        #: 通道取候選是索引掃描，便宜；貴的是融合之後送進 LLM 歸屬的
        #: 那一批。成本該在 ``top_k`` 控制，不是在這裡。取太少還會讓
        #: 融合本身失去意義——兩個通道各取 100 篇，在 23.8 萬篇語料裡
        #: 可能毫無重疊，RRF 就退化成兩份清單接在一起。
        self.per_channel = per_channel

    # ------------------------------------------------------------ 各通道

    def by_identifier(self, base: QuerySet, *,
                      case_numbers: Iterable[str] = (),
                      tax_ids: Iterable[str] = ()) -> list[int]:
        """識別碼精確比對。命中即確定。

        用 ArrayField 的 overlap 查詢，走 GIN 索引。
        """
        case_numbers = [c for c in case_numbers if c]
        tax_ids = [t for t in tax_ids if t]
        if not case_numbers and not tax_ids:
            return []
        condition = Q()
        if case_numbers:
            condition |= Q(case_numbers__overlap=list(case_numbers))
        if tax_ids:
            condition |= Q(tax_ids__overlap=list(tax_ids))
        return list(base.filter(condition)
                    .order_by("-published_at")
                    .values_list("pk", flat=True)[:self.per_channel])

    def by_keyword(self, base: QuerySet, query: str) -> list[int]:
        """關鍵字通道。用 ``|``（任一 bigram 命中）而非 ``&``。

        這裡刻意選寬鬆模式。查詢通常是事件的核心詞組合
        （如「柯文哲 京華城 容積」），要求全部 bigram 同時出現會讓
        「只提到柯文哲與容積、沒寫京華城」的報導落榜——而那正是
        事件中期最常見的報導形態。精確度的損失由 RRF 的名次稀釋，
        以及第二階段的 LLM 承擔。

        **必須依 ts_rank 排序，不能依時間。** 初版寫成
        ``order_by("-published_at")[:100]``，量測出來的召回率是
        11.4%——寬鬆查詢在 23.8 萬篇語料裡命中數千篇，取「最新的
        100 篇」等於取一批近期新聞，京華城案 30 篇正例全數落榜。
        寬鬆模式與依相關性排序是配套的：放寬命中條件的前提，是
        排序能把「命中多個核心詞」的文件推到前面。
        """
        if not query.strip():
            return []
        matched = self.keyword.search(base, query, mode="any")
        return list(self.keyword.rank(matched, query)
                    .values_list("pk", flat=True)[:self.per_channel])

    def by_vector(self, base: QuerySet, query: str) -> list[int]:
        """向量通道。cosine 距離，走 HNSW 索引（ADR-0004）。

        只查有向量的文件——尚未向量化者在此通道不存在，但仍可能由
        其他通道撈到。這是刻意的：向量化是背景作業，不該讓「還沒
        輪到」變成「查不到」。
        """
        if not query.strip():
            return []
        vector = embed_texts([build_embedding_text(query, "")])[0]
        return list(base.exclude(embedding=None)
                    .order_by(CosineDistance("embedding", vector))
                    .values_list("pk", flat=True)[:self.per_channel])

    # ------------------------------------------------------------ 融合

    def search(self, query, *,
               case_numbers: Iterable[str] = (),
               tax_ids: Iterable[str] = (),
               base: QuerySet | None = None,
               top_k: int = 50,
               channels: Sequence[str] = ("keyword", "vector")) -> list[Candidate]:
        """多通道召回並融合，回傳前 ``top_k`` 個候選。

        Args:
            query: 自由文字，或 ``EventQuery``——後者的身分詞與涉案人
                會各自成為一個關鍵字通道，而非併成一個詞袋。原因見
                ``EventQuery`` 的說明（併成一袋會讓人名的數量淹沒
                身分詞，京華城案實測 16/30 對 27/30）。

        **識別碼命中的文件被置頂，不參與 RRF。** 一篇引用了
        「113年度金訴字第51號」的文件就是在講那個案子，不該因為語意
        分數低而被任何東西擠下去；反過來，沒寫案號也不該被扣分——
        多數新聞根本不寫案號。把它當成 RRF 的第三票會同時犯這兩個錯。
        ADR-0005 的「識別碼精確比對優先」指的就是這件事。
        """
        base = base if base is not None else Document.objects.all()
        # 轉載重複的文件不進召回：同一則稿子被三家轉載會佔掉三個名額，
        # 而 canonical 已保留原始那篇（ADR-0012）
        base = base.filter(canonical_of__isnull=True)

        pinned_ids = self.by_identifier(base, case_numbers=case_numbers, tax_ids=tax_ids)

        rankings: dict[str, list[int]] = {}
        if "keyword" in channels:
            for name, terms in self._keyword_channels(query):
                rankings[name] = self.by_keyword(base, terms)
        if "vector" in channels:
            # 向量通道用完整查詢：語意編碼本來就在處理整段文字，
            # 拆開反而失去「這些人與這件事一起出現」的語境
            rankings["vector"] = self.by_vector(base, str(query))

        results = fuse(rankings)
        pinned_set = set(pinned_ids)
        by_id = {c.document_id: c for c in results}
        for rank, doc_id in enumerate(pinned_ids, start=1):
            candidate = by_id.get(doc_id)
            if candidate is None:
                candidate = Candidate(document_id=doc_id, score=0.0)
                results.append(candidate)
            candidate.pinned = True
            candidate.channels["identifier"] = rank

        results.sort(key=lambda c: (-c.pinned, -c.score, c.document_id))
        return results[:max(top_k, len(pinned_set))]

    @staticmethod
    def _keyword_channels(query) -> list[tuple[str, str]]:
        """把查詢拆成各自獨立排序的關鍵字通道。

        自由文字只有一個通道；``EventQuery`` 拆成身分與涉案人兩個。
        """
        identity = getattr(query, "identity", None)
        if identity is None:
            return [("keyword", str(query))]
        channels = [("identity", identity)] if identity else []
        support = " ".join(query.support)
        if support:
            channels.append(("support", support))
        return channels
