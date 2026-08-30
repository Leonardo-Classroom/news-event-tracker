"""建立節點／邊的服務函式，與多跳因果查詢（任務 28-31）。

## 為何節點與邊的建立要走這裡而非直接 ``Model.objects.create``

``TimelineNode`` 的引用是 NOT NULL 欄位，資料庫本身就會拒絕沒有出處
的節點——但 ``CausalEdge`` 的措辭合規性（stated 邊的敘述是否符合無罪
推定）無法用 CHECK constraint 表達，需要在寫入前呼叫
``apps.compliance.wording.check_wording``。這裡把「建立 + 檢查」包成
一個函式，讓呼叫端沒有機會漏掉檢查那一步。

## 遞迴 CTE 的深度上限

ADR-0003：「多跳查詢用遞迴 CTE 並設深度上限（避免環狀因果導致無限
遞迴）」。正常的因果圖應該是 DAG，但**不能假設輸入永遠正確**——
LLM 生成的因果邊、未來的人工編輯，都可能意外造成環。深度上限與
SQL 裡以陣列追蹤已走訪節點是兩層防護，任一層失效另一層仍能擋住。
"""
from __future__ import annotations

import dataclasses

from django.db import connection

from apps.compliance.wording import WordingCheckResult, check_wording
from apps.timeline.models import CausalEdge, CausalKind, TimelineNode

__all__ = [
    "WordingRejected", "create_timeline_node", "create_causal_edge",
    "causal_descendants", "causal_ancestors", "DEFAULT_MAX_DEPTH",
]

#: ADR-0003：「深度通常 < 5」，取 5 作為預設上限。
DEFAULT_MAX_DEPTH = 5


class WordingRejected(ValueError):
    """措辭檢查未通過，拒絕建立節點或邊。"""

    def __init__(self, result: WordingCheckResult):
        self.result = result
        reasons = "；".join(v.reason for v in result.violations)
        super().__init__(f"措辭檢查未通過：{reasons}")


def create_timeline_node(
    *, event, summary: str, citation_document, is_final: bool = False,
    occurred_on=None, citation_excerpt: str = "", generation=None,
) -> TimelineNode:
    """建立時間線節點，寫入前先過措辭檢查器（blocking gate，不可繞過）。"""
    result = check_wording(summary, is_final=is_final)
    if not result.passed:
        raise WordingRejected(result)
    return TimelineNode.objects.create(
        event=event, summary=summary, citation_document=citation_document,
        occurred_on=occurred_on, citation_excerpt=citation_excerpt,
        generation=generation,
    )


def create_causal_edge(
    *, event, from_node: TimelineNode, to_node: TimelineNode, kind: str,
    is_final: bool = False, citation_document=None, citation_excerpt: str = "",
    confidence: float | None = None, generation=None,
    publicly_visible: bool | None = None,
) -> CausalEdge:
    """建立因果邊。stated 邊需要 citation_document，inferred 邊需要
    confidence——這兩項資料庫層已用 CHECK constraint 強制，這裡提早
    在 Python 層擋下，讓錯誤訊息更明確（資料庫的 constraint 違反
    訊息不會告訴你「這是因為 kind=stated 卻沒給引用」，只會說
    constraint 名稱）。

    Args:
        publicly_visible: 不傳時依 ``kind`` 決定預設值——stated 邊
            預設 True（陳述的是報導明說、且強制附引用的事實，沒有
            「需要人工先看過才能公開」的理由），inferred 邊預設 False
            （規格 M9、任務 42：inferred 因果邊預設不公開，須人工
            逐條勾選才顯示）。傳入明確值可覆寫——例如某個 stated 邊
            引用的文件之後被下架申訴撤下，仍要能把它設為不公開。
    """
    if kind == CausalKind.STATED and citation_document is None:
        raise ValueError("stated 邊必須提供 citation_document")
    if kind == CausalKind.INFERRED and confidence is None:
        raise ValueError("inferred 邊必須提供 confidence")
    if from_node.event_id != event.id or to_node.event_id != event.id:
        raise ValueError("因果邊的兩端節點必須屬於同一事件（ADR-0003：不建全庫圖）")

    if kind == CausalKind.STATED:
        text = f"{from_node.summary} → {to_node.summary}"
        result = check_wording(text, is_final=is_final)
        if not result.passed:
            raise WordingRejected(result)

    if publicly_visible is None:
        publicly_visible = kind == CausalKind.STATED

    return CausalEdge.objects.create(
        event=event, from_node=from_node, to_node=to_node, kind=kind,
        citation_document=citation_document, citation_excerpt=citation_excerpt,
        confidence=confidence, generation=generation,
        publicly_visible=publicly_visible,
    )


@dataclasses.dataclass(frozen=True)
class GraphStep:
    edge_id: int
    from_node_id: int
    to_node_id: int
    kind: str
    depth: int


def _walk(start_node_id: int, *, max_depth: int, direction: str) -> list[GraphStep]:
    table = CausalEdge._meta.db_table
    if direction == "descendants":
        seed_col, next_col, walk_col = "from_node_id", "from_node_id", "to_node_id"
    else:
        seed_col, next_col, walk_col = "to_node_id", "to_node_id", "from_node_id"

    # 已走訪節點以陣列追蹤——防環的第二層，深度上限是第一層。
    # 表名直接來自 model._meta.db_table（非使用者輸入），可安全內插。
    sql = f"""
        WITH RECURSIVE walk AS (
            SELECT id, from_node_id, to_node_id, kind, 1 AS depth,
                   ARRAY[{seed_col}, {walk_col}] AS visited
            FROM {table}
            WHERE {seed_col} = %(start)s
            UNION ALL
            SELECT e.id, e.from_node_id, e.to_node_id, e.kind, w.depth + 1,
                   w.visited || e.{walk_col}
            FROM {table} e
            JOIN walk w ON e.{next_col} = w.{walk_col}
            WHERE w.depth < %(max_depth)s
              AND NOT e.{walk_col} = ANY(w.visited)
        )
        SELECT id, from_node_id, to_node_id, kind, depth
        FROM walk ORDER BY depth, id
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, {"start": start_node_id, "max_depth": max_depth})
        rows = cursor.fetchall()
    return [GraphStep(edge_id=r[0], from_node_id=r[1], to_node_id=r[2],
                      kind=r[3], depth=r[4]) for r in rows]


def causal_descendants(start_node_id: int, *, max_depth: int = DEFAULT_MAX_DEPTH) -> list[GraphStep]:
    """從 ``start_node_id`` 出發，沿因果邊向下游走訪（這件事的後續影響）。"""
    return _walk(start_node_id, max_depth=max_depth, direction="descendants")


def causal_ancestors(start_node_id: int, *, max_depth: int = DEFAULT_MAX_DEPTH) -> list[GraphStep]:
    """從 ``start_node_id`` 出發，沿因果邊向上游走訪（這件事的成因）。"""
    return _walk(start_node_id, max_depth=max_depth, direction="ancestors")
