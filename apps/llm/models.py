"""LLM 用量記錄。

每一次 LLM 呼叫都留一筆。設計上有兩個不可妥協的點：

1. **成本在呼叫當下算出並存下**，不在顯示時重算。價格會調整、匯率
   每天在變；依當前費率重算歷史記錄，過去的帳就永遠是錯的。
   因此連當時使用的費率與匯率都一併保存。
2. **未登記價格的模型會讓呼叫失敗**，而非把成本記為 0。記成 0 的帳
   比沒有帳更危險——它看起來是正確的。
"""
from __future__ import annotations

from decimal import Decimal

from django.db import models


class LlmPurpose(models.TextChoices):
    """呼叫用途。對應規格的分層，用於分辨成本花在哪一層。

    ADR-0009 的模型分級策略（L1 用便宜檔、L3/L4 用旗艦檔）能否奏效，
    就看這個維度的成本分布。
    """

    EXTRACTION = "extraction", "L1 結構化抽取"
    RELEVANCE = "relevance", "相關性判定"
    EVENT_LINKING = "event_linking", "L3 事件歸屬"
    EVENT_SUMMARY = "event_summary", "L3 事件摘要"
    CAUSAL = "causal", "L4 因果推理"
    NARRATIVE = "narrative", "L4 敘事生成"
    WORDING_CHECK = "wording_check", "措辭檢查"
    OTHER = "other", "其他"


class LlmUsage(models.Model):
    """單次 LLM 呼叫的用量與成本。"""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    purpose = models.CharField(
        max_length=32, choices=LlmPurpose.choices,
        default=LlmPurpose.OTHER, db_index=True,
    )
    #: Celery 任務名稱。用途說明「做什麼」，這個說明「從哪裡呼叫的」，
    #: 排查異常用量時兩者都需要。
    task_name = models.CharField(max_length=128, blank=True, db_index=True)
    model = models.CharField(max_length=64, db_index=True)

    # --- token 用量 ---
    # DeepSeek 回傳 prompt_cache_hit_tokens 與 prompt_cache_miss_tokens，
    # 兩者相加等於 prompt_tokens。分開存是因為價差約 30 倍，
    # 合併記錄就無法看出 context caching 是否真的發揮作用。
    cached_input_tokens = models.IntegerField(default=0)
    uncached_input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)

    # --- 成本（呼叫當下計算並凍結）---
    cost_usd = models.DecimalField(max_digits=12, decimal_places=8, default=Decimal("0"))
    cost_ntd = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    usd_to_ntd = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("0"))
    peak = models.BooleanField(default=False, help_text="是否為尖峰時段（離峰價格減半）")

    # --- 執行狀況 ---
    duration_ms = models.IntegerField(default=0)
    succeeded = models.BooleanField(default=True, db_index=True)
    error = models.TextField(blank=True)

    #: 關聯的文件（若有）。用於追查某篇文件的處理成本。
    #: 刻意用 SET_NULL 而非 CASCADE——文件被刪除不該讓帳目消失。
    document = models.ForeignKey(
        "ingest.Document", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="llm_usages",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at", "purpose"], name="llmusage_time_purpose"),
            models.Index(fields=["model", "-created_at"], name="llmusage_model_time"),
        ]
        verbose_name = "LLM 用量"
        verbose_name_plural = "LLM 用量"

    def __str__(self) -> str:
        return (f"{self.get_purpose_display()} / {self.model} / "
                f"{self.total_tokens:,} tokens / NT${self.cost_ntd:.4f}")

    @property
    def input_tokens(self) -> int:
        return self.cached_input_tokens + self.uncached_input_tokens

    @property
    def cache_hit_rate(self) -> float:
        """輸入 token 的 cache 命中率。

        這個數字若長期偏低，表示 context caching 沒有發揮作用——
        而那正是 ADR-0009 選用 DeepSeek 的實質理由之一，值得盯著。
        """
        total = self.input_tokens
        return self.cached_input_tokens / total if total else 0.0
