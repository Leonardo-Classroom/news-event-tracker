"""時間線與因果子圖（規格 §4.2、ADR-0003，任務 28-32）。

**per-event 知識子圖，不是全庫知識圖譜**——這是規格的三個決定性設計
選擇之一。``CausalEdge`` 一律屬於某個 ``Event``，查詢永遠限定在單一
事件的子圖內，不存在跨事件的全域圖遍歷需求（ADR-0003）。

## 引用強制是資料庫層的保證，不是應用層的約定

M5：「時間線每一節點都有可點擊出處」。``TimelineNode.citation_document``
是不可為 NULL 的外鍵——任何寫入路徑（含未來的批次腳本、修補程式）
都無法繞過，這與 ADR-0003「引用必填是資料庫層的 constraint」一致。

M9：「100% 的 stated 邊具備有效引文」。``CausalEdge`` 用 CHECK
constraint 強制 ``kind='stated'`` 時 ``citation_document`` 不得為 NULL，
``kind='inferred'`` 時 ``confidence`` 不得為 NULL——後者是因為推論邊
本來就沒有「引用」可言，但必須讓讀者知道這個推論有多可信。
"""
from __future__ import annotations

from django.db import models

from apps.llm.models import LlmPurpose


class GenerationLog(models.Model):
    """所有 LLM 生成文字的版本化紀錄（任務 32）。

    任一段對外文字都能回溯其生成條件——這是規格 §4.5 的要求，也是
    「敘事出錯時能查是哪一版 prompt 生成的」唯一辦法。與
    ``LlmUsage``（任務 LLM 用量記錄）分工不同：``LlmUsage`` 記的是
    成本與 token，這裡記的是「這段文字實際上是什麼、怎麼生出來的」。
    """

    purpose = models.CharField(max_length=16, choices=LlmPurpose.choices)
    model = models.CharField(max_length=64)
    prompt_version = models.CharField(max_length=32)
    input_summary = models.TextField(blank=True, help_text="輸入摘要，非完整原文")
    output_text = models.TextField(blank=True, help_text="模型原始輸出，後處理前")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "生成紀錄"
        verbose_name_plural = "生成紀錄"

    def __str__(self) -> str:
        return f"{self.get_purpose_display()}／{self.model}／{self.prompt_version}"


class TimelineNode(models.Model):
    """時間線上的一個事實條目（任務 28）。

    ``summary`` 只陳述文件明載的程序事實，措辭需先過
    ``apps.compliance.wording.check_wording`` 才能對外呈現——
    這裡的模型層不做這件事，由生成服務與 Event 的 wording gate 負責，
    模型層只保證「有出處」這一半的護欄。
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE,
                              related_name="timeline_nodes")
    occurred_on = models.DateField(null=True, blank=True,
                                   help_text="本節點所述事實的發生日期，未載明則留空")
    summary = models.CharField(max_length=200)

    #: 主要出處。**不可為 NULL**——這就是 M5 的資料庫層保證。
    citation_document = models.ForeignKey(
        "ingest.Document", on_delete=models.PROTECT, related_name="timeline_citations")
    citation_excerpt = models.TextField(blank=True, help_text="引用段落，供人工核對")

    generation = models.ForeignKey(
        GenerationLog, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="timeline_nodes")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_on", "created_at"]
        indexes = [
            models.Index(fields=["event", "occurred_on"], name="tlnode_event_date"),
        ]
        verbose_name = "時間線節點"
        verbose_name_plural = "時間線節點"

    def __str__(self) -> str:
        date = self.occurred_on.isoformat() if self.occurred_on else "日期未載明"
        return f"[{date}] {self.summary[:40]}"


class CausalKind(models.TextChoices):
    STATED = "stated", "報導明述"
    INFERRED = "inferred", "系統推論"


class CausalEdge(models.Model):
    """兩個時間線節點間的因果關係（任務 29、30，ADR-0003）。

    ``event`` 欄位刻意冗餘——``from_node``／``to_node`` 已能推出事件，
    但把它獨立存一份能讓「取某事件全部邊」不必先 join 兩次
    ``TimelineNode``，且讓下面的 CHECK constraint 能直接表達
    「不可跨事件連邊」而不必依賴應用層記得檢查。
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE,
                              related_name="causal_edges")
    from_node = models.ForeignKey(TimelineNode, on_delete=models.CASCADE,
                                  related_name="edges_from")
    to_node = models.ForeignKey(TimelineNode, on_delete=models.CASCADE,
                                related_name="edges_to")
    kind = models.CharField(max_length=8, choices=CausalKind.choices)

    #: stated 邊強制要求，inferred 邊留空——推論本來就沒有「引用」可言。
    citation_document = models.ForeignKey(
        "ingest.Document", null=True, blank=True, on_delete=models.PROTECT,
        related_name="causal_citations")
    citation_excerpt = models.TextField(blank=True)

    #: inferred 邊強制要求。讀者需要知道這個推論有多可信，
    #: stated 邊不需要——它陳述的是報導明說的事，沒有「信心」的問題。
    confidence = models.FloatField(null=True, blank=True)

    generation = models.ForeignKey(
        GenerationLog, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="causal_edges")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # M9：100% 的 stated 邊具備有效引文
            models.CheckConstraint(
                condition=~models.Q(kind=CausalKind.STATED) | models.Q(citation_document__isnull=False),
                name="causal_stated_requires_citation",
            ),
            models.CheckConstraint(
                condition=~models.Q(kind=CausalKind.INFERRED) | models.Q(confidence__isnull=False),
                name="causal_inferred_requires_confidence",
            ),
            models.CheckConstraint(
                condition=~models.Q(from_node=models.F("to_node")),
                name="causal_edge_no_self_loop",
            ),
        ]
        indexes = [
            models.Index(fields=["event"], name="causaledge_event"),
            models.Index(fields=["from_node"], name="causaledge_from"),
        ]
        verbose_name = "因果邊"
        verbose_name_plural = "因果邊"

    def __str__(self) -> str:
        return f"{self.from_node_id} → {self.to_node_id}（{self.get_kind_display()}）"
