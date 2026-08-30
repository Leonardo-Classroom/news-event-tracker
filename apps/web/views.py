"""內部介面。

**以工作為中心，不以資料表為中心。** Django admin 的組織方式是
「一個模型一個頁面」，但使用者實際要做的事是「看某個案子進行到哪」
「這批抓取有沒有問題」——這些都跨越多個模型。

頁面因此對應工作而非資料表：

    /            追蹤中的事件
    /e/<slug>/   單一事件的時間線（核心畫面）
    /review/     待審核的事件（Scope 6 的入口）
    /documents/  文件檢索
    /pipeline/   管線狀態與來源健康度
    /crawlers/   爬蟲排程與手動觸發
    /costs/      LLM 用量與成本
"""
from __future__ import annotations

import datetime as dt

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, Min, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.events.models import (
    DORMANT_AFTER_DAYS, Event, EventDocument, EventStatus,
)
from apps.ingest.models import Document, ExternalSession, Source
from apps.ingest.services import FAILURE_THRESHOLD, effective_interval_minutes, should_poll
from apps.ingest.tasks import dispatch_poll
from apps.llm.budget import approved_usd, remaining_usd, spent_usd
from apps.llm.models import LlmPurpose, LlmUsage

#: 時間線上超過此天數的間隔會被標示為「空白期」。
#: 這是本系統的價值所在——「新聞停了」本身就是資訊，不該被壓縮掉。
GAP_HIGHLIGHT_DAYS = 45


@login_required
def events(request):
    """追蹤中的事件。首頁——事件是本系統的核心物件。"""
    status = request.GET.get("status", "")
    queryset = (Event.objects.exclude(status=EventStatus.REJECTED)
                .annotate(doc_count=Count("event_documents"))
                .order_by("-last_progress_at"))
    if status:
        queryset = queryset.filter(status=status)
    else:
        queryset = queryset.filter(
            status__in=[EventStatus.ACTIVE, EventStatus.DORMANT, EventStatus.CLOSED])

    now = timezone.now()
    rows = []
    for event in queryset:
        days = (now - event.last_progress_at).days if event.last_progress_at else None
        rows.append({"event": event, "days": days})

    return render(request, "web/events.html", {
        "nav": "events", "rows": rows, "status": status,
        "statuses": EventStatus.choices,
        "dormant_after": DORMANT_AFTER_DAYS,
    })


@login_required
def event_detail(request, slug):
    """單一事件的時間線。**這是本系統最核心的畫面。**

    時間線刻意標示出「報導空白期」——規格 §2.1 的問題陳述正是
    「新聞的報導密度呈雙峰分布，中間長期空白」，而系統存在的理由
    就是填補那段空白。把空白畫出來，才能看出填補了沒有。
    """
    event = get_object_or_404(Event, slug=slug)
    # nulls_last=True：理由同 documents 視圖——DESC 排序若不指定，
    # 缺日期的文件會被排到時間線最前面，看起來像是「最新進展」。
    links = (EventDocument.objects
             .filter(event=event)
             .select_related("document", "document__source")
             .order_by(F("document__published_at").desc(nulls_last=True)))

    # 由新到舊排列，並在相鄰節點間插入空白期標記
    items = []
    previous = None
    for link in links:
        doc = link.document
        if previous is not None and doc.published_at and previous.published_at:
            gap = (previous.published_at - doc.published_at).days
            if gap >= GAP_HIGHLIGHT_DAYS:
                items.append({"gap": gap,
                              "from": doc.published_at,
                              "to": previous.published_at})
        items.append({"link": link, "doc": doc})
        previous = doc

    dates = [l.document.published_at for l in links if l.document.published_at]
    span_days = (max(dates) - min(dates)).days if len(dates) > 1 else 0
    months = len({d.strftime("%Y-%m") for d in dates})
    span_months = 0
    if dates:
        lo, hi = min(dates), max(dates)
        span_months = (hi.year - lo.year) * 12 + hi.month - lo.month + 1

    return render(request, "web/event_detail.html", {
        "nav": "events", "event": event, "items": items,
        "doc_count": links.count(),
        "span_days": span_days,
        "months_with_news": months,
        "span_months": span_months,
        "silent_months": max(0, span_months - months),
        "sources": links.values("document__source__slug").distinct().count(),
    })


@login_required
def review(request):
    """待審核的事件。Scope 6 的安全閘門將接在此處。"""
    pending = (Event.objects
               .filter(status__in=[EventStatus.CANDIDATE, EventStatus.DRAFT])
               .annotate(doc_count=Count("event_documents"))
               .order_by("-created_at"))
    return render(request, "web/review.html", {"nav": "review", "events": pending})


