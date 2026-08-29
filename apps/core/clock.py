"""時鐘接縫。

系統有大量與時間相關的行為——事件 active → dormant → 喚醒、分頻排程、
入庫延遲 SLA。若這些邏輯直接呼叫 ``timezone.now()``，測試就只能靠 sleep
或 monkeypatch，兩者都脆弱。

規則：**業務邏輯一律透過注入的 Clock 取得時間，不得直接呼叫 now()。**
如此「事件靜置 30 日後轉為 dormant」可以是毫秒級的 small 測試。
"""
from __future__ import annotations

import datetime as _dt
from typing import Protocol


class Clock(Protocol):
    """時間來源。實作只需提供 now()。"""

    def now(self) -> _dt.datetime:
        """回傳當下時間，必須帶時區（aware datetime）。"""
        ...


class SystemClock:
    """正式環境使用：讀取系統時間（UTC）。"""

    def now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.timezone.utc)


class FixedClock:
    """測試使用：時間固定，可手動前進。

    >>> clock = FixedClock(_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc))
    >>> clock.advance(days=30)
    >>> clock.now().day
    31
    """

    def __init__(self, start: _dt.datetime):
        if start.tzinfo is None:
            raise ValueError("FixedClock 需要帶時區的 datetime")
        self._now = start

    def now(self) -> _dt.datetime:
        return self._now

    def advance(self, **timedelta_kwargs) -> None:
        """前進指定時間，參數同 ``datetime.timedelta``。"""
        self._now += _dt.timedelta(**timedelta_kwargs)

    def set(self, moment: _dt.datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FixedClock 需要帶時區的 datetime")
        self._now = moment
