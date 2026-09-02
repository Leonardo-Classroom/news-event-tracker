"""與 AI 討論並產出候選事件（任務 63）。

**AI 只提議，不建立。** 每一輪對話回傳「回覆文字」與「候選事件清單」，
候選事件寫進 ``EventSuggestion``；真正的 ``Event`` 只有在使用者按下
「建立事件」時才產生。這條界線是刻意的——事件一旦建立就進入審核與
公開流程，不該由模型自行決定。

**成本控制。** 走便宜檔（``LLM_MODEL_CHEAP``），且每次只送最近
``_HISTORY_LIMIT`` 輪對話而非整串歷史：討論會越拉越長，全量重送會讓
每一輪的成本隨對話長度線性成長。預算閘門在 ``LlmProvider`` 內部，
這裡不需要也不應該重複檢查。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re

from django.utils import timezone

from apps.eventbuilder.models import Conversation, EventSuggestion, Message, Role
from apps.ingest.models import Document
from apps.llm.models import LlmPurpose
from apps.llm.provider import LlmError, get_provider
from apps.retrieval.hybrid import HybridRetriever
from apps.retrieval.keyword import BigramFtsBackend

logger = logging.getLogger(__name__)

#: 每次送給模型的歷史輪數。討論會越拉越長，全量重送會讓成本隨長度
#: 線性成長；事件討論的上下文通常集中在最近幾輪。
_HISTORY_LIMIT = 12

_SYSTEM_PROMPT = """你是台灣重大司法案件與政商弊案的追蹤助理。
使用者想建立「持續追蹤的事件」。事件的定義是：一個有明確主體、
會隨司法或行政程序推進而產生新進展的具體案件，例如「京華城容積案」
「新竹棒球場案」。不是新聞類別（如「食安問題」），也不是單一則報導。

你的工作有兩件：
1. 與使用者討論，釐清他想追蹤什麼。問題不清楚時主動追問。
2. 當討論足以形成具體事件時，提出候選事件。

嚴格以 JSON 回覆，格式：
{"reply": "給使用者看的回覆，繁體中文，不要提到 JSON",
 "suggestions": [{"title": "事件名稱（不超過 30 字）",
                  "summary": "兩三句話說明追蹤什麼、目前已知進展",
                  "tags": ["涉案人或機關", "案件類型", "地區"]}]}

沒有足夠資訊形成具體事件時，suggestions 給空陣列，用 reply 追問。
不要重複提出使用者已經看過的候選事件。

若系統提供了「館藏文件」清單，從中挑出真正與討論相關的，把它們的
編號放進 documents 欄位（最多 5 筆）：
{"reply": "...", "suggestions": [...], "documents": [12, 34]}
**只能引用清單裡的編號。** 清單沒有的就不要放，也不要自己寫標題或
網址——你不知道那些新聞是否真的存在。清單裡沒有相關的就給空陣列。"""

_TITLE_PROMPT = """用不超過 16 個繁體中文字，為這段對話下一個標題。
只回覆標題本身，不要引號、不要標點、不要說明。"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
        },
        "documents": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["reply"],
}


@dataclasses.dataclass
class TurnResult:
    reply: str
    suggestions: list[EventSuggestion]
    documents: list = dataclasses.field(default_factory=list)
    error: str = ""


def default_title(now=None) -> str:
    return timezone.localtime(now or timezone.now()).strftime("%Y-%m-%d %H:%M")


def _history(conversation: Conversation) -> list[dict[str, str]]:
    recent = list(conversation.messages.order_by("-created_at", "-id")[:_HISTORY_LIMIT])
    recent.reverse()
    return [{"role": m.role, "content": m.content} for m in recent]


def _existing_titles(conversation: Conversation) -> list[str]:
    return list(conversation.suggestions.alive().values_list("title", flat=True))


#: 一次提供給模型的館藏候選數。太少會讓它挑不到相關的，太多會讓
#: prompt 變長、成本上升——這裡只需要「有沒有相關報導」的線索。
_DOC_CANDIDATES = 12


#: 候選裡保留幾筆給「最新」。相關性最高的通常是報導最密集的那段
#: 時間（京華城案實測前三筆全是宣判日 2026-03-26），使用者問
#: 「有沒有新一點的」時，純靠相關性排序永遠拿不到後續進展——而
#: 後續進展正是這個系統存在的理由。
_RECENT_SLOTS = 4

#: 取「最新」之前，先用相關性篩出多大的池子。太小會退化成純相關性
#: 排序（拿不到後續進展），太大會讓不相關的新文章混進來。
_RECENT_POOL = 300


def conversation_query(conversation: Conversation, text: str) -> str:
    """用對話脈絡組查詢，不是只用最後一則訊息。

    **這是實測發現的缺陷**：使用者的追問常常沒有任何主題詞
    （「有沒有新一點的新聞」「都行，看看近期的」），只拿那句話去
    檢索會撈到除濕機與客語電影，AI 於是回答「館藏沒有相關報導」
    ——但那是因為我們根本沒把相關文件給它。
    """
    recent = [m.content for m in
              conversation.messages.filter(role=Role.USER)
              .order_by("-created_at", "-id")[:3]]
    recent.reverse()
    parts = recent + [text]
    # 去重並保持順序：連續追問常有重複字眼，重複只會稀釋查詢。
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out)[:500]


