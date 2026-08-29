"""法院名稱正規化的 small 測試。

正規化刻意不交給 LLM：實測它會把「北院」展開為「臺灣臺北地方法院」，
結果有用但無法回頭驗證，且原文若未指明轄區時可能猜錯。
確定性的對照表兩個問題都沒有。
"""
import pytest

from apps.extract.courts import normalize_court


class TestNormalizeCourt:
    @pytest.mark.parametrize("alias,expected", [
        ("北院", "臺灣臺北地方法院"),
        ("北檢", "臺灣臺北地方檢察署"),
        ("台東地院", "臺灣臺東地方法院"),
        ("嘉義地方法院", "臺灣嘉義地方法院"),
        ("雲林地檢", "臺灣雲林地方檢察署"),
        ("新北檢", "臺灣新北地方檢察署"),
        ("高院", "臺灣高等法院"),
        ("高檢署", "臺灣高等檢察署"),
    ])
    def test_簡稱展開(self, alias, expected):
        assert normalize_court(alias) == expected

    def test_已是全名者不變(self):
        assert normalize_court("臺灣高等法院") == "臺灣高等法院"
        assert normalize_court("最高法院") == "最高法院"

    def test_台統一為臺(self):
        """官方一律用「臺」，新聞混用。不統一會讓同一機關被視為兩個實體。"""
        assert normalize_court("台灣臺北地方法院") == "臺灣臺北地方法院"
        assert normalize_court("台北地院") == "臺灣臺北地方法院"

    def test_未指明轄區者原樣回傳(self):
        """原文只寫「地院」時補上任何地名都是捏造。"""
        assert normalize_court("地院") == "地院"
        assert normalize_court("法院") == "法院"

    def test_未知名稱原樣回傳(self):
        assert normalize_court("某某特別法庭") == "某某特別法庭"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_空值(self, value):
        assert normalize_court(value) == ""
