"""措辭檢查器（任務 38 設計、任務 39 實作，規格 G6、M6，ADR：blocking gate）。

**設計先於實作**：docs/02-任務拆解.md 原本把這一區塊列為「尚未成形」——
違規句型分類、規則覆蓋範圍、小模型角色皆未定。以下是實測後定案的設計。

## 核心判準

規格 G6：「所有涉及尚未判決確定之個人的敘述，措辭符合無罪推定原則」。
關鍵字比對無法單靠文字本身判斷違規——同一個詞在不同案件狀態下，
一個是陳述事實，一個是違法定性：

    「台灣藍海董事長判10年半定讞」   ← 真的定讞，陳述事實
    「柯文哲涉嫌收賄定讞」           ← 案件尚未定讞，若這樣寫就是違規

因此檢查器**不是純文字分類器**，而是要求呼叫端一併提供這段文字所本的
案件是否已判決確定（來自 `Extraction.payload["is_final"]`，任務 14 已
定義此欄位）。同一個詞，`is_final=True` 放行、`is_final=False` 攔截。

**保守方向**：一段文字若同時涉及多個被告，只要其中一人未定讳，
整段視為 `is_final=False`（寧可誤擋、不可誤放）。

## 三類用語，實測自本語料真實標題（見任務 38 設計紀錄）

1. **程序用語（永遠安全，不受 is_final 影響）**：涉嫌、被控、被訴、
   遭訴、遭起訴、經起訴、羈押、交保、限制出境、開庭、應訊、傳喚、
   約談、一審判決、二審判決、上訴、更審、裁定、認罪、複訊、諭知、
   依法偵辦、通緝。

   實測發現「認罪」原本被誤判為危險詞——但它是被告在法庭上的正式
   程序行為（認罪答辯），是陳述事實，不是我方對其定罪的斷定，
   應與「起訴」「羈押」同屬程序用語。

2. **無條件違規（`is_final=False` 時一律攔截，不論上下文）**：定讞、
   判刑確定、伏法、身陷囹圄、入獄服刑、已入獄、罪證確鑿、詐欺犯、
   貪污犯、圖利犯、詐騙犯、鋃鐺入獄。

   這些詞本身即斷言「案件已終結且此人有罪」，實測本語料的真實用法
   （「詐欺犯再判8月」「陸版絕命毒師伏法」）也都用於已定讞或已執行
   的案件——用它們描述未定讞案件，等同捏造案件狀態。

3. **條件式用語（需搭配法院動作標記才安全）**：有罪、服刑、定罪。

   「法院一審判決A有罪，處有期徒刑8年」是陳述法院做了什麼，即使
   案件未定讞也是真實敘述；但「A有罪」若脫離法院動作的歸屬，就是
   我方逕自斷定。因此這三詞需要鄰近（前後 15 字內）出現法院動作標記
   （法院／庭／判決／諭知／裁定／宣判／求刑／一審／二審／三審／更審／
   量刑／判處）才視為安全。

## 已知缺口（未涵蓋，留待「規則 + 小模型」的小模型部分）

- **無明示定罪動詞的斷定敘事**：如「沈慶京為償賭債，京華城土地套利
  32億」——沒有任何違規關鍵字，但整句以肯定語氣敘述被告的犯罪行為
  而非「檢方指控」。規則比對無法辨識這類隱性斷定。
- **引述被告自身陳述** vs **我方敘事**：「曾志新：『犯下最大錯誤』」
  是引述被告的話，本身不違規；但同樣的字「犯下」若是我方旁白斷定
  （「曾志新犯下侵占案」）就違規。規則無法區分引號內外。

這兩類缺口正是規格所稱「規則 + 小模型」中，小模型該扮演的角色——
規則攔截明顯違規，小模型評估規則測不到的隱性斷定與敘事語氣。
本次先只做規則，小模型部分留待有更多違規範例可訓練／少樣本提示時再做。

## 為何是 blocking gate、不提供繞過

規格明訂「人工審核僅做形式確認」（R8）——若措辭檢查器可被人工繞過，
就等於沒有這道防線。攔截後的唯一路徑是**修改文字重新送檢**，
不是「审核者按下強制通過」。
"""
from __future__ import annotations

