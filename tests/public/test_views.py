"""公開網站測試（Scope 7）。

刻意用真正的 ``Event.publish()`` 流程建立測試資料，不直接
``visibility=EventVisibility.PUBLIC`` 硬塞——這樣才能同時驗證
「公開網站只顯示真的走過發布流程的事件」，而不是「只要資料庫裡
某個欄位剛好是 public 就會被列出來」，兩者在真實系統裡不該有差別，
但寫死欄位的測試沒辦法發現發布流程本身的 bug（例如某天有人不小心
繞過 publish() 直接改欄位）。
"""
import datetime as dt

import pytest

from apps.events.models import Event, EventStatus, EventVisibility, RiskTier
from apps.ingest.models import ContentClass
from apps.timeline.graph import create_causal_edge, create_timeline_node
from apps.timeline.models import CausalKind

UTC = dt.timezone.utc


@pytest.fixture
def published_event(db):
    event = Event.objects.create(
        slug="test-case", title="測試案件",
        status=EventStatus.ACTIVE, risk_tier=RiskTier.MEDIUM,
        summary="這是測試案件的摘要說明。",
        current_status_text="法院已一審宣判，被告提起上訉。",
        first_seen_at=dt.datetime(2025, 1, 1, tzinfo=UTC),
        last_progress_at=dt.datetime(2026, 6, 1, tzinfo=UTC),
    )
    event.publish()
    return event


@pytest.fixture
def news_doc(source):
    from apps.ingest.models import Document

    return Document.objects.create(
        source=source, url="https://news.test/article",
        title="法院宣判被告一審有罪", raw_body="這是完整的新聞內文，理論上不該外流到公開頁。" * 20,
        content_class=ContentClass.COPYRIGHTED,
    )


@pytest.mark.medium
class TestCaseList:
    def test_只顯示已公開的事件(self, published_event, db):
        Event.objects.create(slug="private-case", title="未公開案件",
                             status=EventStatus.DRAFT)
        response = client_get("/case/")
        html = response.content.decode()
        assert "測試案件" in html
        assert "未公開案件" not in html

    def test_無公開事件時顯示空狀態(self, db):
        response = client_get("/case/")
        assert "目前沒有公開追蹤中的案件" in response.content.decode()

    def test_不需要登入(self, published_event):
        assert client_get("/case/").status_code == 200


@pytest.mark.medium
class TestCaseDetail:
    def test_可存取已公開事件(self, published_event):
        response = client_get(f"/case/{published_event.slug}/")
        assert response.status_code == 200
        assert "測試案件" in response.content.decode()

    def test_未公開事件回404(self, db):
        Event.objects.create(slug="draft-case", title="草稿案件", status=EventStatus.DRAFT)
        assert client_get("/case/draft-case/").status_code == 404

    def test_首屏顯示目前進度(self, published_event):
        """規格 G1：事件頁首屏顯示目前進度。"""
        html = client_get(f"/case/{published_event.slug}/").content.decode()
        assert "目前進度" in html
        assert "法院已一審宣判" in html

    def test_新聞全文永不外洩(self, published_event, news_doc):
        """規格 M7、G7，release blocker：公開端點回應絕不含新聞全文。"""
        node = create_timeline_node(
            event=published_event, summary="法院一審判決被告有罪",
            citation_document=news_doc, occurred_on=dt.date(2026, 5, 1),
        )
        html = client_get(f"/case/{published_event.slug}/").content.decode()
        assert news_doc.raw_body[:50] not in html
        assert "法院一審判決被告有罪" in html   # 摘要本身要看得到
        assert news_doc.url in html            # 原文連結要看得到

    def test_公文可全文呈現的連結顯示官方pdf(self, published_event, official_source):
        from apps.ingest.models import Document

        doc = Document.objects.create(
            source=official_source, url="https://data.judicial.gov.tw/x.pdf",
            title="判決書", raw_body="主文：被告有罪……",
            content_class=ContentClass.PUBLIC_RECORD,
        )
        create_timeline_node(event=published_event, summary="法院宣判",
                             citation_document=doc, occurred_on=dt.date(2026, 5, 1))
        html = client_get(f"/case/{published_event.slug}/").content.decode()
        assert doc.url in html

    def test_stated因果邊顯示inferred不顯示(self, published_event, news_doc):
        """任務 42：inferred 因果邊預設不公開。"""
        a = create_timeline_node(event=published_event, summary="檢方複訊被告",
                                 citation_document=news_doc)
        b = create_timeline_node(event=published_event, summary="檢方起訴被告",
                                 citation_document=news_doc)
        c = create_timeline_node(event=published_event, summary="法院裁定羈押",
                                 citation_document=news_doc)
        create_causal_edge(event=published_event, from_node=a, to_node=b,
                           kind=CausalKind.STATED, citation_document=news_doc)
        create_causal_edge(event=published_event, from_node=b, to_node=c,
                           kind=CausalKind.INFERRED, confidence=0.6)

        html = client_get(f"/case/{published_event.slug}/").content.decode()
        assert "檢方複訊被告 → 檢方起訴被告" in html
        assert "檢方起訴被告 → 法院裁定羈押" not in html

    def test_schema_org結構化資料存在(self, published_event):
        html = client_get(f"/case/{published_event.slug}/").content.decode()
        assert 'application/ld+json' in html
        assert '"@type": "Article"' in html

    def test_結構化資料跳脫script標籤避免注入(self, db):
        """事件標題若恰好含 </script> 字樣，直接塞進 <script> 標籤
        會被瀏覽器解析成標籤提前結束——用真實含該字樣的標題驗證
        跳脫確實生效，不是理論疑慮。"""
        event = Event.objects.create(
            slug="xss-case", title="測試</script><script>alert(1)</script>案件",
            status=EventStatus.ACTIVE, risk_tier=RiskTier.LOW,
        )
        event.publish()
        html = client_get(f"/case/{event.slug}/").content.decode()
        assert "<script>alert(1)</script>" not in html

    def test_不需要登入(self, published_event):
        assert client_get(f"/case/{published_event.slug}/").status_code == 200


def client_get(path):
    from django.test import Client

    return Client().get(path)


@pytest.mark.medium
class TestSeoInfrastructure:
    def test_sitemap只列公開事件(self, published_event, db):
        Event.objects.create(slug="draft2", title="草稿", status=EventStatus.DRAFT)
        xml = client_get("/sitemap.xml").content.decode()
        assert "/case/test-case/" in xml
        assert "draft2" not in xml

    def test_sitemap_lastmod對應last_progress_at而非updated_at(self, published_event):
        xml = client_get("/sitemap.xml").content.decode()
        assert "2026-06-01" in xml

    def test_robots文件允許案件頁不允許其他(self):
        text = client_get("/robots.txt").content.decode()
        assert "Allow: /case/" in text
        assert "Disallow: /" in text
        assert "Sitemap:" in text

    def test_sitemap不需要登入(self, published_event):
        assert client_get("/sitemap.xml").status_code == 200
