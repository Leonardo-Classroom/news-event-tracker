"""事件生命週期的測試。

狀態機錯誤沒有外顯徵狀——事件不會報錯，只會**停止被追蹤**，
而那正是本系統要防止的事。因此轉換規則需完整涵蓋。
"""
import datetime as dt

import pytest
from django.utils import timezone

from apps.events.models import (
    DORMANT_AFTER_DAYS, Event, EventStatus, EventVisibility, InvalidTransition,
    RiskTier, WordingGateFailed,
)

UTC = dt.timezone.utc


def make_event(**kw):
    defaults = dict(slug="test-event", title="測試事件")
    return Event(**{**defaults, **kw})


class TestTransitions:
    @pytest.mark.parametrize("source,target", [
        (EventStatus.CANDIDATE, EventStatus.DRAFT),
        (EventStatus.CANDIDATE, EventStatus.REJECTED),
        (EventStatus.DRAFT, EventStatus.ACTIVE),
        (EventStatus.DRAFT, EventStatus.REJECTED),
        (EventStatus.ACTIVE, EventStatus.DORMANT),
        (EventStatus.ACTIVE, EventStatus.CLOSED),
        (EventStatus.DORMANT, EventStatus.ACTIVE),
        (EventStatus.DORMANT, EventStatus.CLOSED),
        (EventStatus.CLOSED, EventStatus.ACTIVE),
    ])
    def test_合法轉換(self, source, target):
        e = make_event(status=source)
        e.transition_to(target, save=False)
        assert e.status == target

    @pytest.mark.parametrize("source,target", [
        (EventStatus.CANDIDATE, EventStatus.ACTIVE),    # 未經審核不可直接追蹤
        (EventStatus.CANDIDATE, EventStatus.CLOSED),      # 未經審核不可直接結案
        (EventStatus.DRAFT, EventStatus.DORMANT),
        (EventStatus.REJECTED, EventStatus.ACTIVE),     # 駁回是終態
        (EventStatus.REJECTED, EventStatus.DRAFT),
    ])
    def test_非法轉換被拒(self, source, target):
        e = make_event(status=source)
        with pytest.raises(InvalidTransition):
            e.transition_to(target, save=False)

    def test_轉為相同狀態不報錯(self):
        e = make_event(status=EventStatus.ACTIVE)
        e.transition_to(EventStatus.ACTIVE, save=False)
        assert e.status == EventStatus.ACTIVE

    def test_駁回為終態(self):
        """避免被駁回的事件被重新啟用而繞過審核。"""
        e = make_event(status=EventStatus.REJECTED)
        for target in EventStatus.values:
            if target == EventStatus.REJECTED:
                continue
            assert not e.can_transition_to(target)

    def test_closed_可因翻案重啟(self):
        """判決確定後仍可能有再審或非常上訴。"""
        e = make_event(status=EventStatus.CLOSED)
        assert e.can_transition_to(EventStatus.ACTIVE)


class TestVisibility:
    def test_僅_active_可公開(self):
        for status in [EventStatus.CANDIDATE, EventStatus.DRAFT,
                       EventStatus.DORMANT, EventStatus.CLOSED]:
            with pytest.raises(InvalidTransition):
                make_event(status=status).publish(save=False)

    def test_active_可公開(self):
        e = make_event(status=EventStatus.ACTIVE)
        e.publish(save=False)
        assert e.visibility == EventVisibility.PUBLIC

    def test_撤下不設狀態前提(self):
        """更正／下架申訴須能立即生效（規格 §8.4，24 小時 SLA）。"""
        for status in EventStatus.values:
            e = make_event(status=status, visibility=EventVisibility.PUBLIC)
            e.unpublish(save=False)
            assert e.visibility == EventVisibility.PRIVATE


class TestDormancy:
    def test_無進展紀錄者不判定沉寂(self):
        assert make_event(status=EventStatus.ACTIVE).should_become_dormant() is False

    def test_達門檻即沉寂(self):
        now = dt.datetime(2026, 6, 1, tzinfo=UTC)
        e = make_event(status=EventStatus.ACTIVE,
                       last_progress_at=now - dt.timedelta(days=DORMANT_AFTER_DAYS))
        assert e.should_become_dormant(now) is True

    def test_未達門檻不沉寂(self):
        now = dt.datetime(2026, 6, 1, tzinfo=UTC)
        e = make_event(status=EventStatus.ACTIVE,
                       last_progress_at=now - dt.timedelta(days=DORMANT_AFTER_DAYS - 1))
        assert e.should_become_dormant(now) is False

    def test_僅_active_會沉寂(self):
        now = dt.datetime(2026, 6, 1, tzinfo=UTC)
        old = now - dt.timedelta(days=365)
        for status in [EventStatus.DORMANT, EventStatus.CLOSED, EventStatus.DRAFT]:
            e = make_event(status=status, last_progress_at=old)
            assert e.should_become_dormant(now) is False


