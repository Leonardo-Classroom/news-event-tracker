"""轉載偵測與歸併（ADR-0012）。

以 bigram 集合的 Jaccard 相似度判定，而非 SimHash——後者實測與真實
相似度幾乎無關（詳見 ADR-0012 的實測數據）。

**必須守住的區分：**

- **轉載**（同文字、不同媒體）→ 歸併。不歸併會虛增媒體家數。
- **同事件獨立報導**（不同文字、同事件）→ **不可歸併**。
  那正是 ADR-0005「≥3 家不同媒體」門檻要的訊號；誤併會直接
  摧毀事件偵測的依據。

門檻 0.70 落在兩者之間，且刻意偏保守：漏抓轉載只是多存一份文件，
誤併卻會讓一個真實事件失去被偵測到的機會。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from django.db.models import Q
from django.utils import timezone

from apps.core.text.bigram import tokenize
from apps.ingest.models import Document

logger = logging.getLogger(__name__)

__all__ = ["jaccard", "find_canonical", "dedupe_recent", "DEFAULT_THRESHOLD",
           "DEFAULT_WINDOW_HOURS"]

#: 實測分離點（ADR-0012）。≥0.7 為轉載，0.4–0.7 為同事件不同寫法。
#: 須在 34 萬篇歷史回填後以更大語料複驗。
DEFAULT_THRESHOLD = 0.70

#: 轉載幾乎都在原稿發布後數小時至數日內出現。限縮候選以避免全語料兩兩比對。
DEFAULT_WINDOW_HOURS = 72

#: 內文過短時 Jaccard 波動大，不做判定
MIN_TOKENS = 40


def jaccard(left: set[str], right: set[str]) -> float:
    """兩個 token 集合的 Jaccard 相似度。任一為空時回傳 0。"""
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _tokens(document: Document) -> set[str]:
    """文件的 bigram 集合。僅取內文——標題的措辭在轉載時常被改寫。"""
    return set(tokenize(document.raw_body))


@dataclass
class DedupResult:
    examined: int = 0
    linked: int = 0
    pairs: list[tuple[int, int, float]] = field(default_factory=list)


def find_canonical(
    document: Document,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> tuple[Document, float] | None:
    """找出此文件的首發版本。找不到時回傳 ``None``。

    歸併方向以發布時間為準：較早者為首發。時間相同時以 id 較小者為準，
    確保重複執行的結果穩定（冪等）。
    """
    tokens = _tokens(document)
    if len(tokens) < MIN_TOKENS:
        return None

    anchor = document.published_at or document.fetched_at
    window_start = anchor - dt.timedelta(hours=window_hours)
    window_end = anchor + dt.timedelta(hours=window_hours)

    candidates = (
        Document.objects.select_related("source")
        .exclude(pk=document.pk)
        .exclude(raw_body="")
        .filter(canonical_of__isnull=True)          # 只跟首發版本比，避免鏈狀歸併
        .filter(
            Q(published_at__range=(window_start, window_end))
            | Q(published_at__isnull=True, fetched_at__range=(window_start, window_end))
        )
    )

    best: tuple[Document, float] | None = None
    for candidate in candidates:
        score = jaccard(tokens, _tokens(candidate))
        if score < threshold:
            continue
        if best is None or score > best[1]:
            best = (candidate, score)

    if best is None:
        return None

    canonical, score = best
    if _is_earlier(document, canonical):
        # 本文較早——不歸併自己，改由對方指向本文（由呼叫端處理）
        return None
    return canonical, score


def _is_earlier(a: Document, b: Document) -> bool:
    ta = a.published_at or a.fetched_at
    tb = b.published_at or b.fetched_at
    if ta != tb:
        return ta < tb
    return a.pk < b.pk


def dedupe_recent(
    *,
    hours: int = DEFAULT_WINDOW_HOURS,
    threshold: float = DEFAULT_THRESHOLD,
    dry_run: bool = False,
) -> DedupResult:
    """對近期文件執行轉載歸併。冪等——已歸併者會被跳過。"""
    since = timezone.now() - dt.timedelta(hours=hours)
    queryset = (
        Document.objects.select_related("source")
        .exclude(raw_body="")
        .filter(canonical_of__isnull=True)
        .filter(Q(published_at__gte=since) | Q(published_at__isnull=True,
                                               fetched_at__gte=since))
        .order_by("published_at", "pk")             # 先處理較早者，方向才穩定
    )

    result = DedupResult()
    for document in queryset:
        result.examined += 1
        document.refresh_from_db(fields=["canonical_of"])
        if document.canonical_of_id:
            continue                                # 已於本輪稍早被歸併

        match = find_canonical(document, threshold=threshold, window_hours=hours)
        if match is None:
            continue

        canonical, score = match
        result.pairs.append((document.pk, canonical.pk, score))
        result.linked += 1
        if not dry_run:
            document.canonical_of = canonical
            document.save(update_fields=["canonical_of"])
        logger.info(
            "轉載歸併：#%d（%s）→ #%d（%s），Jaccard %.3f",
            document.pk, document.source.slug,
            canonical.pk, canonical.source.slug, score,
        )

    return result
