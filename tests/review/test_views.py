"""審核後台視圖。規則已在 service 的 small 測試覆蓋；這裡驗證
表單真的接到那層規則——尤其高風險不能靠隱藏核取方塊來擋。
"""
import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.events.models import (
    AssignmentMethod, Event, EventDocument, EventStatus, EventVisibility,
    RiskTier,
)
from apps.ingest.models import Document

UTC = dt.timezone.utc


@pytest.fixture
def user_client(db):
    User.objects.create_user("reviewer", "r@test.local", "pw")
    client = Client()
    client.login(username="reviewer", password="pw")
    return client


def make_event(**kw):
    defaults = dict(
        slug="pending", title="待審事件",
        status=EventStatus.DRAFT, risk_tier=RiskTier.MEDIUM,
        summary="檢方起訴被告涉嫌圖利，案件仍在審理中",
    )
    return Event.objects.create(**{**defaults, **kw})


class TestReviewList:
    def test_列出待審核事件(self, admin_client, db):
        make_event()
        html = admin_client.get("/review/").content.decode()
        assert "待審事件" in html
        assert "審核閘門尚未實作" not in html

    def test_高風險沒有批次核取方塊且指向逐條審核(self, admin_client, db):
        make_event(risk_tier=RiskTier.HIGH, title="高風險案")
        html = admin_client.get("/review/").content.decode()
        assert "name=\"event_ids\"" not in html
        assert "逐條審核" in html

    def test_中風險有一鍵通過(self, admin_client, db):
        make_event()
        html = admin_client.get("/review/").content.decode()
        assert "name=\"event_ids\"" in html
        assert "value=\"approve\"" in html

    def test_一般帳號可看不能改(self, user_client, db):
        make_event()
        assert user_client.get("/review/").status_code == 200
        response = user_client.post("/review/pending/decide/", {"action": "approve"})
        assert response.status_code == 403
        event = Event.objects.get(slug="pending")
        assert event.status == EventStatus.DRAFT


class TestReviewDecide:
    def test_中風險通過進入內部追蹤(self, admin_client, db):
        make_event()
        response = admin_client.post("/review/pending/decide/", {"action": "approve"})
        assert response.status_code == 302
        event = Event.objects.get(slug="pending")
        assert event.status == EventStatus.ACTIVE
        assert event.visibility == EventVisibility.PRIVATE

    def test_通過並公開(self, admin_client, db):
        make_event()
        admin_client.post("/review/pending/decide/", {"action": "approve_publish"})
        event = Event.objects.get(slug="pending")
        assert event.status == EventStatus.ACTIVE
        assert event.visibility == EventVisibility.PUBLIC

    def test_駁回(self, admin_client, db):
        make_event()
        admin_client.post("/review/pending/decide/", {"action": "reject"})
        assert Event.objects.get(slug="pending").status == EventStatus.REJECTED

    def test_高風險沒勾選被拒(self, admin_client, source, db):
        event = make_event(risk_tier=RiskTier.HIGH)
        doc = Document.objects.create(
            source=source, url="https://review.test/1", title="起訴報導",
            raw_body="內文", content_class=source.content_class,
            published_at=dt.datetime(2026, 1, 1, tzinfo=UTC),
        )
        EventDocument.objects.create(
            event=event, document=doc, method=AssignmentMethod.MANUAL)

        follow = admin_client.post(
            "/review/pending/decide/", {"action": "approve"}, follow=True)
        event.refresh_from_db()
        assert event.status == EventStatus.DRAFT
        assert "必須逐條確認" in follow.content.decode()

    def test_高風險勾完才通過(self, admin_client, source, db):
        event = make_event(risk_tier=RiskTier.HIGH)
        doc = Document.objects.create(
            source=source, url="https://review.test/2", title="起訴報導",
            raw_body="內文", content_class=source.content_class,
        )
        EventDocument.objects.create(
            event=event, document=doc, method=AssignmentMethod.MANUAL)

        admin_client.post("/review/pending/decide/", {
            "action": "approve",
            "wording": ["title", "summary"],
            "document_ids": [str(doc.pk)],
        })
        event.refresh_from_db()
        assert event.status == EventStatus.ACTIVE

    def test_GET不觸發審核(self, admin_client, db):
        make_event()
        assert admin_client.get("/review/pending/decide/").status_code == 405
        assert Event.objects.get(slug="pending").status == EventStatus.DRAFT


class TestReviewBatch:
    def test_批次通過中低風險並跳過高風險(self, admin_client, db):
        high = make_event(slug="high", title="高風險案", risk_tier=RiskTier.HIGH)
        medium = make_event(slug="med", title="中風險案")
        response = admin_client.post("/review/batch/", {
            "event_ids": [str(high.pk), str(medium.pk)],
        }, follow=True)
        html = response.content.decode()
        high.refresh_from_db()
        medium.refresh_from_db()
        assert high.status == EventStatus.DRAFT
        assert medium.status == EventStatus.ACTIVE
        assert medium.visibility == EventVisibility.PRIVATE
        assert "高風險已跳過" in html

    def test_空選取不動作(self, admin_client, db):
        make_event()
        admin_client.post("/review/batch/", {})
        assert Event.objects.get(slug="pending").status == EventStatus.DRAFT


class TestReviewDetail:
    def test_高風險頁有確認核取方塊(self, admin_client, source, db):
        event = make_event(risk_tier=RiskTier.HIGH, title="高風險詳情")
        doc = Document.objects.create(
            source=source, url="https://review.test/detail", title="起訴報導",
            raw_body="內文", content_class=source.content_class,
        )
        EventDocument.objects.create(
            event=event, document=doc, method=AssignmentMethod.MANUAL)
        html = admin_client.get("/review/pending/").content.decode()
        assert "name=\"wording\"" in html
        assert "name=\"document_ids\"" in html
        assert "起訴報導" in html
        assert "必須逐條確認" in html

    def test_中風險頁沒有強制核取方塊(self, admin_client, db):
        make_event()
        html = admin_client.get("/review/pending/").content.decode()
        assert "name=\"wording\"" not in html
        assert "形式確認" in html

    def test_已通過事件導回詳情(self, admin_client, db):
        make_event(status=EventStatus.ACTIVE, slug="gone")
        response = admin_client.get("/review/gone/")
        assert response.status_code == 302
        assert "/e/gone/" in response["Location"]


class TestEventPublish:
    def test_active未公開可從詳情頁公開(self, admin_client, db):
        make_event(status=EventStatus.ACTIVE, slug="ready", title="可公開")
        admin_client.post("/e/ready/publish/")
        event = Event.objects.get(slug="ready")
        assert event.visibility == EventVisibility.PUBLIC

    def test_draft不能公開(self, admin_client, db):
        make_event()
        admin_client.post("/e/pending/publish/")
        assert Event.objects.get(slug="pending").visibility == EventVisibility.PRIVATE
