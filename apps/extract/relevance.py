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
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.core.identifiers import extract_tax_ids, parse_case_number

__all__ = ["RELEVANCE_TERMS", "RelevanceResult", "assess_relevance"]

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
        "北檢", "新北檢", "士檢", "橋檢", "中檢", "南檢", "雄檢", "高檢",
        "檢方", "檢調",
        "地方法院", "高等法院", "最高法院", "智慧財產法院", "懲戒法院",
        "法院", "法官",
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
        if hits:
            signals[category] = hits

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
