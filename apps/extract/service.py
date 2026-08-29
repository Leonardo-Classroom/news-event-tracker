"""L1 結構化抽取（任務 14）。

走 ``LLMProvider``，因此用量記錄與預算閘門自動生效——呼叫端不需要、
也不應該自己處理那兩件事。

**抽取結果會成為時間線事實條目與事件歸屬的依據**，一個被推測出來的
案號或刑度會一路傳遞下去，而下游沒有任何環節能發現它是假的。
因此本模組除了呼叫 LLM，還負責**回頭驗證**：抽出的案號、金額、人名
是否真的出現在原文中。驗證不通過者標記出來，而非默默採用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings

from apps.core.identifiers import normalize_case_number
from apps.extract.courts import normalize_court
from apps.extract.models import PROMPT_VERSION, Extraction
from apps.extract.schemas import build_messages, choose_schema
from apps.ingest.models import Document
from apps.llm.models import LlmPurpose
from apps.llm.provider import LlmError, get_provider

logger = logging.getLogger(__name__)

__all__ = ["extract_document", "verify_payload", "VerificationReport"]


@dataclass
class VerificationReport:
    """抽取結果與原文的一致性檢查。

    LLM 幻覺在本系統是實質風險而非理論顧慮：規格 §8.3 要求時間線只記錄
    可查證的事實。這份報告讓「抽出來的東西原文有沒有」變成可量測的數字。
    """

    checked: int = 0
    grounded: int = 0
    ungrounded: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.grounded / self.checked if self.checked else 1.0

    @property
    def ok(self) -> bool:
        return not self.ungrounded


#: 需回頭比對原文的欄位。字串或字串陣列，其值應可在原文中找到。
#: 摘要類欄位（summary、topic）本就是改寫，不在此列。
#: sentence_details 與 summary 不列入：兩者本質上是把散落各處的事實
#: 組合成一句，不會是原文的連續片段，以子字串驗證必然誤判。
_GROUNDED_FIELDS = (
    "case_numbers", "court", "prosecutor_office", "sentence",
    "tender_ids", "tender_names", "agencies", "locations",
)
#: 物件陣列中需比對的鍵
_GROUNDED_OBJECT_KEYS = {"defendants": "name", "companies": "name",
                         "persons": "name", "organizations": None}


def _values_to_check(payload: dict) -> list[str]:
    values: list[str] = []
    for field_name in _GROUNDED_FIELDS:
        # 正規化過的欄位改查原文形式——正規化後的全名本就不在原文中
        value = payload.get(f"{field_name}_as_written", payload.get(field_name))
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        elif isinstance(value, list):
            values.extend(v.strip() for v in value if isinstance(v, str) and v.strip())

    for field_name, key in _GROUNDED_OBJECT_KEYS.items():
        for item in payload.get(field_name) or []:
            if isinstance(item, dict) and key:
                name = item.get(key)
            elif isinstance(item, str):
                name = item
            else:
                name = None
            if isinstance(name, str) and name.strip():
                values.append(name.strip())
    return values


def verify_payload(payload: dict, source_text: str) -> VerificationReport:
    """檢查抽取值是否真的出現在原文。

    案號另行以正規化後比對——原文的寫法可能含空白或全形數字，
    直接字串比對會誤判為幻覺。
    """
    report = VerificationReport()
    normalized_source = source_text.replace(" ", "").replace("　", "")

    for value in _values_to_check(payload):
        report.checked += 1
        compact = value.replace(" ", "")
        if compact in normalized_source:
            report.grounded += 1
            continue
        # 案號的寫法變體多，改以正規化形式比對
        canonical = normalize_case_number(value)
        if canonical and normalize_case_number(source_text) == canonical:
            report.grounded += 1
            continue
        report.ungrounded.append(value)

    return report


def extract_document(
    document: Document,
    *,
    provider=None,
    task_name: str = "",
    model: str = "",
) -> Extraction:
    """對單一文件執行 L1 抽取並儲存結果。

    失敗（呼叫錯誤或 JSON 解析失敗）也會寫入一筆 ``Extraction``：
    失敗率是需要被看見的訊號，而且能分辨「這篇沒有可抽的資訊」
    與「這篇抽取壞掉了」。
    """
    provider = provider or get_provider()
    schema_kind = choose_schema(document.relevance_signals)
    messages = build_messages(
        title=document.title, body=document.raw_body, schema_kind=schema_kind
    )

    try:
        response = provider.complete(
            messages=messages,
            purpose=LlmPurpose.EXTRACTION,
            model=model or settings.LLM_MODEL_CHEAP,
            json_schema={"type": "object"},
            temperature=0.0,
            # 實測：輸出中位數約 930 tokens；2048 時 5/25 被截斷，4096 時仍有 1/25。
            # 提高上限只在實際用到時才付費，因此從寬設定。
            max_tokens=8192,
            task_name=task_name,
            document_id=document.pk,
        )
    except LlmError as exc:
        return Extraction.objects.create(
            document=document, schema_kind=schema_kind, payload={},
            model=model or settings.LLM_MODEL_CHEAP,
            prompt_version=PROMPT_VERSION, succeeded=False, error=str(exc)[:2000],
        )

    # 先檢查截斷。輸出撞上 max_tokens 時 JSON 必然不完整，
    # 若逕行解析，錯誤會以「JSON 解析失敗」呈現而掩蓋真正的原因。
    if response.finish_reason == "length":
        return Extraction.objects.create(
            document=document, schema_kind=schema_kind, payload={},
            model=response.model, prompt_version=PROMPT_VERSION,
            succeeded=False,
            error=(f"輸出於 {response.output_tokens} tokens 處被截斷"
                   f"（達 max_tokens 上限），JSON 不完整"),
            raw_text=response.text[:8000],
        )

    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"回應不是 JSON 物件，而是 {type(payload).__name__}")
    except (ValueError, TypeError) as exc:
        return Extraction.objects.create(
            document=document, schema_kind=schema_kind, payload={},
            model=response.model, prompt_version=PROMPT_VERSION,
            succeeded=False, error=f"JSON 解析失敗：{exc}"[:2000],
            raw_text=response.text[:8000],
        )

    # 正規化在此以確定性規則完成，不交給 LLM（見 apps.extract.courts）。
    # 保留原文形式於 *_as_written，使驗證仍可比對原文。
    for key in ("court", "prosecutor_office"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            payload[f"{key}_as_written"] = raw
            payload[key] = normalize_court(raw)

    return Extraction.objects.create(
        document=document, schema_kind=schema_kind, payload=payload,
        model=response.model, prompt_version=PROMPT_VERSION, succeeded=True,
    )
