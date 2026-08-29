"""產生待標註的候選清單（任務 20）。

**標註集不能用受測的 LLM 來標**——那是循環論證。因此本指令只負責
挑出候選並依「確定程度」排序，實際判定仍需人工。

排序的用意是壓縮人工成本：標題直接含事件關鍵字者幾乎必然屬於該事件，
只在內文提及者才需要細看。人只需專注在中間那段模糊的部分。

輸出為 TSV，可直接在編輯器或試算表修改 label 欄後匯入。
"""
import csv
import sys

from django.core.management.base import BaseCommand

from apps.ingest.models import Document
from apps.retrieval.keyword import BigramFtsBackend

#: (slug, 顯示名稱, 主要關鍵字, 必須同時出現的佐證詞)
#:
#: **單一關鍵字不足以界定事件。** 初版以「大巨蛋」「南方澳」「光電」
#: 為關鍵字，結果抓到的是：
#:
#:   大巨蛋 → 中職賽事、明星賽衝突、球星引退（場館，不是弊案）
#:   南方澳 → 珊瑚媽祖竊案、漁船失蹤（地名，不是斷橋事件）
#:   光電   → 中科園區慶典、畜牧場火災（產業，不是特定弊案）
#:
#: 場館、地名、產業名都會把「該場所／該產業的所有新聞」一併帶進來。
#: 事件需要更具體的識別依據——當事人、案由或案號——因此改為
#: 「主要關鍵字 + 佐證詞」的組合。
#:
#: 這個發現對 ADR-0005 的事件偵測有直接意義：若僅憑語意叢集，
#: 同一場館的所有報導會被歸為一個事件。
EVENTS = [
    ("hsinchu-stadium", "新竹棒球場案", "新竹棒球場", ["林智堅", "起訴", "偵", "檢"]),
    ("core-pacific", "京華城容積案", "京華城", ["柯文哲", "沈慶京", "容積", "起訴"]),
    ("taipei-dome", "大巨蛋案", "大巨蛋", ["遠雄", "趙藤雄", "圖利", "弊", "檢"]),
    ("nanfangao", "南方澳大橋斷橋", "南方澳", ["斷橋", "大橋", "運安會", "坍"]),
    ("chaosi-eggs", "超思進口蛋案", "超思", ["進口蛋", "農業部", "陳吉仲", "蛋"]),
    ("chengxin-solar", "誠新綠能案", "誠新綠能", ["挪用", "背信", "侵占", "羈押"]),
]

PER_EVENT = 30


class Command(BaseCommand):
    help = "產生待人工標註的候選清單（TSV）"

    def add_arguments(self, parser):
        parser.add_argument("--out", default="fixtures/label_candidates.tsv")
        parser.add_argument("--per-event", type=int, default=PER_EVENT)

    def handle(self, *args, **options):
        backend = BigramFtsBackend()
        base = (Document.objects.relevant()
                .filter(published_at__isnull=False)
                .select_related("source"))

        rows = []
        for slug, name, keyword, corroborators in EVENTS:
            candidates = list(backend.search(base, keyword)
                              .order_by("-published_at")[:options["per_event"] * 4])

            scored = []
            for doc in candidates:
                text = f"{doc.title}\n{doc.raw_body}"
                in_title = keyword in doc.title
                mentions = doc.raw_body.count(keyword)
                # 佐證詞用於區分「事件」與「同名的場所／產業」——
                # 缺少佐證詞的多半只是該場館或該地區的日常新聞
                corroborated = sum(1 for c in corroborators if c in text)

                if in_title and corroborated:
                    confidence = "high"
                elif corroborated >= 2 and mentions >= 2:
                    confidence = "medium"
                elif in_title or (mentions >= 3 and corroborated):
                    confidence = "medium"
                else:
                    confidence = "low"
                scored.append((confidence, mentions, doc))

            order = {"high": 0, "medium": 1, "low": 2}
            scored.sort(key=lambda r: (order[r[0]], -r[1]))

            for confidence, mentions, doc in scored[:options["per_event"]]:
                snippet = doc.raw_body[:70].replace("\n", " ").replace("\t", " ")
                rows.append({
                    "event_slug": slug,
                    "event_name": name,
                    "confidence": confidence,
                    "mentions": mentions,
                    # 待填：1=屬於此事件，0=僅提及或無關。預設留空以強制人工判定。
                    "label": "",
                    "published": doc.published_at.strftime("%Y-%m-%d"),
                    "source": doc.source.slug,
                    "title": doc.title.replace("\t", " "),
                    "snippet": snippet,
                    "doc_id": doc.pk,
                    "url": doc.url,
                })

        path = options["out"]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)

        by_conf = {}
        for r in rows:
            by_conf[r["confidence"]] = by_conf.get(r["confidence"], 0) + 1
        self.stdout.write(self.style.SUCCESS(f"已產出 {len(rows)} 筆 → {path}"))
        for c in ("high", "medium", "low"):
            self.stdout.write(f"  {c:<8}{by_conf.get(c, 0):>5} 筆")
