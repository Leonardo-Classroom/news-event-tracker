"""日期解析。

移植自既有爬蟲 ``crawler/common.py`` 的 ``parse_date``，並補上兩項新需求：

1. 回傳 ``datetime.date`` 而非字串——舊版回傳 "YYYY-MM-DD" 字串是為了組檔名，
   新系統要存進資料庫並做時間運算。
2. 支援**民國年**。官方文件（裁判書、公文、議案）幾乎一律使用民國紀年，
   而這正是本系統的差異化資料來源，不能不處理。
"""
from __future__ import annotations

import datetime as _dt
import re

__all__ = ["parse_date", "parse_datetime", "roc_to_ad", "to_halfwidth"]

# 民國元年 = 西元 1912 年
_ROC_OFFSET = 1911

# 全形 ASCII（U+FF01–U+FF5E）對應半形（U+0021–U+007E），外加全形空格。
# 只轉數字不夠——官方網頁的英文字母、括號、連字號也常是全形，
# 若不一併處理，標案編號與案號的正規化會漏掉這些變體。
_FULLWIDTH_ASCII = str.maketrans(
    {chr(code): chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)}
    | {"　": " "}
)

# 西元：2026-03-05 / 2026/3/5 / 2026.03.05 / 2026年3月5日
_AD_PATTERN = re.compile(
    r"(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})"
)

# 民國：民國111年3月5日 / 111年3月5日 / 111.03.05 / 中華民國111年3月5日
# 年份限 2–3 位，避免誤吃西元年
_ROC_PATTERN = re.compile(
    r"(?:中華)?(?:民國)?\s*(?P<y>\d{2,3})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})"
)


def to_halfwidth(text: str) -> str:
    """全形 ASCII 轉半形（數字、英文字母、標點、空格）。

    官方網頁大量使用全形字元，不正規化會導致同一個識別碼
    在不同來源被視為不同字串，破壞 ADR-0001 的精確比對策略。
    """
    return text.translate(_FULLWIDTH_ASCII)


def roc_to_ad(roc_year: int) -> int:
    """民國年轉西元年。民國 111 年 → 2022 年。"""
    if roc_year < 1:
        raise ValueError(f"民國年必須為正整數，收到 {roc_year}")
    return roc_year + _ROC_OFFSET


def parse_date(raw: str | None, *, prefer_roc: bool = False) -> _dt.date | None:
    """從各種日期字串抽出日期。無法解析時回傳 ``None``。

    Args:
        raw: 原始字串，可能含大量無關文字。
        prefer_roc: 為 True 時，2–3 位數年份一律視為民國年。
            官方文件來源應設為 True；新聞來源留 False（西元為主）。

    回傳 ``None`` 而非拋例外，是因為呼叫端多半在批次處理，
    單筆解析失敗不應中斷整批。缺日期的文件仍有價值，只是排序時往後放。
    """
    if not raw:
        return None

    text = to_halfwidth(str(raw))

    # 西元四位年優先——四位數不可能是民國年，無歧義
    match = _AD_PATTERN.search(text)
    if match:
        return _build_date(
            int(match.group("y")), int(match.group("m")), int(match.group("d"))
        )

    # 明確標示「民國」的，無論 prefer_roc 為何都當民國年
    explicit_roc = re.search(r"(?:中華)?民國", text) is not None
    if explicit_roc or prefer_roc:
        match = _ROC_PATTERN.search(text)
        if match:
            return _build_date(
                roc_to_ad(int(match.group("y"))),
                int(match.group("m")),
                int(match.group("d")),
            )

    return None


def _build_date(year: int, month: int, day: int) -> _dt.date | None:
    """建立 date，數值不合法時回傳 None 而非拋例外。"""
    try:
        return _dt.date(year, month, day)
    except ValueError:
        return None


# ISO 8601 常見變體。官方 JSON-LD 幾乎都用這個格式。
_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
)


def parse_datetime(raw: str | None) -> _dt.datetime | None:
    """解析含時間的時間戳，回傳 aware datetime。無法解析時回傳 ``None``。

    與 ``parse_date`` 的分工很重要：``parse_date`` 用於「文件裡提到的
    某個日期」（如判決日期），只需日期精度；``parse_datetime`` 用於
    「這篇文章何時發布」，**時間精度不可丟失**。

    曾因混用兩者而產生一個難以察覺的錯誤：JSON-LD 的
    ``datePublished`` 是完整 ISO 時間戳，但被 ``parse_date`` 截成日期
    後補上午夜，導致某來源的所有文章時間戳都變成當日 00:00——
    看起來永遠是最早發布的，破壞了所有依時間排序的邏輯。
    """
    if not raw:
        return None

    text = to_halfwidth(str(raw)).strip()
    # Python 3.10 的 %z 不接受結尾的 Z
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    # %z 在 3.10 可接受 +08:00，但保險起見同時嘗試去冒號的形式
    variants = [text, re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", text)]

    for candidate in variants:
        for fmt in _ISO_FORMATS:
            try:
                parsed = _dt.datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)
    return None
