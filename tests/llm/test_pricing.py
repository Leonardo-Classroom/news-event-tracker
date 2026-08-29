"""計價邏輯的 small 測試。

成本算錯不會報錯，只會讓帳目長期失真——因此三個計價維度
（模型分級、cache 命中、尖峰離峰）都要有明確測試。
"""
import datetime as dt
from decimal import Decimal

import pytest

from apps.llm.pricing import PRICING, compute_cost, is_peak

UTC = dt.timezone.utc
# 2026-08-31 是星期一
PEAK = dt.datetime(2026, 8, 31, 2, 0, tzinfo=UTC)        # UTC 週一 02:00
OFFPEAK = dt.datetime(2026, 8, 31, 12, 0, tzinfo=UTC)    # UTC 週一 12:00
WEEKEND = dt.datetime(2026, 8, 29, 2, 0, tzinfo=UTC)     # UTC 週六 02:00


class TestIsPeak:
    @pytest.mark.parametrize("hour,expected", [
        (0, False), (1, True), (3, True), (4, False), (5, False),
        (6, True), (9, True), (10, False), (23, False),
    ])
    def test_平日時段邊界(self, hour, expected):
        assert is_peak(dt.datetime(2026, 8, 31, hour, tzinfo=UTC)) is expected

    def test_週末全時段離峰(self):
        assert is_peak(WEEKEND) is False
        assert is_peak(dt.datetime(2026, 8, 30, 2, tzinfo=UTC)) is False   # 週日

    def test_拒絕無時區的時間(self):
        """naive datetime 會讓尖峰判定隨執行機器的時區而變——
        那是不會報錯、只會讓帳算錯的問題。"""
        with pytest.raises(ValueError):
            is_peak(dt.datetime(2026, 8, 31, 2))


class TestComputeCost:
    def test_離峰為尖峰的一半(self):
        kwargs = dict(model="deepseek-v4-flash", cached_input_tokens=0,
                      uncached_input_tokens=1_000_000, output_tokens=0)
        peak = compute_cost(**kwargs, moment=PEAK)
        off = compute_cost(**kwargs, moment=OFFPEAK)
        assert peak.peak is True and off.peak is False
        assert off.usd == peak.usd / 2

    def test_cache_命中大幅降低成本(self):
        """命中價低約 30 倍，這正是 ADR-0009 選用 DeepSeek 的實質理由。"""
        hit = compute_cost(model="deepseek-v4-flash", cached_input_tokens=1_000_000,
                           uncached_input_tokens=0, output_tokens=0, moment=PEAK)
        miss = compute_cost(model="deepseek-v4-flash", cached_input_tokens=0,
                            uncached_input_tokens=1_000_000, output_tokens=0, moment=PEAK)
        assert miss.usd / hit.usd > 25

    def test_旗艦檔比便宜檔貴(self):
        kwargs = dict(cached_input_tokens=0, uncached_input_tokens=1_000_000,
                      output_tokens=1_000_000, moment=PEAK)
        flash = compute_cost(model="deepseek-v4-flash", **kwargs)
        pro = compute_cost(model="deepseek-v4-pro", **kwargs)
        assert pro.usd == flash.usd * 3

    def test_台幣換算(self):
        result = compute_cost(model="deepseek-v4-flash", cached_input_tokens=0,
                              uncached_input_tokens=1_000_000, output_tokens=0,
                              moment=PEAK, usd_to_ntd=Decimal("32.5"))
        assert result.ntd == result.usd * Decimal("32.5")
        assert result.usd_to_ntd == Decimal("32.5")

    def test_成本分項加總等於總額(self):
        r = compute_cost(model="deepseek-v4-pro", cached_input_tokens=500_000,
                         uncached_input_tokens=300_000, output_tokens=100_000,
                         moment=OFFPEAK)
        assert r.usd_input_cached + r.usd_input_uncached + r.usd_output == r.usd

    def test_未登記的模型拋錯而非記為零(self):
        """記成 0 元的帳比沒有帳更危險——它看起來是正確的。"""
        with pytest.raises(KeyError, match="未登記價格"):
            compute_cost(model="gpt-5-turbo", cached_input_tokens=0,
                         uncached_input_tokens=1000, output_tokens=100, moment=PEAK)

    def test_零用量為零成本(self):
        r = compute_cost(model="deepseek-v4-flash", cached_input_tokens=0,
                         uncached_input_tokens=0, output_tokens=0, moment=PEAK)
        assert r.usd == 0 and r.ntd == 0

    def test_所有登記模型皆可計算(self):
        for model in PRICING:
            assert compute_cost(model=model, cached_input_tokens=100,
                                uncached_input_tokens=100, output_tokens=100,
                                moment=PEAK).usd > 0
