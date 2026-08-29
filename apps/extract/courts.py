"""法院與檢察機關名稱的正規化。

**正規化不交給 LLM。** 實測 LLM 會自行把「北院」「台東地院」展開為
「臺灣臺北地方法院」「臺灣臺東地方法院」——結果有用（實體比對需要
統一形式），但有兩個問題：

1. **無法驗證**。展開後的字串不在原文中，回頭比對會被誤判為幻覺，
   使真正的幻覺被雜訊淹沒。
2. **可能猜錯**。原文若只寫「地院」而未指明轄區，LLM 會補上一個
   看似合理的地名，而那是憑空捏造。

法院名稱是**固定清單**，用對照表處理既可靠又可驗證。抽取端只要求
照原文輸出，正規化在此以確定性的規則完成。
"""
from __future__ import annotations

import re

__all__ = ["normalize_court", "COURT_ALIASES"]

_CITIES = (
    "臺北", "台北", "新北", "士林", "桃園", "新竹", "苗栗", "臺中", "台中",
    "彰化", "南投", "雲林", "嘉義", "臺南", "台南", "高雄", "橋頭", "屏東",
    "臺東", "台東", "花蓮", "宜蘭", "基隆", "澎湖", "金門", "連江",
)
#: 「臺」與「台」在官方與新聞中混用，一律以官方的「臺」為正規形式
_CANON_CITY = {"台北": "臺北", "台中": "臺中", "台南": "臺南", "台東": "臺東"}

COURT_ALIASES: dict[str, str] = {}
for _city in _CITIES:
    _canon = _CANON_CITY.get(_city, _city)
    _full_court = f"臺灣{_canon}地方法院"
    _full_pros = f"臺灣{_canon}地方檢察署"
    for _alias in (f"{_city}地院", f"{_city}地方法院", f"{_city}院"):
        COURT_ALIASES[_alias] = _full_court
    for _alias in (f"{_city}地檢", f"{_city}地檢署", f"{_city}地方檢察署"):
        COURT_ALIASES[_alias] = _full_pros

COURT_ALIASES.update({
    "高院": "臺灣高等法院", "高等法院": "臺灣高等法院",
    "最高院": "最高法院",
    "北院": "臺灣臺北地方法院", "北檢": "臺灣臺北地方檢察署",
    "中院": "臺灣臺中地方法院", "中檢": "臺灣臺中地方檢察署",
    "南院": "臺灣臺南地方法院", "南檢": "臺灣臺南地方檢察署",
    "雄院": "臺灣高雄地方法院", "雄檢": "臺灣高雄地方檢察署",
    "士院": "臺灣士林地方法院", "士檢": "臺灣士林地方檢察署",
    "橋院": "臺灣橋頭地方法院", "橋檢": "臺灣橋頭地方檢察署",
    "新北院": "臺灣新北地方法院", "新北檢": "臺灣新北地方檢察署",
    "高檢": "臺灣高等檢察署", "高檢署": "臺灣高等檢察署",
    "最高檢": "最高檢察署",
})


def normalize_court(name: str | None) -> str:
    """把法院或檢察機關的簡稱展開為官方全名。

    無法對應時**原樣回傳**，不猜測——原文若只寫「地院」而未指明轄區，
    補上任何地名都是捏造。
    """
    if not name:
        return ""
    text = name.strip()
    if not text:
        return ""

    # 已是全名（以「臺灣」或「最高」開頭且含「法院」「檢察署」）
    if re.match(r"^(臺灣|台灣|最高)", text) and ("法院" in text or "檢察署" in text):
        return text.replace("台灣", "臺灣")

    if text in COURT_ALIASES:
        return COURT_ALIASES[text]

    # 「臺灣」前綴缺漏的情況，如「臺北地方法院」
    for alias, full in COURT_ALIASES.items():
        if text == alias:
            return full
    return text
