"""風險分級自動判定（任務 40，規格 §4.3.1）。

**由抽取結果決定，不由人工指定。** 人工審核僅做形式確認（R8），若風險
分級也交由人工判斷，安全性就完全繫於審核者當下的注意力。分級必須是
`Extraction.payload` 的確定性函式，同一批資料永遠得到同一個等級。

## 判準

    高（HIGH）    具名自然人 × 案件未判決確定
    中（MEDIUM）  僅法人／機關，或具名自然人但案件已判決確定
    低（LOW）     無指名對象（政策、天災等）

## 「具名自然人」的認定：兩種 schema 提供依據不同

`judicial` schema 的 ``defendants`` 與 `corruption` schema 的
``persons`` 都是自然人；`corruption` schema 的 ``companies``／
``agencies`` 是法人或機關，不計入。

## 弊案 schema 沒有「是否定讞」的概念，保守視為未定讞

`corruption` schema 不像 `judicial` schema 有 ``is_final`` 欄位——
弊案報導談的是標案、金流、機關，往往在案件尚未進入起訴階段就已成新聞。
若一個事件只有弊案類抽取、沒有司法類抽取，我們**沒有依據**判斷是否
定讞，此時比照「未知」，判為高風險。這與整套系統「不知道就當作未確定」
的保守方向一致（見 `apps.compliance.wording` 的同一原則）。
"""
from __future__ import annotations

from apps.events.models import RiskTier
from apps.extract.schemas import SchemaKind

__all__ = ["determine_risk_tier", "determine_risk_tier_for_event"]


def determine_risk_tier(extractions) -> str:
    """依一批抽取結果（``Extraction`` 或具相同介面的物件）判定風險分級。

    Args:
        extractions: 可疊代的物件，每個須有 ``schema_kind`` 與 ``payload``
            兩個屬性——刻意不要求是 Django QuerySet，讓純邏輯可以用
            small 測試覆蓋，不必碰資料庫。
    """
    saw_named_person = False
    saw_unresolved_person = False

    for extraction in extractions:
        payload = extraction.payload or {}

        if extraction.schema_kind == SchemaKind.JUDICIAL:
            names = {d.get("name") for d in payload.get("defendants") or [] if d.get("name")}
            if names:
                saw_named_person = True
                if not payload.get("is_final"):
                    saw_unresolved_person = True

        elif extraction.schema_kind == SchemaKind.CORRUPTION:
            names = {p.get("name") for p in payload.get("persons") or [] if p.get("name")}
            if names:
                saw_named_person = True
                # 弊案 schema 無 is_final 概念——沒有依據判斷已定讞，保守視為未定讞
                saw_unresolved_person = True

    if not saw_named_person:
        return RiskTier.LOW
    if saw_unresolved_person:
        return RiskTier.HIGH
    return RiskTier.MEDIUM


def determine_risk_tier_for_event(event) -> str:
    """對 ``Event`` 的包裝：取其掛載文件的全部抽取結果並判定。"""
    from apps.extract.models import Extraction

    extractions = Extraction.objects.filter(
        document__event_documents__event=event
    ).only("schema_kind", "payload")
    return determine_risk_tier(extractions)
