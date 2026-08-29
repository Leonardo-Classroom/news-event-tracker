"""Clock 接縫的 small 測試。

這道接縫的價值在於：讓「事件靜置 30 日後轉為 dormant」這類行為
成為毫秒級的確定性測試，而不需要 sleep 或等待真實時間。
"""
import datetime as dt

import pytest

from apps.core.clock import FixedClock, SystemClock


class TestSystemClock:
    def test_回傳帶時區的時間(self):
        assert SystemClock().now().tzinfo is not None


class TestFixedClock:
    def test_時間固定不動(self):
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        clock = FixedClock(start)
        assert clock.now() == start == clock.now()

    def test_advance_前進(self):
        clock = FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        clock.advance(days=30)
        assert clock.now() == dt.datetime(2026, 1, 31, tzinfo=dt.timezone.utc)

    def test_set_直接設定(self):
        clock = FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        target = dt.datetime(2027, 6, 15, tzinfo=dt.timezone.utc)
        clock.set(target)
        assert clock.now() == target

    def test_拒絕無時區的時間(self):
        """naive datetime 在跨時區運算時會靜默出錯，直接在建構時擋掉。"""
        with pytest.raises(ValueError):
            FixedClock(dt.datetime(2026, 1, 1))

    def test_set_也拒絕無時區(self):
        clock = FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        with pytest.raises(ValueError):
            clock.set(dt.datetime(2026, 1, 1))