class TestWakeUp:
    """沉寂事件因官方進展而喚醒——本系統的核心行為。"""

    def test_沉寂事件被喚醒(self):
        e = make_event(status=EventStatus.DORMANT,
                       last_progress_at=dt.datetime(2026, 1, 1, tzinfo=UTC))
        woke = e.record_progress(dt.datetime(2026, 6, 1, tzinfo=UTC), save=False)
        assert woke is True
        assert e.status == EventStatus.ACTIVE
        assert e.last_progress_at == dt.datetime(2026, 6, 1, tzinfo=UTC)

    def test_active_事件記錄進展不算喚醒(self):
        e = make_event(status=EventStatus.ACTIVE,
                       last_progress_at=dt.datetime(2026, 1, 1, tzinfo=UTC))
        assert e.record_progress(dt.datetime(2026, 6, 1, tzinfo=UTC), save=False) is False
        assert e.status == EventStatus.ACTIVE

    def test_較早的進展不倒退時間(self):
        """補抓到的舊文件不該讓 last_progress_at 往回走，
        否則會誤觸發沉寂判定。"""
        latest = dt.datetime(2026, 6, 1, tzinfo=UTC)
        e = make_event(status=EventStatus.ACTIVE, last_progress_at=latest)
        e.record_progress(dt.datetime(2026, 1, 1, tzinfo=UTC), save=False)
        assert e.last_progress_at == latest


class TestWordingGate:
    """進入 draft 前強制過措辭檢查器，不提供繞過參數（任務 39）。"""

    def test_違規措辭擋下進入draft(self):
        e = make_event(status=EventStatus.CANDIDATE, risk_tier=RiskTier.HIGH,
                       summary="柯文哲貪污犯，京華城案已定讞")
        with pytest.raises(WordingGateFailed):
            e.transition_to(EventStatus.DRAFT, save=False)
        assert e.status == EventStatus.CANDIDATE, "檢查失敗時狀態不應被改動"

    def test_合規措辭可進入draft(self):
        e = make_event(status=EventStatus.CANDIDATE, risk_tier=RiskTier.HIGH,
                       summary="柯文哲遭起訴涉嫌圖利，案件仍在審理中")
        e.transition_to(EventStatus.DRAFT, save=False)
        assert e.status == EventStatus.DRAFT

    def test_低中風險事件的定讞用語視為真實敘述(self):
        """risk_tier 非 HIGH 代表沒有未定讞的具名自然人，
        此時終結性用語（如「已定讞」）是陳述已發生的事實。"""
        e = make_event(status=EventStatus.CANDIDATE, risk_tier=RiskTier.MEDIUM,
                       summary="陳水扁貪污案判刑確定")
        e.transition_to(EventStatus.DRAFT, save=False)
        assert e.status == EventStatus.DRAFT

    def test_current_status_text也受檢查(self):
        e = make_event(status=EventStatus.CANDIDATE, risk_tier=RiskTier.HIGH,
                       current_status_text="沈慶京詐欺犯，已入獄服刑")
        with pytest.raises(WordingGateFailed):
            e.transition_to(EventStatus.DRAFT, save=False)

    def test_空白摘要不觸發檢查(self):
        e = make_event(status=EventStatus.CANDIDATE, risk_tier=RiskTier.HIGH)
        e.transition_to(EventStatus.DRAFT, save=False)
        assert e.status == EventStatus.DRAFT

    def test_發布前再次檢查(self):
        """summary 可能在 draft 之後、發布之前又被編輯過，
        不能只信任進入 draft 時的那一次結果。"""
        e = make_event(status=EventStatus.ACTIVE, risk_tier=RiskTier.HIGH,
                       summary="合規的敘述")
        e.summary = "柯文哲貪污犯定讞"
        with pytest.raises(WordingGateFailed):
            e.publish(save=False)


class TestRiskTier:
    def test_高風險不可批次通過(self):
        assert make_event(risk_tier=RiskTier.HIGH).allows_batch_review is False

    def test_中低風險可批次通過(self):
        assert make_event(risk_tier=RiskTier.MEDIUM).allows_batch_review is True
        assert make_event(risk_tier=RiskTier.LOW).allows_batch_review is True


class TestCheckInterval:
    def test_沉寂事件的官方源頻率不降低(self):
        """規格 §4.4 的核心約束：新聞沉寂期正是判決出爐的時期，
        降低官方源頻率等於放棄本系統的核心功能。"""
        e = make_event(status=EventStatus.DORMANT)
        assert e.check_interval_days(official_source=True) == 1     # 與 active 相同
        assert e.check_interval_days(official_source=False) == 7    # 新聞降頻

    def test_active_兩者皆每日(self):
        e = make_event(status=EventStatus.ACTIVE)
        assert e.check_interval_days(official_source=True) == 1
        assert e.check_interval_days(official_source=False) == 1

    def test_closed_官方源仍每季檢查(self):
        """防翻案與再審。"""
        e = make_event(status=EventStatus.CLOSED)
        assert e.check_interval_days(official_source=True) == 90
        assert e.check_interval_days(official_source=False) is None

    def test_未上線狀態不排程(self):
        for status in [EventStatus.CANDIDATE, EventStatus.DRAFT, EventStatus.REJECTED]:
            e = make_event(status=status)
            assert e.check_interval_days(official_source=True) is None
