"""審核規則的 small 測試：不碰資料庫。

高風險禁止批次、必須逐條確認——這兩條若只寫在模板裡，改 POST
就能繞過。所以規則在 service 層，用不存檔的 Event 就能覆蓋。
"""
import pytest

from apps.events.models import Event, EventStatus, EventVisibility, RiskTier
from apps.review.service import (
    NotAwaitingReview, ReviewBlocked, ReviewIncomplete, ReviewItems,
    approve, batch_approve, missing_confirmations, reject,
)


def make_event(**kw):
    defaults = dict(slug="pending", title="待審事件",
                    status=EventStatus.DRAFT, risk_tier=RiskTier.MEDIUM)
    return Event(**{**defaults, **kw})


class TestMissingConfirmations:
    def test_中低風險不要求逐條勾選(self):
        items = ReviewItems(("title", "summary"), (1, 2), ())
        for tier in (RiskTier.MEDIUM, RiskTier.LOW):
            assert missing_confirmations(risk_tier=tier, items=items) == ()

    def test_高風險未勾選時列出缺項(self):
        items = ReviewItems(("title", "summary"), (11, 12), (3,))
        missing = missing_confirmations(risk_tier=RiskTier.HIGH, items=items)
        assert "標題" in missing
        assert "摘要" in missing
        assert "2 篇時間線文件" in missing
        assert "1 則時間線節點" in missing

    def test_高風險全部勾完為空(self):
        items = ReviewItems(("title",), (11,), ())
        assert missing_confirmations(
            risk_tier=RiskTier.HIGH, items=items,
            confirmed_wording={"title"}, confirmed_documents={11},
        ) == ()

    def test_多勾無關id不能補漏(self):
        items = ReviewItems(("title",), (11,), ())
        missing = missing_confirmations(
            risk_tier=RiskTier.HIGH, items=items,
            confirmed_wording={"title"},
            confirmed_documents={99, 100},
        )
        assert missing == ("1 篇時間線文件",)


class TestApprove:
    def test_中風險草稿一按即通過且未公開(self):
        e = make_event()
        approve(e, save=False)
        assert e.status == EventStatus.ACTIVE
        assert e.visibility == EventVisibility.PRIVATE

    def test_中風險可選擇通過並公開(self):
        e = make_event()
        approve(e, publish=True, save=False)
        assert e.status == EventStatus.ACTIVE
        assert e.visibility == EventVisibility.PUBLIC

    def test_候選先過措辭閘門再進入active(self):
        e = make_event(status=EventStatus.CANDIDATE,
                       summary="檢方起訴被告涉嫌圖利")
        approve(e, save=False)
        assert e.status == EventStatus.ACTIVE

    def test_高風險未確認被拒且狀態不變(self):
        e = make_event(risk_tier=RiskTier.HIGH,
                       summary="檢方起訴被告涉嫌圖利")
        with pytest.raises(ReviewIncomplete):
            approve(e, save=False)
        assert e.status == EventStatus.DRAFT

    def test_高風險確認措辭後可通過(self):
        e = make_event(risk_tier=RiskTier.HIGH,
                       summary="檢方起訴被告涉嫌圖利")
        approve(e, confirmed_wording={"title", "summary"}, save=False)
        assert e.status == EventStatus.ACTIVE

    def test_違規措辭即使勾完也被閘門擋住(self):
        e = make_event(status=EventStatus.CANDIDATE, risk_tier=RiskTier.HIGH,
                       summary="柯文哲貪污犯，京華城案已定讞")
        with pytest.raises(ReviewBlocked):
            approve(e, confirmed_wording={"title", "summary"}, save=False)
        assert e.status == EventStatus.CANDIDATE

    def test_已在追蹤中者不能再審一次(self):
        e = make_event(status=EventStatus.ACTIVE)
        with pytest.raises(NotAwaitingReview):
            approve(e, save=False)


class TestReject:
    def test_駁回為終態(self):
        e = make_event(status=EventStatus.CANDIDATE)
        reject(e, save=False)
        assert e.status == EventStatus.REJECTED

    def test_已通過者不能駁回(self):
        e = make_event(status=EventStatus.ACTIVE)
        with pytest.raises(NotAwaitingReview):
            reject(e, save=False)


class TestBatchApprove:
    def test_高風險被跳過中低風險通過(self):
        high = make_event(slug="high", title="高", risk_tier=RiskTier.HIGH)
        medium = make_event(slug="med", title="中", risk_tier=RiskTier.MEDIUM)
        low = make_event(slug="low", title="低", risk_tier=RiskTier.LOW)
        result = batch_approve([high, medium, low], save=False)
        assert result.skipped_high == (high,)
        assert {e.slug for e in result.approved} == {"med", "low"}
        assert result.blocked == ()
        assert high.status == EventStatus.DRAFT
        assert medium.status == EventStatus.ACTIVE
        assert low.status == EventStatus.ACTIVE
        assert all(e.visibility == EventVisibility.PRIVATE for e in result.approved)

    def test_批次從不公開(self):
        """批次公開會讓『要不要把真實事件放到公開頁』變成一次誤點。"""
        e = make_event()
        batch_approve([e], save=False)
        assert e.visibility == EventVisibility.PRIVATE
