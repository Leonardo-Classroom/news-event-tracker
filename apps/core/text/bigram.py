"""中文 bigram 切分。

ADR-0001 決定不安裝 zhparser / pg_jieba extension，改以 bigram 切分餵給
PostgreSQL 內建 FTS。代價是自然語言召回品質低於真斷詞（「起訴書」會產生
無意義的「訴書」），換到的是零 extension、零新服務的維運成本。

輸出直接餵給 ``to_tsvector('simple', ...)``，所以主要介面回傳空格分隔字串。
"""
from __future__ import annotations

import re

from ..dates import to_halfwidth

__all__ = ["tokenize", "to_tsvector_input", "is_cjk"]


# CJK 統一漢字（含擴展 A）與注音符號
_CJK_RANGES = (
    ("㐀", "䶿"),   # 擴展 A
    ("一", "鿿"),   # 基本區
    ("豈", "﫿"),   # 相容漢字
)

# 非 CJK 的可索引詞：英文、數字、以及兩者混合（案號的數字部分靠此保留）
_LATIN_TOKEN = re.compile(r"[A-Za-z0-9]+")


def is_cjk(char: str) -> bool:
    """判斷單一字元是否為 CJK 漢字。"""
    return any(low <= char <= high for low, high in _CJK_RANGES)


def tokenize(text: str | None) -> list[str]:
    """把文字切成可索引的 token。

    - 連續 CJK 字元切為 bigram：「起訴書」→ ["起訴", "訴書"]
    - 單一 CJK 字元保留自身：「我」→ ["我"]
    - 英數詞整詞保留：「Covid19」→ ["covid19"]（轉小寫）
    - 標點與空白作為分隔符，不產生 token

    英數不切 bigram，是因為它們本來就有詞界；切了只會製造雜訊。

    >>> tokenize("起訴書")
    ['起訴', '訴書']
    >>> tokenize("111年度")
    ['111', '年度']
    """
    if not text:
        return []

    normalized = to_halfwidth(str(text))
    tokens: list[str] = []
    cjk_run: list[str] = []

    def flush_cjk() -> None:
        if not cjk_run:
            return
        if len(cjk_run) == 1:
            tokens.append(cjk_run[0])
        else:
            tokens.extend(
                cjk_run[i] + cjk_run[i + 1] for i in range(len(cjk_run) - 1)
            )
        cjk_run.clear()

    index = 0
    length = len(normalized)
    while index < length:
        char = normalized[index]

        if is_cjk(char):
            cjk_run.append(char)
            index += 1
            continue

        flush_cjk()

        latin = _LATIN_TOKEN.match(normalized, index)
        if latin:
            tokens.append(latin.group(0).lower())
            index = latin.end()
        else:
            index += 1  # 標點、空白、其他符號：略過

    flush_cjk()
    return tokens


def to_tsvector_input(text: str | None) -> str:
    """回傳可直接餵給 ``to_tsvector('simple', ...)`` 的空格分隔字串。

    使用 'simple' 設定而非語言特定設定，因為我們已自行完成切分，
    不希望 PostgreSQL 再套用英文的 stemming 或 stop word 過濾。
    """
    return " ".join(tokenize(text))
