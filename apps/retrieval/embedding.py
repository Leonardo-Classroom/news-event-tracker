"""Embedding 服務（ADR-0004、ADR-0011）。

BGE-M3、1024 維、**CPU 執行、16 執行緒**。

執行緒數是實測值而非 ``os.cpu_count()``：i9-14900K 為混合架構
（8 P-core + 16 E-core），實測 16 執行緒比 32 快 **2.6 倍**——
Transformer 每層都有同步點，分派到較慢的 E-core 會讓每個同步點
都等最慢的核心。這個差距不會有任何錯誤徵狀，只是慢。

    threads=4  → 1.27 篇/秒     threads=16 → 2.43 篇/秒  ← 最佳
    threads=8  → 1.83 篇/秒     threads=32 → 0.92 篇/秒

**模型只載入一次。** 以模組層單例持有——BGE-M3 約 2.3GB，
每個 Celery worker 各載一份會直接吃光記憶體。需要多程序共用時，
應以獨立服務行程提供（見 ``embed_service`` 管理指令）。
"""
from __future__ import annotations

import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

__all__ = ["get_encoder", "embed_texts", "EMBEDDING_MODEL_VERSION"]

#: 寫入每筆向量記錄。換模型時可辨識哪些向量需重算，支援漸進式遷移——
#: 沒有這個欄位，換模型就只能全部重算或含糊地混用兩種向量。
EMBEDDING_MODEL_VERSION = "bge-m3/2026-08"

_encoder = None
_lock = threading.Lock()


def get_encoder():
    """取得模型單例。首次呼叫才載入（約 7 秒）。"""
    global _encoder
    if _encoder is not None:
        return _encoder

    with _lock:
        if _encoder is not None:
            return _encoder

        import torch
        from sentence_transformers import SentenceTransformer

        threads = int(getattr(settings, "EMBEDDING_THREADS", 16))
        torch.set_num_threads(threads)

        device = getattr(settings, "EMBEDDING_DEVICE", "cpu")
        name = getattr(settings, "EMBEDDING_MODEL", "BAAI/bge-m3")
        logger.info("載入 embedding 模型 %s（device=%s, threads=%d）",
                    name, device, threads)

        model = SentenceTransformer(name, device=device)
        model.max_seq_length = int(getattr(settings, "EMBEDDING_MAX_LENGTH", 1024))
        _encoder = model
        return _encoder


def embed_texts(texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
    """把文本轉為正規化後的 dense 向量。

    正規化（``normalize_embeddings=True``）使餘弦相似度等同內積，
    也讓 pgvector 的 ``<=>`` 運算子直接可用。
    """
    if not texts:
        return []

    model = get_encoder()
    size = batch_size or int(getattr(settings, "EMBEDDING_BATCH_SIZE", 8))
    vectors = model.encode(
        texts,
        batch_size=size,
        show_progress_bar=False,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return [v.tolist() for v in vectors]


def build_embedding_text(title: str, body: str, *, max_chars: int = 2000) -> str:
    """組出送去 embedding 的文本。

    ``max_chars=2000`` 搭配 ``max_seq_length=1024`` 為實測後的選擇。
    較快的設定（512 token／800 字）吞吐高一倍，且與完整設定的
    **餘弦相似度達 0.9866**——看似無損。但相似度不是有意義的品質指標：

        top-10 重疊率  平均 84.6%，最低 30%
        最近鄰相同     僅 82%
        top-5  重疊率  平均 82.4%，最低 40%

    **向量版本間的高餘弦相似度不代表排序穩定**——排序取決於相近鄰居
    之間的細微差距，而那正是截斷所丟失的資訊。ADR-0005 明訂
    「召回率的量測優先於準確率」，召回候選改變即召回率改變，
    因此不以品質換速度。

    標題重複一次以提高權重——標題是編輯對全文的濃縮，
    在事件歸屬時的鑑別力高於內文任一段落。

    內文截斷取前段：新聞的倒金字塔結構把關鍵事實放在前面，
    尾段多為背景與引述。
    """
    body_text = (body or "")[:max_chars]
    return f"{title}\n{title}\n{body_text}".strip()
