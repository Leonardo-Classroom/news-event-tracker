"""時間線與因果子圖測試（任務 28-31）。

DB CHECK constraint 的測試特別重要——它們是規格 M9 的實際保證，
必須驗證**繞不過**：即使程式碼有 bug 忘記檢查，資料庫仍會拒絕。
"""
import datetime as dt

import pytest
from django.db import IntegrityError, transaction

from apps.events.models import Event
from apps.timeline.graph import (
    DEFAULT_MAX_DEPTH, WordingRejected, causal_ancestors, causal_descendants,
    create_causal_edge, create_timeline_node,
)
from apps.timeline.models import CausalEdge, CausalKind, TimelineNode

UTC = dt.timezone.utc


@pytest.fixture
def event(db):
    return Event.objects.create(slug="e", title="測試事件")


@pytest.fixture
def doc(source):
    from apps.ingest.models import Document

    return Document.objects.create(
        source=source, url="https://t.test/1", title="標題",
        raw_body="檢方於2026年1月5日就圖利罪嫌起訴被告", content_class=source.content_class,
    )


@pytest.mark.medium
class TestCreateTimelineNode:
    def test_合規敘述可建立(self, event, doc):
        node = create_timeline_node(
            event=event, summary="檢方就圖利罪嫌起訴被告",
            citation_document=doc, is_final=False,
        )
        assert node.citation_document == doc

    def test_違規措辭被拒(self, event, doc):
        with pytest.raises(WordingRejected):
            create_timeline_node(
                event=event, summary="被告貪污犯定讞", citation_document=doc,
                is_final=False,
            )
        assert TimelineNode.objects.count() == 0

    def test_未提供出處資料庫層拒絕(self, event, db):
        """M5 的資料庫層保證：citation_document 是 NOT NULL 外鍵，
        即使繞過 create_timeline_node 直接呼叫 ORM 也擋不掉這關。"""
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TimelineNode.objects.create(event=event, summary="無出處的敘述")


@pytest.mark.medium
class TestCreateCausalEdge:
    @pytest.fixture
    def two_nodes(self, event, doc):
        a = create_timeline_node(event=event, summary="檢方複訊被告", citation_document=doc)
        b = create_timeline_node(event=event, summary="檢方起訴被告", citation_document=doc)
        return a, b

    def test_stated邊需要引用(self, event, two_nodes):
        a, b = two_nodes
        with pytest.raises(ValueError, match="citation_document"):
            create_causal_edge(event=event, from_node=a, to_node=b, kind=CausalKind.STATED)

    def test_inferred邊需要confidence(self, event, two_nodes):
        a, b = two_nodes
        with pytest.raises(ValueError, match="confidence"):
            create_causal_edge(event=event, from_node=a, to_node=b, kind=CausalKind.INFERRED)

    def test_stated邊提供引用後可建立(self, event, two_nodes, doc):
        a, b = two_nodes
        edge = create_causal_edge(
            event=event, from_node=a, to_node=b, kind=CausalKind.STATED,
            citation_document=doc, citation_excerpt="複訊後檢方認定犯嫌重大予以起訴",
        )
        assert edge.kind == CausalKind.STATED

    def test_inferred邊提供confidence後可建立(self, event, two_nodes):
        a, b = two_nodes
        edge = create_causal_edge(
            event=event, from_node=a, to_node=b, kind=CausalKind.INFERRED,
            confidence=0.7,
        )
        assert edge.confidence == 0.7

    def test_stated邊預設公開(self, event, two_nodes, doc):
        """陳述的是報導明說、且強制附引用的事實，沒有『需要人工先看
        過才能公開』的理由——審核瓶頸該花在真正需要判斷的地方。"""
        a, b = two_nodes
        edge = create_causal_edge(
            event=event, from_node=a, to_node=b, kind=CausalKind.STATED,
            citation_document=doc,
        )
        assert edge.publicly_visible is True

    def test_inferred邊預設不公開(self, event, two_nodes):
        """任務 42、規格 M9：inferred 因果邊預設不公開，
        須人工逐條勾選才顯示。"""
        a, b = two_nodes
        edge = create_causal_edge(
            event=event, from_node=a, to_node=b, kind=CausalKind.INFERRED,
            confidence=0.7,
        )
        assert edge.publicly_visible is False

    def test_可明確覆寫預設公開狀態(self, event, two_nodes, doc):
        """例如 stated 邊引用的文件之後被下架申訴撤下，
        仍要能把它設為不公開。"""
        a, b = two_nodes
        edge = create_causal_edge(
            event=event, from_node=a, to_node=b, kind=CausalKind.STATED,
            citation_document=doc, publicly_visible=False,
        )
        assert edge.publicly_visible is False

    def test_資料庫CHECK繞不過(self, event, two_nodes):
        """M9 的核心保證：即使繞過 create_causal_edge 直接用 ORM 建立，
        資料庫仍拒絕沒有引用的 stated 邊——這就是選擇 CHECK constraint
        而非只在應用層檢查的理由（ADR-0003）。"""
        a, b = two_nodes
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                CausalEdge.objects.create(
                    event=event, from_node=a, to_node=b, kind=CausalKind.STATED,
                )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                CausalEdge.objects.create(
                    event=event, from_node=a, to_node=b, kind=CausalKind.INFERRED,
                )

    def test_不可自我連邊(self, event, two_nodes):
        a, _ = two_nodes
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                CausalEdge.objects.create(
                    event=event, from_node=a, to_node=a, kind=CausalKind.INFERRED,
                    confidence=0.5,
                )

    def test_不可跨事件連邊(self, two_nodes, doc):
        a, b = two_nodes
        other_event = Event.objects.create(slug="other", title="另一事件")
        with pytest.raises(ValueError, match="同一事件"):
            create_causal_edge(
                event=other_event, from_node=a, to_node=b, kind=CausalKind.STATED,
                citation_document=doc,
            )

    def test_合規敘述可建立stated邊(self, event, doc):
        a = create_timeline_node(event=event, summary="檢方複訊被告", citation_document=doc)
        b = create_timeline_node(event=event, summary="被告遭起訴", citation_document=doc)
        edge = create_causal_edge(
            event=event, from_node=a, to_node=b, kind=CausalKind.STATED,
            citation_document=doc, is_final=False,
        )
        assert edge.kind == CausalKind.STATED

    def test_stated邊的敘述也過措辭檢查(self, event, doc):
        """節點各自的措辭在建立時已檢查過，但兩節點拼成因果敘述
        （from → to）是另一段新文字，仍須重新檢查——單獨看起來合規的
        兩句話拼在一起未必仍然合規。"""
        a = create_timeline_node(event=event, summary="檢方複訊被告", citation_document=doc)
        b = create_timeline_node(event=event, summary="被告貪污犯定讞", citation_document=doc,
                                 is_final=True)
        with pytest.raises(WordingRejected):
            create_causal_edge(
                event=event, from_node=a, to_node=b, kind=CausalKind.STATED,
                citation_document=doc, is_final=False,
            )


