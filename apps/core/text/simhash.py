"""SimHash 近似去重。

同一則通稿常被多家媒體轉載，內容幾乎相同但標題與細節有差異。
URL 去重抓不到這種情況，逐字比對又太慢。SimHash 產生 64 位元指紋，
相似文本的指紋漢明距離小，可用整數運算快速篩選。

去重後以 ``Document.canonical_of`` 指向首發版本（規格 §4.1）。
這對事件偵測特別重要——ADR-0005 的門檻是「≥3 家不同媒體」，
若轉載未被歸併，一則通稿就會被誤判為跨媒體的重大事件。
"""
from __future__ import annotations

import hashlib
from collections import Counter

from .bigram import tokenize

__all__ = ["simhash", "hamming_distance", "is_near_duplicate", "DEFAULT_THRESHOLD"]

_BITS = 64
_MASK = (1 << _BITS) - 1

# 門檻經實測校準（非猜測）。以一則典型的檢調新聞為基準，各情境的漢明距離：
#
#   完全相同        0        大幅改寫       29
#   輕度改寫        5        不同主題       29
#   僅改標題前綴     7
#   加編按／刪末句   8
#
# ≤8 與 29 之間有乾淨的分界，取 10 留出餘裕。
#
# ⚠️ 已知限制：**大幅改寫（29）與完全不同主題（29）無法區分。**
# SimHash 只能捕捉逐字或近逐字的轉載，對重寫稿本質上無能為力，
# 且無法靠調高門檻解決——那會把不相干的文章一併誤判為重複。
# 重寫稿的處理留給 L2 的向量相似度與 L3 的事件歸屬，那裡本來就會做。
#
# 這對 ADR-0005 的偵測門檻有一個推論：「≥3 家不同媒體」可能被
# 改寫過的通稿灌水。真正防止通稿灌水的是「跨 ≥2 天」這個條件——
# 同一則通稿的轉載幾乎都落在同一天。
#
# 本數值以單一文本對校準，需在 Scope 1 任務 12 回填後以真實語料複驗。
DEFAULT_THRESHOLD = 10


def _feature_hash(token: str) -> int:
    """將 token 雜湊為 64 位元整數。

    用 blake2b 而非內建 hash()，因為後者受 PYTHONHASHSEED 影響，
    跨程序不一致——指紋要存進資料庫長期比對，必須是決定性的。
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash(text: str | None) -> int:
    """計算文本的 64 位元 SimHash 指紋。空文本回傳 0。"""
    if not text:
        return 0

    weights = Counter(tokenize(text))
    if not weights:
        return 0

    columns = [0] * _BITS
    for token, weight in weights.items():
        token_hash = _feature_hash(token)
        for bit in range(_BITS):
            if token_hash >> bit & 1:
                columns[bit] += weight
            else:
                columns[bit] -= weight

    fingerprint = 0
    for bit in range(_BITS):
        if columns[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint & _MASK


def hamming_distance(left: int, right: int) -> int:
    """兩個指紋的漢明距離（相異位元數）。"""
    return ((left ^ right) & _MASK).bit_count()


def is_near_duplicate(
    left: int, right: int, threshold: int = DEFAULT_THRESHOLD
) -> bool:
    """判斷兩個指紋是否近似重複。

    指紋為 0（空文本）時一律回傳 False——空文本之間不構成「轉載」關係，
    否則所有解析失敗的文件會被歸併成同一份。
    """
    if left == 0 or right == 0:
        return False
    return hamming_distance(left, right) <= threshold