@login_required
def documents(request):
    """文件檢索。搜尋走 bigram FTS 的 GIN 索引（ADR-0001）。"""
    query = request.GET.get("q", "").strip()
    source = request.GET.get("source", "")
    only = request.GET.get("only", "")

    # nulls_last=True：PostgreSQL 對 DESC 排序預設把 NULL 排在最前面，
    # 若不指定，僅 0.2% 缺日期的文件會佔滿列表最前面幾頁，
    # 讓「大部分文件沒有日期」看起來像是普遍問題而非罕見的資料缺陷。
    queryset = (Document.objects.select_related("source")
                .defer("raw_body", "embedding", "search_text")
                .order_by(F("published_at").desc(nulls_last=True)))
    if query:
        from apps.retrieval.keyword import BigramFtsBackend
        queryset = BigramFtsBackend().search(queryset, query)
    if source:
        queryset = queryset.filter(source__slug=source)
    if only == "relevant":
        queryset = queryset.filter(relevant=True)
    elif only == "case":
        queryset = queryset.exclude(case_numbers=[])
    elif only == "vector":
        queryset = queryset.exclude(embedding=None)

    page = Paginator(queryset, 50).get_page(request.GET.get("page"))
    # 帶省略號的頁碼範圍在 view 端算好——模板不支援呼叫帶參數的方法
    # （get_elided_page_range 需要 number／on_each_side／on_ends 參數）。
    page_range = page.paginator.get_elided_page_range(
        page.number, on_each_side=2, on_ends=1)
    return render(request, "web/documents.html", {
        "nav": "documents", "page": page, "page_range": page_range, "q": query,
        "source": source, "only": only,
        "sources": Source.objects.order_by("name").values("slug", "name"),
    })


@login_required
def document_detail(request, pk):
    doc = get_object_or_404(
        Document.objects.select_related("source", "canonical_of"), pk=pk)
    return render(request, "web/document_detail.html", {
        "nav": "documents", "doc": doc,
        "extractions": doc.extractions.order_by("-created_at")[:5],
        "events": Event.objects.filter(event_documents__document=doc),
    })


@login_required
def pipeline(request):
    """管線狀態。

    來源健康度是這裡最重要的東西——fixture 測試偵測不到「網站改版了」，
    只有連續失敗計數能在生產環境發現，而那個數字若沒有人看等於不存在。
    """
    now = timezone.now()
    sources = (Source.objects.filter(enabled=True)
               .annotate(docs=Count("documents"),
                         latest=Max("documents__published_at"))
               .order_by("-consecutive_failures", "slug"))
    rows = []
    for source in sources:
        stale_hours = ((now - source.last_success_at).total_seconds() / 3600
                       if source.last_success_at else None)
        rows.append({
            "source": source,
            "interval": effective_interval_minutes(source),
            "throttled": effective_interval_minutes(source) != source.poll_interval_minutes,
            "stale_hours": stale_hours,
            "unhealthy": source.consecutive_failures >= FAILURE_THRESHOLD,
        })

    total = Document.objects.count()
    relevant = Document.objects.relevant().count()
    with_body = Document.objects.exclude(raw_body="").count()
    vectorised = Document.objects.exclude(embedding=None).count()
    today = Document.objects.filter(fetched_at__gte=now - dt.timedelta(days=1)).count()

    return render(request, "web/pipeline.html", {
        "nav": "pipeline", "rows": rows,
        "total": total, "relevant": relevant,
        "with_body": with_body, "vectorised": vectorised,
        "fetched_today": today,
        "relevant_pct": relevant / total * 100 if total else 0,
        "body_pct": with_body / total * 100 if total else 0,
        "vector_pct": vectorised / relevant * 100 if relevant else 0,
    })


#: 需要人工登入才能下載的外部平台。key 是 slug。
#:
#: 只登記在這裡，不寫進 Source——ExternalSession 儲存的是「登入狀態」，
#: 概念上與「新聞來源的輪詢設定」不同，硬塞進同一個模型會讓
#: Source 同時承擔兩種不相關的職責。
EXTERNAL_SESSIONS = {
    "judicial-opendata": (
        "司法院資料開放平台",
        "https://opendata.judicial.gov.tw/member/login",
    ),
}


@login_required
def crawlers(request):
    """爬蟲排程設定與手動觸發。

    與 ``pipeline`` 的分工：pipeline 是**觀察**（健康度、覆蓋率），
    這裡是**操作**（改排程、立即跑一次）。兩者都需要時硬湊在同一頁
    會讓「看數字」與「按按鈕」互相干擾，拆開比較不會誤觸。
    """
    now = timezone.now()
    rows = []
    for source in Source.objects.order_by("-enabled", "name"):
        rows.append({
            "source": source,
            "effective_interval": effective_interval_minutes(source),
            "due_now": should_poll(source) if source.enabled else False,
            "unhealthy": source.consecutive_failures >= FAILURE_THRESHOLD,
        })

    sessions = []
    for slug, (name, login_url) in EXTERNAL_SESSIONS.items():
        session, _ = ExternalSession.objects.get_or_create(
            slug=slug, defaults={"name": name, "login_url": login_url})
        sessions.append(session)

    return render(request, "web/crawlers.html", {
        "nav": "crawlers", "rows": rows, "now": now, "sessions": sessions,
    })


