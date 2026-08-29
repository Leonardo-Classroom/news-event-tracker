"""公開內容白名單（任務 41，規格 G7、M7、R2，⚠️ release blocker）。

**單一函式是唯一合法路徑。** 規格 M7 的驗收是「公開端點回應含第三方
新聞全文之案例 = 0」——這件事不能靠每個公開視圖各自小心處理欄位來
保證，因為「小心」不是機制，是一個容易被遺忘的習慣。唯一可靠的做法
是把「哪些欄位可以外流」收斂到一個函式，所有公開序列化都必須經過它，
而不是各自决定要不要放 ``raw_body``。

## 為什麼是白名單而非黑名單

黑名單（「除了 raw_body 都可以」）在模型新增欄位時預設外洩；
白名單（「只有列出的欄位可以」）在模型新增欄位時預設不外洩。
序列化的安全性應該在「忘記更新」的情況下傾向安全，而非傾向外洩。

## Scope 7 尚未建置：本函式是機制，不是完整驗收

任務 41 要求的「自動化測試掃描所有公開端點」需要公開端點存在
（Scope 7），而 Scope 7 依規劃在 Scope 6 之後才開始。目前只有這個
函式與它自己的測試——確保**若未來任何公開視圖使用這個函式**，
新聞全文不會外流。等 Scope 7 的公開視圖寫出來後，還需要另一層
「掃描所有公開端點回應」的測試，那一層現在無法寫，因為端點不存在。
這個缺口記在 docs/02-任務拆解.md 的任務 41。
"""
from __future__ import annotations

from apps.ingest.models import ContentClass

__all__ = ["public_document_fields", "assert_no_copyrighted_body"]

#: 白名單。**新增欄位到 Document 模型不會自動出現在這裡**——
#: 這正是白名單的設計目的：不確定安不安全，就先不外流。
_ALWAYS_SAFE_FIELDS = ("id", "title", "url", "published_at")


def public_document_fields(document) -> dict:
    """把 ``Document`` 轉成可對外呈現的欄位集合。

    ``body`` 鍵僅在 ``content_class == PUBLIC_RECORD``（公文，依著作權法
    第 9 條不受著作權保護，如裁判書）時才會出現。新聞（``COPYRIGHTED``）
    永遠不含 ``body``——公開頁對新聞只能放自製摘要與原文連結，
    原文連結就是 ``url`` 欄位，已在白名單內。
    """
    fields = {name: getattr(document, name) for name in _ALWAYS_SAFE_FIELDS}
    fields["source"] = document.source.name
    if document.content_class == ContentClass.PUBLIC_RECORD:
        fields["body"] = document.raw_body
    return fields


def assert_no_copyrighted_body(payload: dict, *, corpus: list[str]) -> None:
    """回歸測試用的斷言：遞迴掃描一個公開回應的 payload，確認任何字串
    值都不是（或不包含）語料庫中受著作權保護文件的全文。

    Args:
        corpus: 一批新聞全文（``raw_body``），來自 ``content_class ==
            COPYRIGHTED`` 的文件。用「內文是否整段出現在回應中」而非
            「payload 有沒有 body 鍵」來驗證——後者只驗證了序列化函式
            本身沒漏，前者驗證了序列化函式**真的有被用到**，若有人
            繞過 ``public_document_fields`` 直接把 model 丟進 JSON
            回應，這個檢查依然抓得到。
    """
    serialised = repr(payload)
    for body in corpus:
        # 全文比對而非任意子字串，避免摘要引用了新聞的一兩句話就誤判——
        # 自製摘要合理引用原文片語是允許的，全文轉載才是規格禁止的
        if body and len(body) > 200 and body in serialised:
            raise AssertionError(
                f"公開回應包含受著作權保護文件的全文（前 40 字：{body[:40]}…）"
            )