@pytest.mark.medium
class TestRecursiveTraversal:
    @pytest.fixture
    def chain(self, event, doc):
        """建立 a → b → c → d 的因果鏈。"""
        nodes = [create_timeline_node(event=event, summary=f"節點{i}", citation_document=doc)
                for i in range(4)]
        for a, b in zip(nodes, nodes[1:]):
            create_causal_edge(event=event, from_node=a, to_node=b,
                               kind=CausalKind.STATED, citation_document=doc)
        return nodes

    def test_沿因果邊向下游走訪(self, chain):
        a, b, c, d = chain
        steps = causal_descendants(a.pk)
        visited = [s.to_node_id for s in steps]
        assert visited == [b.pk, c.pk, d.pk]
        assert [s.depth for s in steps] == [1, 2, 3]

    def test_沿因果邊向上游走訪(self, chain):
        a, b, c, d = chain
        steps = causal_ancestors(d.pk)
        visited = [s.from_node_id for s in steps]
        assert visited == [c.pk, b.pk, a.pk]

    def test_末端節點無下游(self, chain):
        _, _, _, d = chain
        assert causal_descendants(d.pk) == []

    def test_深度上限阻止環狀因果導致無限遞迴(self, event, doc):
        """正常的因果圖應是 DAG，但不能假設輸入永遠正確——
        這裡刻意建一個環，驗證深度上限確實擋得住無限遞迴。"""
        a = create_timeline_node(event=event, summary="節點A", citation_document=doc)
        b = create_timeline_node(event=event, summary="節點B", citation_document=doc)
        c = create_timeline_node(event=event, summary="節點C", citation_document=doc)
        create_causal_edge(event=event, from_node=a, to_node=b, kind=CausalKind.STATED, citation_document=doc)
        create_causal_edge(event=event, from_node=b, to_node=c, kind=CausalKind.STATED, citation_document=doc)
        create_causal_edge(event=event, from_node=c, to_node=a, kind=CausalKind.STATED, citation_document=doc)

        steps = causal_descendants(a.pk, max_depth=10)
        assert len(steps) <= 10, "深度上限應阻止環狀因果無限累積邊"
        assert max(s.depth for s in steps) <= 10

    def test_已走訪節點的陣列追蹤是防環的第二層(self, event, doc):
        """即使 max_depth 給得很大，陣列追蹤也該讓環狀路徑提前停止，
        不必吃滿整個深度上限才停。"""
        a = create_timeline_node(event=event, summary="節點A", citation_document=doc)
        b = create_timeline_node(event=event, summary="節點B", citation_document=doc)
        create_causal_edge(event=event, from_node=a, to_node=b, kind=CausalKind.STATED, citation_document=doc)
        create_causal_edge(event=event, from_node=b, to_node=a, kind=CausalKind.STATED, citation_document=doc)

        steps = causal_descendants(a.pk, max_depth=1000)
        assert len(steps) < 1000

    def test_預設深度上限為5(self):
        assert DEFAULT_MAX_DEPTH == 5

    def test_不存在的起點回傳空清單(self, db):
        assert causal_descendants(999999) == []


@pytest.mark.medium
class TestCausalEdgePublicVisibility:
    """任務 42、規格 M9：inferred 因果邊預設不公開的查詢層驗證。"""

    def test_只回傳公開的邊(self, event, doc):
        a = create_timeline_node(event=event, summary="檢方複訊被告", citation_document=doc)
        b = create_timeline_node(event=event, summary="檢方起訴被告", citation_document=doc)
        c = create_timeline_node(event=event, summary="法院裁定羈押", citation_document=doc)

        stated = create_causal_edge(event=event, from_node=a, to_node=b,
                                    kind=CausalKind.STATED, citation_document=doc)
        inferred = create_causal_edge(event=event, from_node=b, to_node=c,
                                      kind=CausalKind.INFERRED, confidence=0.6)

        visible = CausalEdge.objects.publicly_visible()
        assert stated in visible
        assert inferred not in visible