@login_required
@require_POST
def save_external_session(request, slug):
    """儲存人工登入後取得的 session cookie。

    **為什麼需要人工這一步。** 這類平台的登入頁掛了 Cloudflare
    Turnstile，自動化瀏覽器過不了——不是技術難度問題，是 Turnstile
    刻意要擋。真人在彈出視窗裡完成登入後，把 Cookie 標頭字串貼回來，
    系統重放這個 session，不必也不會嘗試自動通過 Turnstile。
    """
    session = get_object_or_404(ExternalSession, slug=slug)
    cookie_header = request.POST.get("cookie_header", "").strip()
    if not cookie_header:
        messages.error(request, "請貼上登入後的 Cookie 字串")
        return redirect("web:crawlers")

    session.cookie_header = cookie_header
    session.captured_at = timezone.now()
    session.save(update_fields=["cookie_header", "captured_at", "updated_at"])
    messages.success(request, f"已儲存「{session.name}」的登入 session")
    return redirect("web:crawlers")


@login_required
@require_POST
def crawler_run(request, slug):
    """手動觸發單一來源立即爬取一次。

    非同步派工（Celery），不同步等待完成——單次爬取（尤其
    Playwright 站台）可達數分鐘，同步等待會讓請求逾時，且使用者
    看不出頁面是卡住還是真的在跑。派工後導回列表頁，結果稍後在
    「最後成功時間」「連續失敗次數」自然反映出來。
    """
    source = get_object_or_404(Source, slug=slug)
    dispatch_poll(source)
    messages.success(request, f"已派發「{source.name}」的爬取任務，稍後重新整理查看結果")
    return redirect("web:crawlers")


@login_required
@require_POST
def crawler_update(request, slug):
    """更新單一來源的排程參數：多久爬一次、可用時段。"""
    source = get_object_or_404(Source, slug=slug)

    try:
        interval = int(request.POST.get("poll_interval_minutes", ""))
        if interval < 1:
            raise ValueError
    except ValueError:
        messages.error(request, "爬取間隔須為正整數（分鐘）")
        return redirect("web:crawlers")

    source.poll_interval_minutes = interval
    source.enabled = request.POST.get("enabled") == "on"

    start = request.POST.get("service_window_start_hour", "").strip()
    end = request.POST.get("service_window_end_hour", "").strip()
    if start and end:
        try:
            start_hour, end_hour = int(start), int(end)
            if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
                raise ValueError
        except ValueError:
            messages.error(request, "可用時段須為 0–23 的整數小時")
            return redirect("web:crawlers")
        source.service_window_start_hour = start_hour
        source.service_window_end_hour = end_hour
    else:
        source.service_window_start_hour = None
        source.service_window_end_hour = None

    source.save(update_fields=[
        "poll_interval_minutes", "enabled",
        "service_window_start_hour", "service_window_end_hour", "updated_at",
    ])
    messages.success(request, f"已更新「{source.name}」的排程設定")
    return redirect("web:crawlers")


@login_required
def costs(request):
    """LLM 用量與成本。

    三個要回答的問題（見 apps/llm/admin.py）：錢花在哪一層、
    context caching 有沒有作用、有多少花在尖峰。
    """
    usage = LlmUsage.objects.all()
    totals = usage.aggregate(
        calls=Count("id"), usd=Sum("cost_usd"), ntd=Sum("cost_ntd"),
        tokens=Sum("total_tokens"),
        cached=Sum("cached_input_tokens"), uncached=Sum("uncached_input_tokens"),
        failures=Count("id", filter=Q(succeeded=False)),
    )
    cached = totals["cached"] or 0
    uncached = totals["uncached"] or 0
    labels = dict(LlmPurpose.choices)

    return render(request, "web/costs.html", {
        "nav": "costs", "totals": totals,
        "cache_rate": cached / (cached + uncached) * 100 if cached + uncached else 0,
        "by_purpose": [
            {**row, "label": labels.get(row["purpose"], row["purpose"])}
            for row in usage.values("purpose").annotate(
                calls=Count("id"), usd=Sum("cost_usd"), ntd=Sum("cost_ntd"),
            ).order_by("-usd")
        ],
        "by_peak": [
            {**row, "label": "尖峰" if row["peak"] else "離峰（半價）"}
            for row in usage.values("peak").annotate(
                calls=Count("id"), usd=Sum("cost_usd")).order_by("peak")
        ],
        "spent": spent_usd(), "approved": approved_usd(), "remaining": remaining_usd(),
        "recent": usage.select_related("document").order_by("-created_at")[:25],
    })
