"""分頻排程測試（任務 27）。

``last_official_check_at`` 與 ``last_progress_at`` 分開記錄——後者只在
真的有新進展時更新，若拿它當「上次檢查時間」的依據，「查過但沒有新
東西」會被誤判成「還沒查過」，導致同一天對官方源重複發request。
"""
import datetime as dt

import pytest

from apps.events.models import Event, EventStatus

UTC = dt.timezone.utc


def make_event(**kw):
    defaults = dict(slug="test-event", title="測試事件")
    return Event(**{**defaults, **kw})


class TestDueForOfficialCheck:
    def test_從未檢查過即到期(self):
        e = make_event(status=EventStatus.ACTIVE)
        assert e.due_for_official_check() is True

    def test_active事件每日到期(self):
        now = dt.datetime(2026, 6, 2, tzinfo=UTC)
        e = make_event(status=EventStatus.ACTIVE,
                       last_official_check_at=now - dt.timedelta(days=1))
        assert e.due_for_official_check(now) is True

    def test_active事件當天已查則不到期(self):
        now = dt.datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
        e = make_event(status=EventStatus.ACTIVE,
                       last_official_check_at=now - dt.timedelta(hours=1))
        assert e.due_for_official_check(now) is False

    def test_dormant事件官方源仍每日到期(self):
        """規格 §4.4 的核心約束：新聞沉寂期正是判決出爐的時期，
        降低官方源頻率等於放棄本系統的核心功能。"""
        now = dt.datetime(2026, 6, 2, tzinfo=UTC)
        e = make_event(status=EventStatus.DORMANT,
                       last_official_check_at=now - dt.timedelta(days=1))
        assert e.due_for_official_check(now) is True

    def test_closed事件每季到期(self):
        now = dt.datetime(2026, 6, 2, tzinfo=UTC)
        recently_checked = make_event(
            status=EventStatus.CLOSED,
            last_official_check_at=now - dt.timedelta(days=30))
        assert recently_checked.due_for_official_check(now) is False

        overdue = make_event(
            status=EventStatus.CLOSED,
            last_official_check_at=now - dt.timedelta(days=91))
        assert overdue.due_for_official_check(now) is True

    def test_未上線狀態永不到期(self):
        for status in [EventStatus.CANDIDATE, EventStatus.DRAFT, EventStatus.REJECTED]:
            e = make_event(status=status)
            assert e.due_for_official_check() is False

    def test_檢查後記錄時間但不影響進展時間(self):
        """檢查了但沒查到新東西，last_progress_at 不該被更動——
        它專門標記「真的有新進展」，混用會讓沉寂判定失準。"""
        e = make_event(status=EventStatus.ACTIVE,
                       last_progress_at=dt.datetime(2026, 1, 1, tzinfo=UTC))
        checked_at = dt.datetime(2026, 6, 1, tzinfo=UTC)
        e.record_official_check(checked_at, save=False)
        assert e.last_official_check_at == checked_at
        assert e.last_progress_at == dt.datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.medium
class TestMaybeDueQuerySet:
    def test_排除候選與草稿(self, db):
        Event.objects.create(slug="c", title="候選", status=EventStatus.CANDIDATE)
        Event.objects.create(slug="d", title="草稿", status=EventStatus.DRAFT)
        active = Event.objects.create(slug="a", title="追蹤中", status=EventStatus.ACTIVE)

        due = list(Event.objects.maybe_due_for_official_check())

        assert due == [active]

    def test_今天已檢查過的不在寬鬆預篩內(self, db):
        from django.utils import timezone

        Event.objects.create(
            slug="a", title="追蹤中", status=EventStatus.ACTIVE,
            last_official_check_at=timezone.now(),
        )
        assert list(Event.objects.maybe_due_for_official_check()) == []