import dataclasses
import re

__all__ = [
    "WordingViolation", "WordingCheckResult", "check_wording",
    "PROCEDURAL_SAFE_TERMS", "UNCONDITIONAL_VIOLATION_TERMS", "CONDITIONAL_TERMS",
    "COURT_ACTION_MARKERS",
]

#: 程序用語。列在此處純為文件與測試之用（供標註測試集交叉核對），
#: 不參與比對邏輯——安全詞不需要被「排除」，本來就不在違規清單裡。
PROCEDURAL_SAFE_TERMS = (
    "涉嫌", "被控", "被訴", "遭訴", "遭起訴", "經起訴",
    "羈押", "交保", "限制出境", "開庭", "應訊", "傳喚", "約談",
    "一審判決", "二審判決", "上訴", "更審", "裁定", "認罪", "複訊",
    "諭知", "依法偵辦", "通緝",
)

#: 無條件違規：本身即斷言「案件已終結且此人有罪」。
UNCONDITIONAL_VIOLATION_TERMS = (
    "定讞", "判刑確定", "伏法", "身陷囹圄", "入獄服刑", "已入獄",
    "罪證確鑿", "詐欺犯", "貪污犯", "圖利犯", "詐騙犯", "鋃鐺入獄",
)

#: 條件式：需鄰近法院動作標記才安全。
CONDITIONAL_TERMS = ("有罪", "服刑", "定罪")

#: 法院動作標記——出現在條件式用語附近，代表這是在陳述法院的動作
#: 而非我方逕自斷定。
COURT_ACTION_MARKERS = (
    "法院", "地院", "高院", "最高法院", "庭", "判決", "諭知", "裁定",
    "宣判", "求刑", "一審", "二審", "三審", "更審", "量刑", "判處",
)

#: 條件式用語的鄰近視窗（字元數）。定得比 bigram 檢索的視窗寬，
#: 因為法院動作標記常與詞語隔了主詞或副詞（「法院依法判決其有罪」）。
_WINDOW = 15


@dataclasses.dataclass(frozen=True)
class WordingViolation:
    term: str
    position: int
    context: str
    reason: str


@dataclasses.dataclass(frozen=True)
class WordingCheckResult:
    passed: bool
    violations: tuple[WordingViolation, ...] = ()

    def __bool__(self) -> bool:
        return self.passed


def _context(text: str, position: int, term: str) -> str:
    start = max(0, position - 10)
    end = min(len(text), position + len(term) + 10)
    return text[start:end]


def check_wording(text: str, *, is_final: bool = False) -> WordingCheckResult:
    """檢查文字是否違反無罪推定原則。

    Args:
        is_final: 這段文字所涉案件是否已判決確定。**預設 False**——
            不知道就當作未確定，這是保守方向該有的預設值。若一段文字
            涉及多名被告且判決狀態不一，呼叫端應傳入
            ``all(defendant_is_final)``，任一未確定就整段視為 False。
    """
    if is_final:
        return WordingCheckResult(passed=True)

    violations: list[WordingViolation] = []

    for term in UNCONDITIONAL_VIOLATION_TERMS:
        for match in re.finditer(re.escape(term), text):
            violations.append(WordingViolation(
                term=term, position=match.start(),
                context=_context(text, match.start(), term),
                reason=f"「{term}」斷言案件已終結且此人有罪，"
                       f"但本案尚未判決確定",
            ))

    for term in CONDITIONAL_TERMS:
        for match in re.finditer(re.escape(term), text):
            window_start = max(0, match.start() - _WINDOW)
            window_end = min(len(text), match.end() + _WINDOW)
            window = text[window_start:window_end]
            if not any(marker in window for marker in COURT_ACTION_MARKERS):
                violations.append(WordingViolation(
                    term=term, position=match.start(),
                    context=_context(text, match.start(), term),
                    reason=f"「{term}」附近未見法院動作標記"
                           f"（如法院／判決／裁定），視為我方逕自斷定",
                ))

    return WordingCheckResult(passed=not violations, violations=tuple(violations))
