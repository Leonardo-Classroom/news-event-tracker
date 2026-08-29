"""識別碼的抽取、正規化與驗證。

ADR-0001 的支點：司法與弊案領域的關鍵檢索標的是**精確識別碼**，
不是自然語言關鍵字。裁判書案號、統一編號、標案編號一旦正規化，
就能用資料庫的精確比對命中，準確率 100%——遠優於任何中文斷詞方案。

事件歸屬時，識別碼比對優先於向量與全文檢索（ADR-0005 階段一步驟 3）。
因此本模組的正確性直接決定「裁判書能否歸入正確事件」，
也就是整個系統的核心價值能否成立。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .dates import to_halfwidth

__all__ = [
    "CaseNumber",
    "parse_case_number",
    "extract_case_numbers",
    "normalize_case_number",
    "is_valid_tax_id",
    "extract_tax_ids",
    "normalize_tender_id",
]


# ---------------------------------------------------------------- 裁判書案號

@dataclass(frozen=True)
class CaseNumber:
    """裁判書案號的結構化表示。

    例：「111年度金重訴字第123號」→ CaseNumber(111, "金重訴", 123)

    以民國年儲存（不轉西元），因為案號本身就是民國紀年的識別碼，
    轉換會失去與原始文件對照的能力。
    """

    year: int      # 民國年
    category: str  # 字別，如「金重訴」「台上」「易」
    number: int    # 號次

    def canonical(self) -> str:
        """正規形式，用於資料庫儲存與精確比對。"""
        return f"{self.year}年度{self.category}字第{self.number}號"

    def __str__(self) -> str:
        return self.canonical()


# 「臺」與「台」在官方文件中混用，正規化時統一為「台」
_CATEGORY_NORMALIZE = str.maketrans({"臺": "台"})

# 年度可省略「度」字；各段之間可能有空白
_CASE_PATTERN = re.compile(
    r"(?P<year>\d{2,3})\s*年\s*度?\s*"
    r"(?P<category>[一-鿿]{1,8}?)\s*"
    r"字\s*第?\s*"
    r"(?P<number>\d{1,7})\s*號"
)


def parse_case_number(text: str | None) -> CaseNumber | None:
    """從文字中抽出第一個案號。找不到時回傳 ``None``。

    容忍的變體：全形數字、各段間的空白、省略「度」字、「臺」與「台」混用。

    >>> parse_case_number("臺灣高等法院 111 年度 金重訴 字第 123 號判決")
    CaseNumber(year=111, category='金重訴', number=123)
    """
    if not text:
        return None

    match = _CASE_PATTERN.search(to_halfwidth(str(text)))
    if not match:
        return None

    category = match.group("category").translate(_CATEGORY_NORMALIZE)
    return CaseNumber(
        year=int(match.group("year")),
        category=category,
        number=int(match.group("number")),
    )


def normalize_case_number(text: str | None) -> str | None:
    """把任意寫法的案號轉為正規字串。找不到時回傳 ``None``。"""
    case = parse_case_number(text)
    return case.canonical() if case else None


def extract_case_numbers(text: str | None) -> list[str]:
    """抽出文中**所有**案號的正規形式，去重且保持出現順序。

    一篇報導常同時提及多個審級的案號（如一審與二審），
    只取第一個會讓跨審級的事件歸屬失效。
    """
    if not text:
        return []

    seen: dict[str, None] = {}
    for match in _CASE_PATTERN.finditer(to_halfwidth(str(text))):
        category = match.group("category").translate(_CATEGORY_NORMALIZE)
        case = CaseNumber(
            year=int(match.group("year")),
            category=category,
            number=int(match.group("number")),
        )
        seen.setdefault(case.canonical(), None)
    return list(seen)


# ---------------------------------------------------------------- 統一編號

# 財政部統一編號檢查碼權重
_TAX_ID_WEIGHTS = (1, 2, 1, 2, 1, 2, 4, 1)

_TAX_ID_CANDIDATE = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def is_valid_tax_id(value: str | None) -> bool:
    """驗證台灣統一編號的檢查碼。

    演算法：每位數字乘以對應權重，取乘積的各位數字和，全部加總後須為 5 的倍數。
    第 7 位為 7 時另有特例（總和或總和加 1 為 5 的倍數皆可）。

    需要驗證檢查碼而非只比對長度，是因為新聞內文常出現 8 位數字
    （金額、日期串、電話），不驗證會產生大量誤判的實體。

    >>> is_valid_tax_id("22099131")   # 台積電
    True
    >>> is_valid_tax_id("12345678")
    False
    """
    if not value:
        return False

    digits_text = to_halfwidth(str(value)).strip()
    if not re.fullmatch(r"\d{8}", digits_text):
        return False

    digits = [int(char) for char in digits_text]
    total = 0
    for digit, weight in zip(digits, _TAX_ID_WEIGHTS):
        product = digit * weight
        total += product // 10 + product % 10

    if total % 5 == 0:
        return True
    # 第 7 位為 7 的特例
    return digits[6] == 7 and (total + 1) % 5 == 0


def extract_tax_ids(text: str | None) -> list[str]:
    """從文字中抽出所有通過檢查碼驗證的統一編號，保持出現順序且去重。"""
    if not text:
        return []

    seen: dict[str, None] = {}
    for match in _TAX_ID_CANDIDATE.finditer(to_halfwidth(str(text))):
        candidate = match.group(1)
        if is_valid_tax_id(candidate):
            seen.setdefault(candidate, None)
    return list(seen)


# ---------------------------------------------------------------- 標案編號

def normalize_tender_id(value: str | None) -> str | None:
    """正規化政府採購標案編號。

    各機關的標案編號格式自訂，沒有統一規則可驗證，因此只做保守正規化：
    全形轉半形、去除空白與常見分隔符號的差異、英文字母轉大寫。
    刻意不做格式驗證——猜錯格式會漏掉真實標案，代價高於留下少數雜訊。
    """
    if not value:
        return None

    normalized = to_halfwidth(str(value)).strip().upper()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.replace("–", "-").replace("—", "-").replace("_", "-")
    return normalized or None
