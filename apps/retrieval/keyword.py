"""關鍵字檢索（ADR-0001）。

**查詢必須與文件經過相同的切分。** 這是 bigram 索引最容易出錯的地方：
文件被切成 bigram 存入索引，查詢字串若原封不動丟給 ``to_tsquery``，
只有恰好等於單一 bigram 的兩字詞會命中，三字以上的詞完全失效。

實測（341 篇真實新聞標題）：
    「起訴」 → 命中 9，正確（剛好是一個 bigram）
    「檢察官」→ 命中 0，實際應為多篇（查詢未切分）
    「警」   → 命中 1，實際 23（單字無法對上 bigram 索引）

介面隔離的用意見 ADR-0001：若 bigram 的召回品質不足，可換成
BGE-M3 的 sparse 輸出（learned lexical weights）而不動呼叫端。
"""
from __future__ import annotations

from typing import Protocol

from django.db.models import QuerySet

from apps.core.text.bigram import tokenize

__all__ = ["KeywordSearchBackend", "BigramFtsBackend", "build_tsquery"]


def build_tsquery(query: str, *, operator: str = "&") -> str:
    """把查詢字串切成與索引相同的 bigram，組成 tsquery 表達式。

    Args:
        operator: ``&`` 要求全部 bigram 都出現（精確），
                  ``|`` 任一出現即可（寬鬆，召回優先）。

    >>> build_tsquery("檢察官")
    '檢察 & 察官'
    >>> build_tsquery("起訴")
    '起訴'
    """
    tokens = tokenize(query)
    if not tokens:
        return ""
    # tsquery 的特殊字元需排除，避免查詢字串造成語法錯誤
    safe = [t for t in tokens if t.isalnum() or not t.isascii()]
    return f" {operator} ".join(safe)


class KeywordSearchBackend(Protocol):
    def search(self, queryset: QuerySet, query: str, *, mode: str = "all") -> QuerySet: ...


class BigramFtsBackend:
    """PostgreSQL 內建 FTS + bigram 切分。

    已知限制：**單一中文字的查詢無法命中**。索引中的 token 是 bigram，
    單字只有在整段 CJK 只有一個字時才會成為獨立 token。這是 bigram
    索引的固有性質，無法靠調整查詢解決。實務影響有限——單字查詢的
    精確度本來就低——但呼叫端若需支援，應改走 ILIKE 或改用 sparse 向量。
    """

    def search(self, queryset: QuerySet, query: str, *, mode: str = "all") -> QuerySet:
        expression = build_tsquery(query, operator="&" if mode == "all" else "|")
        if not expression:
            return queryset.none()
        # **欄位必須限定表名。** Document 有指向自身的 canonical_of，
        # 任何 join（如 admin 的 list_select_related）都會讓未限定的
        # search_vector 變成 "column reference is ambiguous"——
        # 而該錯誤只在有 join 時出現，單獨查詢完全正常。
        table = queryset.model._meta.db_table
        return queryset.extra(                                  # noqa: S610
            where=[f'"{table}"."search_vector" @@ to_tsquery(\'simple\', %s)'],
            params=[expression],
        )
