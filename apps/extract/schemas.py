"""L1 結構化抽取的 schema 與提示詞（規格 §4.2、任務 14）。

三種 schema，依文件領域選用。分開設計而非用單一通用 schema，
理由是**抽取品質與欄位具體度成正比**——問「有哪些實體」得到的是
模糊清單，問「被告是誰、案號多少、判幾年」得到的是可用的結構。

    judicial     司法案件：被告、案號、法院、審級、罪名、刑度、程序階段
    corruption   政商弊案：標案、公司、金流、涉案機關
    generic      其他：人、組織、地點、事由

**抽取只記錄文件明確陳述的內容。** 這不是禮貌性的提醒——L1 的輸出會
成為時間線事實條目與事件歸屬的依據，一個被推測出來的刑度或案號會
一路傳遞下去，且沒有任何下游環節能發現它是假的。因此每個 schema 都
明確要求「文件未載明者留空」，並在提示詞中重複這項要求。
"""
from __future__ import annotations

__all__ = ["SchemaKind", "SCHEMAS", "choose_schema", "build_messages"]


class SchemaKind:
    JUDICIAL = "judicial"
    CORRUPTION = "corruption"
    GENERIC = "generic"


# ---------------------------------------------------------------- JSON schema

_JUDICIAL_SCHEMA = {
    "case_numbers": "文中出現的裁判書案號，如「111年度金重訴字第123號」。陣列，未載明則為空陣列",
    "defendants": "被告或犯罪嫌疑人。陣列，每項為 {name, role}；role 如「前市長」「公司負責人」",
    "court": "審理法院，**照原文寫法**（原文寫「北院」就填「北院」，不要展開為全名）。未載明則為空字串",
    "prosecutor_office": "偵辦的檢察機關，**照原文寫法**。未載明則為空字串",
    "instance": "審級：偵查／一審／二審／三審／更審／再審／執行。未載明則為空字串",
    "charges": "罪名。陣列，如「貪污治罪條例第4條」「偽造文書」",
    "stage": "程序階段：起訴／不起訴／緩起訴／羈押／交保／開庭／宣判／上訴／定讞／其他",
    "sentence": "主刑，照原文的核心用語，如「有期徒刑12年」「無罪」「死刑」。附帶條件（易科罰金、緩刑條件等）另填 sentence_details。未宣判則為空字串",
    "sentence_details": "刑度的附帶條件，如「得易科罰金」「緩刑4年」「付保護管束」。陣列，無則為空陣列",
    "is_final": "判決是否已確定（定讞）。布林值；未載明則為 false",
    "occurred_on": "本文所述司法動作的發生日期，格式 YYYY-MM-DD。未載明則為空字串",
    "summary": "一句話摘要本文陳述的司法進展，20-40 字，僅陳述文中明載的事實",
}

_CORRUPTION_SCHEMA = {
    "tender_ids": "標案編號。陣列，未載明則為空陣列",
    "tender_names": "標案或工程名稱。陣列",
    "companies": "涉案公司。陣列，每項為 {name, tax_id}；tax_id 為統一編號，未載明則為空字串",
    "agencies": "涉案的政府機關或公營事業。陣列",
    "persons": "涉案人。陣列，每項為 {name, role}",
    "amounts": "涉及金額。陣列，每項為 {value, currency, description}；value 為數字（新台幣元）",
    "irregularity": "弊端類型：綁標／圍標／回扣／圖利／收賄／利益輸送／其他。未載明則為空字串",
    "occurred_on": "本文所述事件的發生日期，格式 YYYY-MM-DD。未載明則為空字串",
    "summary": "一句話摘要，20-40 字，僅陳述文中明載的事實",
}

_GENERIC_SCHEMA = {
    "persons": "文中的主要人物。陣列，每項為 {name, role}",
    "organizations": "文中的組織、機關或公司。陣列",
    "locations": "地點。陣列",
    "occurred_on": "本文所述事件的發生日期，格式 YYYY-MM-DD。未載明則為空字串",
    "topic": "事件主題，10 字以內",
    "summary": "一句話摘要，20-40 字，僅陳述文中明載的事實",
}

SCHEMAS: dict[str, dict[str, str]] = {
    SchemaKind.JUDICIAL: _JUDICIAL_SCHEMA,
    SchemaKind.CORRUPTION: _CORRUPTION_SCHEMA,
    SchemaKind.GENERIC: _GENERIC_SCHEMA,
}


# ---------------------------------------------------------------- schema 選用

def choose_schema(relevance_signals: dict) -> str:
    """依相關性過濾的命中類別選 schema。

    以規則決定而非再問一次 LLM——多一次呼叫要花錢，而過濾階段的
    命中類別已經足以判斷領域。

    司法優先於弊案：弊案報導幾乎都伴隨司法程序，而司法 schema 的
    欄位（案號、審級、刑度）正是事件時間線最需要的節點。
    """
    categories = set((relevance_signals or {}).get("categories", []))

    if categories & {"judicial_process", "judicial_body"}:
        return SchemaKind.JUDICIAL
    if categories & {"corruption_charge", "corruption_context", "oversight"}:
        return SchemaKind.CORRUPTION
    return SchemaKind.GENERIC


# ---------------------------------------------------------------- 提示詞

_SYSTEM_PROMPT = """你是台灣司法與政經新聞的結構化資訊抽取器。

規則（違反任何一項都會讓下游的事件時間線出現錯誤資訊）：

1. 只抽取文件**明確載明**的內容。不推測、不補充常識、不從其他案件套用。
2. 文件未提及的欄位一律留空（字串用 ""，陣列用 []，布林用 false）。
   留空是正確答案，猜測不是。
3. **人名、機關名、案號一律照原文**，不簡化、不補全、不展開簡稱。
   原文寫「北院」就填「北院」，不要改成「臺灣臺北地方法院」——
   名稱的正規化由程式處理，你補全反而可能猜錯轄區。
4. 日期一律轉為 YYYY-MM-DD。文中若為民國年（如「111年3月5日」），
   請換算為西元（民國年 + 1911）。無法確定則留空。
5. 摘要只陳述文中明載的事實，不加評價、不推論因果。

只輸出 JSON，不要有其他文字。"""


def build_messages(
    *, title: str, body: str, schema_kind: str, max_body_chars: int = 6000
) -> list[dict[str, str]]:
    """組裝抽取用的訊息。

    ``max_body_chars`` 截斷過長內文以控制成本。截斷取前段——
    新聞的倒金字塔結構把關鍵事實放在前面，尾段多為背景與引述。
    """
    schema = SCHEMAS[schema_kind]
    fields = "\n".join(f'  "{key}": {desc}' for key, desc in schema.items())

    body_text = (body or "")[:max_body_chars]
    truncated = "\n（內文過長，已截斷）" if len(body or "") > max_body_chars else ""

    user = (
        f"請依下列欄位抽取資訊：\n\n{{\n{fields}\n}}\n\n"
        f"---\n標題：{title}\n\n內文：\n{body_text}{truncated}"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
