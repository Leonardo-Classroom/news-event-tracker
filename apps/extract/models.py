"""L1 抽取結果。

**版本化**：記錄 model 與 prompt version。prompt 一改，抽取結果的
可比較性就斷了——若不記錄版本，日後無法分辨「品質變差」是模型退步
還是 prompt 改動所致。
"""
from __future__ import annotations

from django.db import models

#: prompt 或 schema 有實質變動時必須遞增。
#: 這個字串會存進每一筆抽取結果，是回溯品質變化的唯一依據。
PROMPT_VERSION = "2026-08-30.1"


class Extraction(models.Model):
    document = models.ForeignKey(
        "ingest.Document", on_delete=models.CASCADE, related_name="extractions",
    )
    schema_kind = models.CharField(max_length=16, db_index=True)
    payload = models.JSONField(default=dict)

    model = models.CharField(max_length=64)
    prompt_version = models.CharField(max_length=32, db_index=True)

    #: 抽取當下是否解析成功。失敗也留紀錄——失敗率是需要被看見的訊號，
    #: 且能分辨「這篇沒有可抽的資訊」與「這篇抽取壞掉了」。
    succeeded = models.BooleanField(default=True, db_index=True)
    error = models.TextField(blank=True)
    raw_text = models.TextField(blank=True, help_text="解析失敗時保留原始回應供除錯")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["document", "-created_at"], name="extraction_doc_time"),
            models.Index(fields=["schema_kind", "prompt_version"],
                         name="extraction_kind_version"),
        ]
        verbose_name = "L1 抽取結果"
        verbose_name_plural = "L1 抽取結果"

    def __str__(self) -> str:
        return f"{self.schema_kind} / {self.document_id} / {self.prompt_version}"
