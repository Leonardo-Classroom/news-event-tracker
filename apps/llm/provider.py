"""LLM 供應商抽象層（測試策略的接縫 1、ADR-0009 第 2 點）。

業務程式碼一律透過本介面呼叫，不得直接依賴任何供應商 SDK。三個理由：

1. **可測試性**——注入固定回應後，「事件歸屬判定」「因果生成」等原本
   非確定性的行為全部變成 small 測試。這是抽象層最先兌現的價值，
   比「日後可換供應商」實際得多。
2. **用量記錄無法被遺漏**——每次呼叫自動寫入 ``LlmUsage``。若靠呼叫端
   自行記錄，總有一條路徑會忘記，而忘記的後果是帳目缺口。
3. **預算閘門無法被繞過**——同上，設在此處而非呼叫端。

DeepSeek 相容 OpenAI 的介面，但**不直接依賴 openai 套件的型別**——
只用它發請求，回應一律轉成本模組自己的 ``LlmResponse``。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from django.conf import settings
from django.utils import timezone

from apps.llm.budget import BudgetExceeded, check_budget
from apps.llm.models import LlmPurpose, LlmUsage
from apps.llm.pricing import compute_cost

logger = logging.getLogger(__name__)

__all__ = ["LlmResponse", "LlmProvider", "DeepSeekProvider", "FakeProvider",
           "get_provider", "LlmError"]


class LlmError(Exception):
    """LLM 呼叫失敗。"""


@dataclass
class LlmResponse:
    text: str
    model: str
    #: 供應商回報的結束原因。"length" 表示撞上 max_tokens 而被截斷——
    #: 若不檢查此欄位，截斷的 JSON 會以「解析失敗」的面貌出現，
    #: 把問題指向錯誤的方向。
    finish_reason: str = ""
    cached_input_tokens: int = 0
    uncached_input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def input_tokens(self) -> int:
        return self.cached_input_tokens + self.uncached_input_tokens

    def json(self) -> Any:
        """把回應內容解析為 JSON。用於 structured output。"""
        text = self.text.strip()
        # 部分模型會用 ```json 包裹，即使已要求 JSON 格式
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        return json.loads(text)


class LlmProvider(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        purpose: str = LlmPurpose.OTHER,
        model: str = "",
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        task_name: str = "",
        document_id: int | None = None,
    ) -> LlmResponse: ...


class _RecordingProvider:
    """共用的記錄與預算邏輯。實際發送請求由子類的 ``_call`` 實作。"""

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        purpose: str = LlmPurpose.OTHER,
        model: str = "",
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        task_name: str = "",
        document_id: int | None = None,
    ) -> LlmResponse:
        model = model or settings.LLM_MODEL_CHEAP

        # 預算檢查在發送之前。超額時直接拋出，不記錄用量——
        # 沒有發生的呼叫不該出現在帳上。
        check_budget()

        started = time.monotonic()
        moment = timezone.now()
        error = ""
        response: LlmResponse | None = None

        try:
            response = self._call(
                messages=messages, model=model, json_schema=json_schema,
                temperature=temperature, max_tokens=max_tokens,
            )
        except BudgetExceeded:
            raise
        except Exception as exc:                        # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:2000]
            raise LlmError(error) from exc
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            # 失敗的呼叫也要留紀錄：供應商對已消耗的 token 仍可能計費，
            # 且失敗率本身是需要被看見的訊號。
            self._record(
                response=response, model=model, purpose=purpose,
                task_name=task_name, document_id=document_id,
                duration_ms=duration_ms, moment=moment, error=error,
            )

        response.duration_ms = duration_ms
        return response

    def _record(self, *, response, model, purpose, task_name, document_id,
                duration_ms, moment, error) -> None:
        cached = response.cached_input_tokens if response else 0
        uncached = response.uncached_input_tokens if response else 0
        output = response.output_tokens if response else 0

        try:
            cost = compute_cost(
                model=model,
                cached_input_tokens=cached,
                uncached_input_tokens=uncached,
                output_tokens=output,
                moment=moment,
                usd_to_ntd=Decimal(str(settings.LLM_USD_TO_NTD)),
            )
        except KeyError:
            # 未登記價格的模型：記錄用量但成本留空並明確標記，
            # 而非記為 0——成本為 0 的帳看起來是正確的，那才危險。
            logger.error("模型 %s 未登記價格，用量已記錄但成本無法計算", model)
            LlmUsage.objects.create(
                purpose=purpose, model=model, task_name=task_name[:128],
                cached_input_tokens=cached, uncached_input_tokens=uncached,
                output_tokens=output, total_tokens=cached + uncached + output,
                duration_ms=duration_ms, succeeded=not error,
                error=(error or "") + "｜模型未登記價格，成本未計入",
                document_id=document_id,
            )
            return

        LlmUsage.objects.create(
            purpose=purpose, model=model, task_name=task_name[:128],
            cached_input_tokens=cached, uncached_input_tokens=uncached,
            output_tokens=output, total_tokens=cached + uncached + output,
            cost_usd=cost.usd, cost_ntd=cost.ntd, usd_to_ntd=cost.usd_to_ntd,
            peak=cost.peak, duration_ms=duration_ms,
            succeeded=not error, error=error, document_id=document_id,
        )

    def _call(self, **kwargs) -> LlmResponse:
        raise NotImplementedError


class DeepSeekProvider(_RecordingProvider):
    """正式環境使用。DeepSeek 相容 OpenAI 介面。"""

    def __init__(self, api_key: str = "", base_url: str = ""):
        from openai import OpenAI

        key = api_key or settings.LLM_API_KEY
        if not key:
            raise LlmError("未設定 DEEPSEEK_API_KEY")
        self._client = OpenAI(
            api_key=key,
            base_url=base_url or settings.LLM_BASE_URL,
            timeout=120.0,
        )

    def _call(self, *, messages, model, json_schema, temperature, max_tokens):
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}

        completion = self._client.chat.completions.create(**kwargs)
        usage = completion.usage

        # DeepSeek 回傳 prompt_cache_hit_tokens 與 prompt_cache_miss_tokens。
        # 兩者價差 31 倍，分開記錄才看得出 context caching 是否發揮作用。
        cached = getattr(usage, "prompt_cache_hit_tokens", None)
        if cached is None:
            cached = 0
            uncached = getattr(usage, "prompt_tokens", 0)
        else:
            uncached = getattr(usage, "prompt_cache_miss_tokens",
                               max(0, usage.prompt_tokens - cached))

        choice = completion.choices[0]
        return LlmResponse(
            text=choice.message.content or "",
            model=model,
            finish_reason=getattr(choice, "finish_reason", "") or "",
            cached_input_tokens=cached,
            uncached_input_tokens=uncached,
            output_tokens=getattr(usage, "completion_tokens", 0),
            raw=completion.model_dump() if hasattr(completion, "model_dump") else {},
        )


class FakeProvider(_RecordingProvider):
    """測試使用。以預設回應取代真實呼叫。

    仍會走完整的記錄與預算流程——那正是要測試的部分。
    """

    def __init__(self, responses: list[str] | None = None,
                 tokens: tuple[int, int, int] = (100, 900, 50)):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self._tokens = tokens

    def _call(self, *, messages, model, json_schema, temperature, max_tokens):
        self.calls.append({"messages": messages, "model": model,
                           "json_schema": json_schema})
        text = self.responses.pop(0) if self.responses else "{}"
        if isinstance(text, Exception):
            raise text
        cached, uncached, output = self._tokens
        return LlmResponse(
            text=text, model=model, finish_reason="stop",
            cached_input_tokens=cached, uncached_input_tokens=uncached,
            output_tokens=output,
        )


_provider: LlmProvider | None = None


def get_provider() -> LlmProvider:
    """取得預設 provider。程序內單例——OpenAI client 持有連線池。"""
    global _provider
    if _provider is None:
        _provider = DeepSeekProvider()
    return _provider


def set_provider(provider: LlmProvider | None) -> None:
    """供測試注入替身。"""
    global _provider
    _provider = provider
