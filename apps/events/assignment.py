"""事件自動歸屬（任務 23，ADR-0005）。

兩階段，識別碼優先：

    1. 識別碼精確比對——文件的案號／統編與事件的案號有交集，直接歸屬，
       不呼叫 LLM。這是唯一「確定」的訊號，不需要語意判斷。
    2. Hybrid 召回 + LLM 判定——對每個事件用任務 19 校準過的
       `EventQuery`（身分詞＋涉案人分離）取關鍵字候選，未被識別碼
       歸屬的候選批次送 LLM 判定是否真的屬於該事件。

**必須對全部文件執行，不限通過相關性過濾者。** 任務 22 人工歸屬時
發現：事件的行政與工程階段（驗收、監察院糾正、罷免攻防）沒有司法
詞彙，僅對通過相關性過濾的文件跑歸屬會系統性漏掉這段（新竹棒球場案
40 篇裡漏了 15 篇）。因此本模組的候選查詢一律針對
``Document.objects.all()``（僅排除轉載重複），不套用 ``relevant()``。

**LLM 只用於已進入候選視窗的文件，不是全庫。** 這是控制成本的關鍵：
關鍵字召回先把 23.8 萬篇篩到每個事件數百篇的候選集合，LLM 只需要對
候選集合做「真的屬於嗎」的二元判斷，而非從全庫大海撈針。
"""
from __future__ import annotations

import dataclasses
import json
import logging

from django.db.models import QuerySet

from apps.events.models import AssignmentMethod, Event, EventDocument
from apps.events.terms import build_event_query
from apps.ingest.models import Document
from apps.llm.models import LlmPurpose
from apps.llm.provider import LlmProvider, get_provider
from apps.retrieval.hybrid import HybridRetriever
from apps.retrieval.keyword import BigramFtsBackend

logger = logging.getLogger(__name__)

__all__ = [
    "AssignmentResult", "identifier_match", "candidate_documents_for_event",
    "judge_candidates", "auto_assign",
]

#: 候選視窗大小。任務 19 校準召回率時發現，事件核心詞的 bigram 命中
#: 在 23.8 萬篇語料上可達數百篇，取太小會系統性漏掉正例（實測 100 會漏
#: 88%），但這裡只做 keyword 召回（成本可忽略），可以取得比檢索量測
#: 更大一些，交給 LLM 篩掉多數雜訊。
CANDIDATE_TOP_K = 300

#: 每次 LLM 呼叫判定的候選數。批次是為了攤提固定的 system prompt
#: 成本——20 篇一批而非逐篇呼叫，同樣的判斷品質下呼叫次數少 20 倍。
BATCH_SIZE = 20

_SYSTEM_PROMPT = """你是新聞事件歸屬判定器。給你一個事件的描述與一批候選文件
（標題＋摘要），判斷每篇文件是否**確實在報導這個事件**，而非只是提到
相同的人名或地名卻在講別的事。

判斷原則：
1. 事件行政與工程階段的報導（驗收、糾正、罷免攻防、監察院調查）
   算屬於該事件，即使沒有司法詞彙。
2. 只是提到同一人物但主題無關（例如同一位市長的其他政績新聞）不算屬於。
3. 同一場館／地點的其他新聞（賽事、演唱會等與弊案無關者）不算屬於。
4. 不確定時傾向判定不屬於——寧可漏掉交給下次批次重新評估，
   也不要把無關文件誤歸進事件時間線。

只輸出 JSON，格式：{"assignments": [{"doc_id": 整數, "belongs": true 或 false,
"reason": "十字以內的理由"}, ...]}，候選文件全部要有對應項目。"""


@dataclasses.dataclass(frozen=True)
class AssignmentResult:
    document_id: int
    event: Event
    method: str
    reason: str


def identifier_match(document: Document, events: list[Event]) -> Event | None:
    """案號精確比對。多個事件同時命中時無法判斷，交給 LLM 階段處理
    （回傳 None，該文件仍會進入候選召回，由 LLM 判定歸屬哪一個）。
    """
    doc_cases = set(document.case_numbers or [])
    if not doc_cases:
        return None
    matched = [e for e in events if doc_cases & set(e.case_numbers or [])]
    return matched[0] if len(matched) == 1 else None


def candidate_documents_for_event(
    event: Event, retriever: HybridRetriever, *, base: QuerySet, top_k: int = CANDIDATE_TOP_K
) -> list[int]:
    """該事件的關鍵字候選文件 id。僅用 keyword 通道——這裡的目的是
    控制 LLM 判定量，不是量測召回率，向量通道的算力成本不值得在此付。
    """

    def frequency(term: str) -> float:
        matched = BigramFtsBackend().search(base, term, mode="all").count()
        total = base.count()
        return matched / max(total, 1)

    query = build_event_query(event, document_frequency=frequency)
    if not query:
        return []
    results = retriever.search(query, base=base, top_k=top_k, channels=("keyword",))
    return [c.document_id for c in results]


def _snippet(document: Document, *, max_chars: int = 200) -> str:
    return (document.raw_body or "")[:max_chars].replace("\n", " ")