def retrieve_candidates(text: str, *, limit: int = _DOC_CANDIDATES) -> list[Document]:
    """從館藏找出相關文件：相關性為主，但保留名額給最新的。

    走 ``HybridRetriever``（向量＋關鍵字 RRF 融合）而非只有關鍵字：
    bigram 是字面比對，「不利於民進黨」不會命中「某綠營人士涉貪」
    這類語意相關但用詞不同的文件。向量通道正是為此存在。
    """
    text = (text or "").strip()
    if not text:
        return []
    base = Document.objects.exclude(raw_body="").select_related("source")
    ranked: list[Document] = []
    newest: list[Document] = []

    try:
        hits = HybridRetriever().search(text, base=base, top_k=limit * 4)
        ids = [c.document_id for c in hits]
        by_id = {d.pk: d for d in base.filter(pk__in=ids)}
        ranked = [by_id[i] for i in ids if i in by_id]
    except Exception as exc:                       # noqa: BLE001
        # 檢索失敗不該讓整輪對話失敗——沒有推薦新聞仍然可以討論。
        logger.warning("建立事件的相關性檢索失敗：%s", exc)

    # **最新的必須從「命中集合」取，不能從已排序截斷的前段取。**
    # 實測：黃景茂涉京華城案判 6 年半（2026-09-02）確實命中關鍵字
    # 通道，卻排在 RRF 前 48 名之外——若只從前段挑最新，這種「案件
    # 的最新進展」永遠浮不上來，而那正是本系統存在的理由。
    try:
        keyword = BigramFtsBackend()
        matched = keyword.search(base, text, mode="any")
        # **先用相關性篩出池子，再在池子裡取最新。** 直接對命中集合
        # 按日期排序是行不通的：mode="any" 會命中任何字面沾到邊的
        # 文件，取最新等於「最近入庫的任何新聞」——實測撈到 YouTube
        # 廣告與 C 肝群聚。ts_rank 前段才是真的相關。
        pool = list(keyword.rank(matched, text)
                    .filter(published_at__isnull=False)[:_RECENT_POOL])
        pool.sort(key=lambda d: d.published_at, reverse=True)
        newest = pool[:_RECENT_SLOTS]
    except Exception as exc:                       # noqa: BLE001
        logger.warning("建立事件的最新報導檢索失敗：%s", exc)

    if not ranked and not newest:
        return []

    merged, seen = [], set()
    for doc in newest + ranked:          # 最新的排前面
        if doc.pk not in seen:
            seen.add(doc.pk)
            merged.append(doc)
    return merged[:limit]


def _candidate_block(documents: list[Document]) -> str:
    lines = []
    for doc in documents:
        when = doc.published_at.strftime("%Y-%m-%d") if doc.published_at else "日期不明"
        lines.append(f"{doc.pk}｜{when}｜{doc.source.name}｜{doc.title}")
    return ("館藏文件（只能引用這些編號，已含最新報導）：\n"
            + "\n".join(lines))


#: 模型偶爾會回傳截斷或非 JSON 的內容（2026-09-03 實測：同一個 prompt
#: 連跑 3 次都正常，但使用者實際遇到過一次空回應、一次在
#: `"suggestions":[],` 就斷掉）。這是間歇性故障，重試一次就好——
#: 重試太多次只是把使用者的等待時間拉長。
_RETRIES = 1

