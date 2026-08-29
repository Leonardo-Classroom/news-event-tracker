"""從標註集建立事件與歸屬（任務 22）。

以人工標註的結果建出真實事件，作為兩件事的基礎：

1. **端到端驗證資料模型可用**——先走人工路徑，確認 Event、
   EventDocument、狀態機在真實資料上運作正常，再做自動歸屬。
2. **自動歸屬的對照基準**——任務 23 的誤歸屬率（規格 M4）需要
   ground truth 才能量測。

事件的 ``core_terms`` 由其文件的標題統計而來，而非人工指定。
標註集實測顯示單一關鍵字不足以界定事件（「大巨蛋」會帶進場館的
所有體育新聞），因此改以文件集合的高頻專名作為事件的識別特徵。
"""
import collections
import csv
import re

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.events.models import (
    AssignmentMethod, Event, EventAlias, EventDocument, EventStatus, EventDomain,
)
from apps.ingest.models import Document

DEFAULT_PATH = "fixtures/label_candidates.tsv"

#: 事件所屬領域。弊案類事件的實體集合含標案與公司，與純司法案件不同。
DOMAINS = {
    "hsinchu-stadium": EventDomain.CORRUPTION,
    "core-pacific": EventDomain.CORRUPTION,
    "taipei-dome": EventDomain.CORRUPTION,
    "nanfangao": EventDomain.JUDICIAL,
    "chaosi-eggs": EventDomain.CORRUPTION,
    "chengxin-solar": EventDomain.CORRUPTION,
}

#: 過於通用、不具識別力的詞，不納入 core_terms
_STOP_TERMS = {
    "表示", "指出", "認為", "強調", "回應", "說明", "今天", "昨天", "記者",
    "報導", "新聞", "議員", "市長", "市府", "政府", "民眾", "台灣", "臺灣",
    "相關", "問題", "可能", "已經", "沒有", "這個", "我們", "他們",
}


class Command(BaseCommand):
    help = "從標註集建立事件與人工歸屬"

    def add_arguments(self, parser):
        parser.add_argument("--path", default=DEFAULT_PATH)
        parser.add_argument("--reset", action="store_true",
                            help="先刪除既有的匯入事件")

    def handle(self, *args, **options):
        rows = list(csv.DictReader(open(options["path"], encoding="utf-8"),
                                   delimiter="\t"))
        by_event = collections.defaultdict(list)
        for row in rows:
            if row["label"] == "1":
                by_event[(row["event_slug"], row["event_name"])].append(int(row["doc_id"]))

        if options["reset"]:
            slugs = [slug for slug, _ in by_event]
            deleted, _ = Event.objects.filter(slug__in=slugs).delete()
            self.stdout.write(f"已刪除既有事件相關記錄 {deleted} 筆")

        self.stdout.write(f"{'事件':<16}{'文件':>6}{'core_terms':>12}{'最後進展':>13}")
        self.stdout.write("-" * 50)

        for (slug, name), doc_ids in sorted(by_event.items()):
            with transaction.atomic():
                event = self._build(slug, name, doc_ids)
            self.stdout.write(
                f"{name:<16}{event.event_documents.count():>6}"
                f"{len(event.core_terms):>12}"
                f"{event.last_progress_at.strftime('%Y-%m-%d') if event.last_progress_at else '—':>13}"
            )

        total = Event.objects.filter(slug__in=[s for s, _ in by_event]).count()
        self.stdout.write(self.style.SUCCESS(
            f"\n完成：{total} 個事件、"
            f"{EventDocument.objects.filter(event__slug__in=[s for s, _ in by_event]).count()} 筆歸屬"))

    # ---------------------------------------------------------------- 內部

    def _build(self, slug: str, name: str, doc_ids: list[int]) -> Event:
        documents = list(Document.objects.filter(id__in=doc_ids))

        event, _ = Event.objects.update_or_create(
            slug=slug,
            defaults={
                "title": name,
                "domain": DOMAINS.get(slug, EventDomain.GENERIC),
                # 人工建立且已審核，直接進 active——這批是標註集，
                # 已經過人工判讀，不需再走 candidate → draft 的審核流程
                "status": EventStatus.ACTIVE,
                "core_terms": self._core_terms(documents),
                "case_numbers": self._case_numbers(documents),
            },
        )

        EventAlias.objects.get_or_create(event=event, name=name)

        for doc in documents:
            EventDocument.objects.update_or_create(
                event=event, document=doc,
                defaults={
                    "method": AssignmentMethod.IMPORT,
                    "relevance_score": 1.0,
                    "reason": "人工標註（fixtures/LABELING_GUIDE.md）",
                },
            )

        # 進展時間取自文件而非匯入時間——事件的時間軸屬於它的內容，
        # 不屬於系統的操作紀錄
        dates = [d.published_at for d in documents if d.published_at]
        if dates:
            event.last_progress_at = max(dates)
            event.first_seen_at = min(dates)
            event.save(update_fields=["last_progress_at", "first_seen_at"])
        return event

    @staticmethod
    def _core_terms(documents, top: int = 12) -> list[str]:
        """由 L1 抽取的實體組成事件的識別特徵。

        **不自行從中文標題切詞。** 曾嘗試三種方式，全部失敗：

        1. **固定長度 n-gram 取高頻**——選到「起訴」「檢方」這類
           全語料同樣常見的詞，毫無鑑別力
        2. **加上 TF-IDF 式的稀有性對比**——修掉了通用詞，卻反而
           **獎勵碎片**：「案林智堅不」「柯文哲京華」這類跨詞邊界的
           片段在語料中極度罕見，因此拿到最高分
        3. **以子字串包含關係剔除碎片**——仍殘留「周廢」「命償」「堅不」

        根本問題是中文沒有詞界，而這等於在重新發明斷詞。
        （jieba 亦不可行：預設詞典為簡體導向，「柯文哲京華城」被切為
        「柯文／哲京／華城」，「誠新綠能」無法辨識；加自訂詞典則是
        雞生蛋問題。）

        **L1 抽取已經解決了這件事**——LLM 理解詞界，直接輸出
        「柯文哲」「沈慶京」等完整專名。直接採用其結果。

        另需說明：core_terms 是次要欄位。ADR-0005 的歸屬主力是
        向量檢索，沒有 core_terms 也能運作；它的用途是讓關鍵字路徑
        與人工檢視更方便。
        """
        from apps.extract.models import PROMPT_VERSION, Extraction

        extractions = Extraction.objects.filter(
            document__in=documents, succeeded=True, prompt_version=PROMPT_VERSION,
        ).values_list("payload", flat=True)

        counter = collections.Counter()
        for payload in extractions:
            for key in ("defendants", "persons", "companies"):
                for item in payload.get(key) or []:
                    name = item.get("name") if isinstance(item, dict) else item
                    if isinstance(name, str) and 2 <= len(name) <= 12:
                        counter[name.strip()] += 1
            for key in ("agencies", "organizations", "tender_names"):
                for item in payload.get(key) or []:
                    if isinstance(item, str) and 2 <= len(item) <= 16:
                        counter[item.strip()] += 1

        # 出現在 ≥2 篇者才採信——單篇提及的多半是背景人物
        return [name for name, count in counter.most_common(top * 2)
                if count >= 2][:top]

    @staticmethod
    def _case_numbers(documents) -> list[str]:
        numbers: dict[str, None] = {}
        for doc in documents:
            for number in doc.case_numbers or []:
                numbers.setdefault(number, None)
        return list(numbers)