def judge_candidates(
    event: Event, documents: list[Document], *, provider: LlmProvider,
) -> list[AssignmentResult]:
    """批次向 LLM 詢問一批候選文件是否屬於某事件。"""
    if not documents:
        return []

    event_desc = (
        f"事件：{event.title}\n涉案人／機關：{', '.join(event.core_terms) or '（無登記）'}"
    )
    candidates = [
        {"doc_id": doc.pk, "title": doc.title, "snippet": _snippet(doc)}
        for doc in documents
    ]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"{event_desc}\n\n候選文件：\n{json.dumps(candidates, ensure_ascii=False)}"},
    ]

    response = provider.complete(
        messages=messages, purpose=LlmPurpose.EVENT_LINKING,
        json_schema={"type": "object"}, task_name=f"assign:{event.slug}",
        max_tokens=4096,
    )

    # 先檢查截斷，理由與 apps/extract/service.py 相同：輸出撞上
    # max_tokens 時 JSON 必然不完整，若不先檢查，截斷的 JSON 會以
    # 「解析失敗」的面貌出現，看起來像模型判斷全部不屬於，
    # 而不是「這批本來就沒判完」——兩者需要的後續動作完全不同
    # （前者該調整批次大小或 max_tokens，後者才是真的無候選）。
    if response.finish_reason == "length":
        logger.warning(
            "事件 %s 的歸屬判定輸出被截斷（達 max_tokens 上限），"
            "本批 %d 筆候選結果捨棄，不採計任何判定",
            event.slug, len(documents),
        )
        return []

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        logger.warning("事件 %s 的歸屬判定回應無法解析為 JSON", event.slug)
        return []

    results = []
    for item in payload.get("assignments", []):
        if item.get("belongs"):
            results.append(AssignmentResult(
                document_id=item["doc_id"], event=event, method=AssignmentMethod.LLM,
                reason=item.get("reason", ""),
            ))
    return results


def auto_assign(
    events: list[Event], *, provider: LlmProvider | None = None,
    max_llm_calls: int | None = None, persist: bool = True,
) -> list[AssignmentResult]:
    """對一批事件執行自動歸屬，回傳所有新建立的 ``AssignmentResult``。

    已歸屬給任一事件的文件會被排除，不重新judge——避免重複呼叫 LLM
    評估已經人工或先前自動確認過的文件。

    Args:
        max_llm_calls: 限制本次執行最多呼叫幾次 LLM，供成本控管
            （每次呼叫仍受 ``apps.llm.budget`` 的預算硬上限保護，
            這個參數是額外的、呼叫端主動設定的節流）。
        persist: **每個批次判定完就立刻寫入資料庫**，而非蒐集完全部
            事件才一次寫入。這不是效能考量——LLM 呼叫的花費在呼叫當下
            就已經發生且無法收回；若把寫入延到最後才做，一次逾時或
            中斷就會讓已經花掉的錢對應到零筆寫入的結果。實測撞過這個
            問題：一次 10 分鐘逾時的執行花了 US$0.086（38 次呼叫），
            因為寫入寫在最後，全部判定結果隨進程終止而遺失，
            EventDocument 一筆都沒增加。
    """
    provider = provider or get_provider()
    retriever = HybridRetriever()
    base = Document.objects.filter(canonical_of__isnull=True)
    already_assigned = set(EventDocument.objects.values_list("document_id", flat=True))

    results: list[AssignmentResult] = []
    llm_calls = 0

    for event in events:
        candidate_ids = [
            doc_id for doc_id in candidate_documents_for_event(event, retriever, base=base)
            if doc_id not in already_assigned
        ]
        if not candidate_ids:
            continue

        documents = list(Document.objects.filter(pk__in=candidate_ids))

        # 識別碼優先，不需要 LLM
        remaining = []
        identifier_results = []
        for doc in documents:
            matched = identifier_match(doc, [event])
            if matched:
                identifier_results.append(AssignmentResult(
                    document_id=doc.pk, event=event, method=AssignmentMethod.IDENTIFIER,
                    reason=f"案號精確比對：{set(doc.case_numbers) & set(event.case_numbers)}",
                ))
                already_assigned.add(doc.pk)
            else:
                remaining.append(doc)
        if identifier_results:
            if persist:
                apply_assignments(identifier_results)
            results.extend(identifier_results)

        for i in range(0, len(remaining), BATCH_SIZE):
            if max_llm_calls is not None and llm_calls >= max_llm_calls:
                break
            batch = remaining[i:i + BATCH_SIZE]
            batch_results = judge_candidates(event, batch, provider=provider)
            llm_calls += 1
            new_results = [r for r in batch_results if r.document_id not in already_assigned]
            if new_results:
                if persist:
                    apply_assignments(new_results)
                for r in new_results:
                    already_assigned.add(r.document_id)
                results.extend(new_results)
            logger.info("事件 %s：批次 %d 完成，判定 %d/%d 篇屬於（累計呼叫 %d 次）",
                       event.slug, i // BATCH_SIZE + 1, len(new_results), len(batch), llm_calls)

    return results


def apply_assignments(results: list[AssignmentResult]) -> int:
    """把 ``AssignmentResult`` 寫入 ``EventDocument``，並重算受影響事件的
    風險分級與最後進展時間。回傳寫入筆數。
    """
    created = 0
    touched_events: dict[int, Event] = {}
    for r in results:
        _, was_created = EventDocument.objects.get_or_create(
            event=r.event, document_id=r.document_id,
            defaults={"method": r.method, "reason": r.reason},
        )
        if was_created:
            created += 1
            touched_events[r.event.pk] = r.event

    for event in touched_events.values():
        event.recompute_risk_tier(save=True)

    return created
