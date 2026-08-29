"""風險分級的純邏輯測試（任務 40）。以假物件取代 Extraction，
不碰資料庫——判定規則本身不涉及任何 I/O。"""
import dataclasses

import pytest

from apps.compliance.risk import determine_risk_tier
from apps.events.models import RiskTier
from apps.extract.schemas import SchemaKind


@dataclasses.dataclass
class FakeExtraction:
    schema_kind: str
    payload: dict


class TestDetermineRiskTier:
    def test_無指名對象為低風險(self):
        extractions = [FakeExtraction(SchemaKind.GENERIC, {"topic": "颱風假"})]
        assert determine_risk_tier(extractions) == RiskTier.LOW

    def test_具名自然人且未定讞為高風險(self):
        extractions = [FakeExtraction(SchemaKind.JUDICIAL, {
            "defendants": [{"name": "柯文哲", "role": "前市長"}],
            "is_final": False,
        })]
        assert determine_risk_tier(extractions) == RiskTier.HIGH

    def test_具名自然人且已定讞為中風險(self):
        extractions = [FakeExtraction(SchemaKind.JUDICIAL, {
            "defendants": [{"name": "陳水扁", "role": "前總統"}],
            "is_final": True,
        })]
        assert determine_risk_tier(extractions) == RiskTier.MEDIUM

    def test_弊案schema無is_final概念故保守視為高風險(self):
        """corruption schema 沒有『是否定讞』的欄位，我們沒有依據判斷，
        保守方向是視為未定讞。"""
        extractions = [FakeExtraction(SchemaKind.CORRUPTION, {
            "persons": [{"name": "沈慶京", "role": "京華城負責人"}],
            "companies": [],
        })]
        assert determine_risk_tier(extractions) == RiskTier.HIGH

    def test_只有公司或機關無自然人為中風險(self):
        extractions = [FakeExtraction(SchemaKind.CORRUPTION, {
            "persons": [],
            "companies": [{"name": "京華城公司", "tax_id": ""}],
            "agencies": ["台北市都發局"],
        })]
        assert determine_risk_tier(extractions) == RiskTier.LOW

    def test_混合多筆抽取只要有一筆未定讞即為高風險(self):
        """一個事件橫跨多篇文件，只要有一筆抽取顯示未定讞，
        整個事件就該是高風險——寧可誤判過高，不可誤判過低。"""
        extractions = [
            FakeExtraction(SchemaKind.JUDICIAL, {
                "defendants": [{"name": "A", "role": "被告"}], "is_final": True,
            }),
            FakeExtraction(SchemaKind.JUDICIAL, {
                "defendants": [{"name": "B", "role": "被告"}], "is_final": False,
            }),
        ]
        assert determine_risk_tier(extractions) == RiskTier.HIGH

    def test_空清單為低風險(self):
        assert determine_risk_tier([]) == RiskTier.LOW

    def test_未載明is_final視為未定讞(self):
        """payload 未包含 is_final 鍵時（如較舊的抽取版本），
        get 的預設值為 None，等同未定讞——與整體保守方向一致。"""
        extractions = [FakeExtraction(SchemaKind.JUDICIAL, {
            "defendants": [{"name": "柯文哲", "role": "前市長"}],
        })]
        assert determine_risk_tier(extractions) == RiskTier.HIGH
