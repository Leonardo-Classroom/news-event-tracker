"""相關性過濾（ADR-0009 第 4、5 點）。

在 L1 抽取之前先以**純規則**篩掉與司法案件、政商弊案無關的文件。
兩個目的，缺一不可：

1. **省錢**——L1 抽取是量體最大的 LLM 用途，無關文件不該送
2. **保護事件偵測**——這才是主要理由。無關文件若進入 ADR-0005 的叢集，
   同類報導（體育、娛樂、股市）彼此語意相似度很高，足以形成密集簇並
   通過「≥3 家媒體 ≥5 篇跨 ≥2 天」的門檻，產生大量無意義的事件候選，
   直接淹沒本已吃緊的人工審核量能

**規則刻意偏向召回。** 漏掉一篇相關文件的代價是「某個事件的時間線缺一塊」，
而且**不會報錯**——那是靜默失效。多送一篇無關文件的代價只是幾分錢。
因此判定為 OR 邏輯：命中任一詞彙即通過。

詞彙表以實測校準，非憑直覺。在 238,324 篇語料上各詞的命中率：

    法院 10.29%   地檢署 3.60%   檢方 3.23%   判決 2.77%   起訴 2.74%
    偵查 1.43%   被告 1.34%   判刑 1.24%   有期徒刑 1.13%
    圖利 0.44%   貪污 0.08%   ← 直覺會高估「貪污」，實際遠低於「圖利」
    對照：表示 65.42%   颱風 9.44%   股價 4.40%

**憑直覺列詞會漏掉整組用語。** 初版遺漏了「檢方」（比「檢察官」更常用）、
「判刑」「有期徒刑」等判決結果用語、以及「北檢」「新北檢」等簡稱
（台灣新聞的主流寫法，合計約 3,500 篇）。這些都是靠實測補回來的。

**中文沒有詞界，子字串比對會被更長的詞誤觸發。** 這不是理論顧慮：
實測「法院」的命中有 47.9% 純粹來自「立法院」，導致「星鏈條款闖關」
「中油不買天然氣」等完全無關的政治新聞被判為司法案件。
有歧義的詞改以 ``RELEVANCE_PATTERNS`` 的正則處理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.core.identifiers import extract_tax_ids, parse_case_number

__all__ = ["RELEVANCE_TERMS", "RelevanceResult", "assess_relevance",
           "is_corroborated", "STRONG_CATEGORIES"]

#: 領域詞彙，依類別分組。類別會記入 signals，供日後調校時判斷
#: 「是哪一類規則帶進來的」——只記布林值就無從調整。
RELEVANCE_TERMS: dict[str, tuple[str, ...]] = {
    "judicial_process": (
        "起訴", "不起訴", "緩起訴", "偵結", "偵辦", "偵查終結", "偵查",
        "判決", "宣判", "定讞", "上訴", "抗告", "更審", "再審",
        "羈押", "交保", "具保", "限制出境", "限制出海",
        "傳喚", "約談", "搜索", "扣押", "查扣", "聲請簡易判決",
        "開庭", "辯論終結", "言詞辯論", "起訴書", "判決書",
        # 判決結果用語。實測「判刑」1.24%、「有期徒刑」1.13%——
        # 憑直覺列規則時整組漏掉，而這正是事件時間線最關鍵的節點。
        "判刑", "有期徒刑", "無期徒刑", "緩刑", "量刑", "科刑",
        "無罪", "有罪", "免訴", "撤銷原判決",
        "被告", "犯嫌", "涉犯", "罪嫌",
    ),
    "judicial_body": (
        "地檢署", "高檢署", "最高檢", "檢察署",
        "檢察官", "檢察總長", "主任檢察官",
        # 簡稱是台灣新聞的主流寫法，實測合計約 3,500 篇。
        # 只列全稱會系統性漏掉大量報導。
        "北檢", "新北檢", "士檢", "橋檢", "中檢", "南檢", "雄檢",
        "檢方", "檢調",
        "地方法院", "高等法院", "最高法院", "智慧財產法院", "懲戒法院",
        "司法院", "法官",
        "調查局", "廉政署", "政風", "刑事局",
    ),
    "corruption_charge": (
        "貪污", "貪瀆", "圖利", "背信", "瀆職", "收賄", "行賄", "受賄",
        "洗錢", "偽造文書", "內線交易", "掏空", "侵占", "詐領",
        "違背職務", "不違背職務", "貪污治罪條例", "證券交易法",
    ),
    "corruption_context": (
        "弊案", "弊端", "回扣", "綁標", "圍標", "關說", "利益輸送",
        "不法所得", "犯罪所得", "公款", "採購案", "標案", "工程款",
    ),
    "oversight": (
        "彈劾", "糾正案", "糾舉", "監察院", "監委", "審計部",
    ),
}

#: 有歧義的詞，需以正則排除被更長詞誤觸發的情況。
#:
#: 中文沒有詞界，子字串比對會被包含該詞的更長詞誤觸發。實測（3 萬篇抽樣）
#: 顯示這是嚴重問題，不是理論顧慮：
#:
#:     法院  命中 3,380，其中 1,620 篇（**47.9%**）僅來自「立法院」
#:     高檢  命中   107，其中    18 篇（16.8%）僅來自「提高檢驗」等
#:
#: 未處理前，「立法院」相關的政治新聞會大量被誤判為司法案件——
#: 抽樣中出現「星鏈條款闖關」「中油不買天然氣」「金石堂分店熄燈」等
#: 完全無關的文件。
#:
#: 「司法院」不排除：它本身就是司法脈絡。僅排除「立法院」。
RELEVANCE_PATTERNS: dict[str, dict[str, re.Pattern]] = {
    "judicial_body": {
        "法院": re.compile(r"(?<!立)法院"),
        "高檢": re.compile(r"(?<![提拉升增])高檢"),
    },
}

#: 命中即通過。刻意不設「需命中 N 個」的門檻——那會系統性地漏掉
#: 短篇快訊（如「某某遭起訴」），而快訊往往是事件的第一則報導。
MIN_HITS = 1


@dataclass
class RelevanceResult:
    relevant: bool
    #: 命中的詞彙，依類別分組。用於日後調校規則。
    signals: dict[str, list[str]] = field(default_factory=dict)
    #: 是否含案號或統一編號。這類識別碼是強訊號，
    #: 且正是 ADR-0001 事件歸屬的主要依據。
    has_identifier: bool = False

    @property
    def hit_count(self) -> int:
        return sum(len(v) for v in self.signals.values())

    @property
    def categories(self) -> list[str]:
        return sorted(self.signals)


#: 單獨出現就足以判定「這篇是在講具體的司法案件或弊案」的類別。
#: 不含 judicial_body——這個類別只需命中「法官」「檢方」等機構稱謂，
#: 而「大法官」這種與具體案件無關的政治新聞（大法官人事任命爭議、
#: 憲法法庭運作與否）也會觸發它，因為子字串比對「法官」對「大法官」
#: 一樣命中。
STRONG_CATEGORIES = frozenset({"corruption_charge", "corruption_context", "oversight"})


def is_corroborated(signals: dict) -> bool:
    """該文件的相關性訊號是否足夠具體，值得作為**事件偵測**（而非只是
    「該不該做 L1 抽取」）的候選。

    **這是任務 13.5 相關性過濾與任務 24 事件偵測之間，一個原本沒被
    注意到的落差。** 相關性過濾刻意偏向召回（ADR-0009：漏掉一篇的
    代價遠高於多送一篇），單一類別命中就通過。這個設計對「該不該做
    L1 抽取」是對的——抽取的代價只是幾分錢。但 HDBSCAN 事件偵測把
    這個寬鬆的候選池直接拿來分群，而分群只看語意相似度：「大法官
    人事任命」「軍公教年金訴訟」這類長期政治爭議報導彼此高度相似，
    會被分成看起來完整的一群，但它們是政治新聞，不是規格範圍內的
    司法案件或政商弊案。

    實測：對成長中的真實語料跑 ``detect_events``，343 個候選事件中
    245 個（71%）的文件標題完全不含任何司法弊案核心詞——「憲法法庭
    停擺」「行政院拒編預算」「軍警調薪」等政治爭議新聞，都是透過
    ``judicial_body``（「法官」命中了「大法官」）這個類別單獨通過
    相關性過濾，才進入了叢集候選池。

    判準：命中弊案專屬類別（``corruption_charge``／``corruption_context``／
    ``oversight``）即足夠——這些詞彙本身就具體指向弊案（「圖利」
    「回扣」「糾正案」不會出現在無關的政治新聞裡）；或有案號／統編
    這類強識別碼；否則需要至少兩個類別同時命中（互相佐證），
    與 ``fixtures/LABELING_GUIDE.md`` 「主要關鍵字 + 佐證詞」是
    同一個道理——單一類別的命中不足以排除「同名但無關」的可能。
    """
    categories = set(signals.get("categories", []))
    if categories & STRONG_CATEGORIES:
        return True
    if signals.get("has_identifier"):
        return True
    return len(categories) >= 2


def assess_relevance(title: str = "", body: str = "") -> RelevanceResult:
    """判定文件是否與司法案件或政商弊案相關。

    純函式、不呼叫 LLM、不查資料庫——因此可以在 238k 篇語料上批次執行，
    也可以用 small 測試完整涵蓋邊界。
    """
    text = f"{title}\n{body}"
    if not text.strip():
        return RelevanceResult(relevant=False)

    signals: dict[str, list[str]] = {}
    for category, terms in RELEVANCE_TERMS.items():
        hits = [term for term in terms if term in text]
        for label, pattern in RELEVANCE_PATTERNS.get(category, {}).items():
            if pattern.search(text):
                hits.append(label)
        if hits:
            signals[category] = sorted(set(hits))

    # 案號是強訊號。裁判書、起訴書的報導幾乎必然帶案號，
    # 而它同時也是事件歸屬的主要依據（ADR-0001）。
    has_identifier = parse_case_number(text) is not None

    # 統編單獨出現不足以判定相關（企業新聞也有），
    # 但與弊案語彙同時出現時值得記下來供調校參考。
    if not has_identifier and signals.get("corruption_context"):
        has_identifier = bool(extract_tax_ids(text))

    relevant = sum(len(v) for v in signals.values()) >= MIN_HITS or (
        parse_case_number(text) is not None
    )

    return RelevanceResult(
        relevant=relevant,
        signals=signals,
        has_identifier=has_identifier,
    )
