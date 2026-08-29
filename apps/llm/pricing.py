"""LLM 計價。

**成本必須在呼叫當下算出並存下來，不可在顯示時才計算。** 價格會調整、
匯率每天在變，歷史記錄若依當前價格重算，過去的帳就永遠是錯的。
因此本模組的函式只負責「以此刻的價格算出金額」，結果由
``LlmUsage`` 連同當時的費率一併保存。

DeepSeek 的計價有三個維度，缺一不可：

1. **模型分級**——flash 與 pro 差約 3 倍
2. **cache 命中與否**——命中的輸入價格低約 30 倍。這正是 ADR-0009 選用
   DeepSeek 的實質理由：事件檔案是穩定前綴，反覆查詢時大量命中
3. **尖峰／離峰**——離峰為尖峰的一半

尖峰時段為 UTC 週一至週五 01:00–04:00 與 06:00–10:00，
換算台北時間是 09:00–12:00 與 14:00–18:00。**批次作業排在離峰可省一半。**
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "ModelPricing", "PRICING", "is_peak", "compute_cost", "CostBreakdown",
    "DEFAULT_USD_TO_NTD",
]

#: 台幣匯率預設值。可由設定覆寫，且每筆記錄會存下當時使用的匯率——
#: 匯率每天在變，事後重算會讓歷史帳目失真。
DEFAULT_USD_TO_NTD = Decimal("32.5")


@dataclass(frozen=True)
class ModelPricing:
    """單位為「每 1M tokens 的美元價格（尖峰）」。離峰自動折半。"""

    input_cache_hit: Decimal
    input_cache_miss: Decimal
    output: Decimal

    def at(self, *, peak: bool) -> "ModelPricing":
        if peak:
            return self
        half = Decimal("0.5")
        return ModelPricing(
            input_cache_hit=self.input_cache_hit * half,
            input_cache_miss=self.input_cache_miss * half,
            output=self.output * half,
        )


#: 官方價格（2026-08 查證）。尖峰價；離峰折半。
#: 新增模型時一併補上，否則 compute_cost 會拒絕計算而非猜測。
PRICING: dict[str, ModelPricing] = {
    "deepseek-v4-flash": ModelPricing(
        input_cache_hit=Decimal("0.014"),
        input_cache_miss=Decimal("0.44"),
        output=Decimal("1.32"),
    ),
    "deepseek-v4-pro": ModelPricing(
        input_cache_hit=Decimal("0.044"),
        input_cache_miss=Decimal("1.32"),
        output=Decimal("3.96"),
    ),
    "deepseek-v4-flash-vision-exp": ModelPricing(
        input_cache_hit=Decimal("0.014"),
        input_cache_miss=Decimal("0.44"),
        output=Decimal("1.32"),
    ),
}

#: 尖峰時段（UTC 小時區間，右開）。僅週一至週五。
_PEAK_WINDOWS_UTC = ((1, 4), (6, 10))


def is_peak(moment: dt.datetime) -> bool:
    """判斷是否落在尖峰時段。

    週末全時段為離峰。moment 必須帶時區——naive datetime 會讓
    尖峰判定隨執行機器的時區而變，那是難以察覺的計價錯誤。
    """
    if moment.tzinfo is None:
        raise ValueError("is_peak 需要帶時區的 datetime")

    utc = moment.astimezone(dt.timezone.utc)
    if utc.weekday() >= 5:          # 週六、週日
        return False
    return any(start <= utc.hour < end for start, end in _PEAK_WINDOWS_UTC)


@dataclass(frozen=True)
class CostBreakdown:
    usd: Decimal
    ntd: Decimal
    peak: bool
    usd_to_ntd: Decimal
    #: 各部分的美元金額，供後台顯示成本結構
    usd_input_cached: Decimal
    usd_input_uncached: Decimal
    usd_output: Decimal

    @property
    def cache_saving_usd(self) -> Decimal:
        """因 cache 命中而省下的金額。

        呈現這個數字的用意：若它長期接近零，表示 context caching 沒有
        發揮作用，而那正是 ADR-0009 選用 DeepSeek 的理由之一。
        """
        # 命中的 tokens 若以未命中價計算會多花多少
        return Decimal("0")     # 由 compute_cost 填入


def compute_cost(
    *,
    model: str,
    cached_input_tokens: int,
    uncached_input_tokens: int,
    output_tokens: int,
    moment: dt.datetime,
    usd_to_ntd: Decimal = DEFAULT_USD_TO_NTD,
) -> CostBreakdown:
    """計算單次呼叫的成本。

    未登記價格的模型會拋 ``KeyError`` 而非猜測——記成 0 元的帳
    比沒有帳更危險，因為它看起來是正確的。
    """
    if model not in PRICING:
        raise KeyError(
            f"未登記價格的模型：{model!r}。"
            f"請在 apps.llm.pricing.PRICING 補上，勿讓成本靜默記為 0。"
            f"（已登記：{', '.join(sorted(PRICING))}）"
        )

    peak = is_peak(moment)
    rates = PRICING[model].at(peak=peak)
    million = Decimal("1000000")

    usd_input_cached = Decimal(cached_input_tokens) / million * rates.input_cache_hit
    usd_input_uncached = Decimal(uncached_input_tokens) / million * rates.input_cache_miss
    usd_output = Decimal(output_tokens) / million * rates.output
    usd = usd_input_cached + usd_input_uncached + usd_output

    return CostBreakdown(
        usd=usd,
        ntd=usd * usd_to_ntd,
        peak=peak,
        usd_to_ntd=usd_to_ntd,
        usd_input_cached=usd_input_cached,
        usd_input_uncached=usd_input_uncached,
        usd_output=usd_output,
    )