#: 從截斷的 JSON 裡救出 reply。實測失敗案例中 reply 本身是完整的，
#: 只有後面的欄位被切掉——與其整輪失敗，不如至少把話講完。
_REPLY_RE = re.compile(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _ask(provider, messages) -> tuple[dict | None, str]:
    """呼叫模型並解析 JSON。回傳 ``(payload, 錯誤訊息)``。"""
    last = ""
    for attempt in range(_RETRIES + 1):
        try:
            response = provider.complete(
                messages=messages, purpose=LlmPurpose.EVENT_SUMMARY,
                json_schema=_SCHEMA, temperature=0.3,
                task_name="eventbuilder.turn",
            )
        except LlmError as exc:
            return None, str(exc)

        try:
            return response.json(), ""
        except (json.JSONDecodeError, ValueError):
            pass

        # finish_reason 必須檢查：撞上 max_tokens 而被截斷的 JSON，
        # 表面上看起來就是「解析失敗」，會把問題指向錯誤的方向
        # （LlmResponse.finish_reason 的註解已經警告過這件事）。
        if response.finish_reason == "length":
            last = "回應過長被截斷，請把問題描述得更聚焦一些"
            break

        salvaged = _salvage_reply(response.text)
        if salvaged and attempt >= _RETRIES:
            # 救得回 reply 就別讓整輪失敗——候選事件下一輪還會再提。
            logger.info("模型回傳的 JSON 不完整，已救回 reply（%d 字）",
                        len(salvaged))
            return {"reply": salvaged, "suggestions": [], "documents": []}, ""
        last = "AI 回覆格式異常，請再說一次"
    return None, last or "AI 回覆格式異常，請再說一次"


def _salvage_reply(text: str) -> str:
    match = _REPLY_RE.search(text or "")
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"')
    except (json.JSONDecodeError, ValueError):
        return ""


def send_message(conversation: Conversation, text: str, *, provider=None) -> TurnResult:
    """送出一則使用者訊息，取得回覆與新的候選事件。"""
    text = (text or "").strip()
    if not text:
        return TurnResult(reply="", suggestions=[], error="訊息不可為空")

    Message.objects.create(conversation=conversation, role=Role.USER, content=text)

    provider = provider or get_provider()
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    seen = _existing_titles(conversation)
    if seen:
        messages.append({
            "role": "system",
            "content": "使用者已看過這些候選事件，不要重複提出：" + "、".join(seen),
        })
    candidates = retrieve_candidates(conversation_query(conversation, text))
    if candidates:
        messages.append({"role": "system",
                         "content": _candidate_block(candidates)})
    messages += _history(conversation)

    payload, error = _ask(provider, messages)
    if payload is None:
        logger.warning("建立事件對話失敗：%s", error)
        # 使用者的訊息已經寫進去了，不回滾——讓他看得到自己說過什麼，
        # 重試時也不必重打。
        return TurnResult(reply="", suggestions=[], error=error)

    reply = (payload.get("reply") or "").strip()
    assistant = Message.objects.create(conversation=conversation,
                                       role=Role.ASSISTANT, content=reply)

    # **只認得候選清單裡的編號。** 模型可能回傳不存在的 id（幻覺）或
    # 我們沒提供的 id；以本輪候選的集合為準做交集，其餘一律丟棄。
    allowed = {d.pk: d for d in candidates}
    cited = [allowed[i] for i in _as_ints(payload.get("documents"))
             if i in allowed][:5]
    if cited:
        assistant.documents.set(cited)

    created: list[EventSuggestion] = []
    for item in payload.get("suggestions") or []:
        title = (item.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.append(title)
        tags = [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()]
        created.append(EventSuggestion.objects.create(
            conversation=conversation,
            title=title[:256],
            summary=(item.get("summary") or "").strip(),
            tags=tags[:8],
        ))

    conversation.save(update_fields=["updated_at"])
    if not conversation.title_generated:
        _rename_by_topic(conversation, provider=provider)
    return TurnResult(reply=reply, suggestions=created, documents=cited)


def _as_ints(values) -> list[int]:
    out = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def create_event_from(suggestion: EventSuggestion):
    """把候選事件變成真正的 ``Event``，狀態為「追蹤中」。

    **直接進 ACTIVE 而非 CANDIDATE。** CANDIDATE 是給自動偵測
    （HDBSCAN 叢集）的產物用的——那些需要人判斷「這到底是不是一個
    事件」。這裡的候選事件是使用者在對話中親自看過、按下按鈕確認的，
    等同已經完成人工判斷；再丟回待審核佇列只是讓他把同一件事做兩次。

    ``visibility`` 維持預設的 PRIVATE：加入追蹤與對外公開是兩回事，
    公開仍須走審核（任務 45）。
    """
    from django.utils.text import slugify

    from apps.events.models import Event, EventStatus, RiskTier

    if suggestion.created_event_id:
        return suggestion.created_event

    base = slugify(suggestion.title, allow_unicode=True)[:40] or "event"
    slug = base
    n = 2
    while Event.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1

    now = timezone.now()
    event = Event.objects.create(
        slug=slug,
        title=suggestion.title[:256],
        summary=suggestion.summary,
        status=EventStatus.ACTIVE,
        # 保守起見一律高風險：這些多半是具名自然人的未決案件，
        # 分級只影響審核強度，寧可多審不可少審（規格 §4.3.1）。
        risk_tier=RiskTier.HIGH,
        first_seen_at=now,
        last_progress_at=now,
    )
    suggestion.created_event = event
    suggestion.save(update_fields=["created_event"])
    return event


def _rename_by_topic(conversation: Conversation, *, provider) -> None:
    """第一輪討論後把預設的日期時間標題換成主題名稱。

    失敗不影響對話本身——標題只是方便辨識，不值得讓整輪對話失敗。
    只做一次（``title_generated``），之後使用者自己改的名字不該被覆寫。
    """
    try:
        response = provider.complete(
            messages=[{"role": "system", "content": _TITLE_PROMPT},
                      *_history(conversation)],
            purpose=LlmPurpose.OTHER, temperature=0.3, max_tokens=64,
            task_name="eventbuilder.title",
        )
        title = " ".join(response.text.split()).strip("「」\"' 。")
    except (LlmError, ValueError) as exc:
        logger.info("對話標題生成失敗，保留預設名稱：%s", exc)
        return

    if title:
        conversation.title = title[:128]
        conversation.title_generated = True
        conversation.save(update_fields=["title", "title_generated", "updated_at"])
