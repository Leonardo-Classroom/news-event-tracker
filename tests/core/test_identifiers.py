"""識別碼模組的 small 測試。

ADR-0001 把「識別碼精確比對」當成事件歸屬的第一優先路徑，
因此這裡的正確性直接決定裁判書能否歸入正確事件。
"""
import pytest

from apps.core.identifiers import (
    CaseNumber,
    extract_tax_ids,
    is_valid_tax_id,
    normalize_case_number,
    normalize_tender_id,
    parse_case_number,
)


class TestParseCaseNumber:
    def test_標準格式(self):
        assert parse_case_number("111年度金重訴字第123號") == CaseNumber(
            111, "金重訴", 123
        )

    def test_夾在長句中(self):
        text = "臺灣高等法院111年度金重訴字第123號刑事判決，被告不服提起上訴。"
        assert parse_case_number(text) == CaseNumber(111, "金重訴", 123)

    @pytest.mark.parametrize(
        "raw",
        [
            "111年度金重訴字第123號",
            "111 年度 金重訴 字第 123 號",     # 各段有空白
            "111年金重訴字第123號",            # 省略「度」
            "１１１年度金重訴字第１２３號",      # 全形數字
            "111年度金重訴字123號",            # 省略「第」
        ],
    )
    def test_容忍的書寫變體(self, raw):
        """官方文件與新聞的寫法不一致，全部要能正規化到同一個結果。"""
        assert parse_case_number(raw) == CaseNumber(111, "金重訴", 123)

    def test_臺與台統一(self):
        """官方用「臺」新聞用「台」，不統一會導致同一案件比對失敗。"""
        assert parse_case_number("110年度臺上字第55號") == CaseNumber(110, "台上", 55)
        assert parse_case_number("110年度台上字第55號") == CaseNumber(110, "台上", 55)

    def test_單字字別(self):
        assert parse_case_number("112年度易字第9號") == CaseNumber(112, "易", 9)

    @pytest.mark.parametrize("raw", [None, "", "這則新聞沒有案號", "111年3月5日"])
    def test_無案號時回傳_None(self, raw):
        assert parse_case_number(raw) is None

    def test_不誤把日期當案號(self):
        """日期字串含「年」與數字，不得被誤判。"""
        assert parse_case_number("民國111年3月5日宣判") is None


class TestNormalizeCaseNumber:
    def test_各種變體正規化為同一字串(self):
        variants = [
            "111 年度 金重訴 字第 123 號",
            "111年金重訴字第123號",
            "１１１年度金重訴字第１２３號",
        ]
        results = {normalize_case_number(v) for v in variants}
        assert results == {"111年度金重訴字第123號"}

    def test_無案號回傳_None(self):
        assert normalize_case_number("沒有案號") is None


class TestIsValidTaxId:
    @pytest.mark.parametrize("tax_id", ["22099131", "04541302"])
    def test_真實統編通過(self, tax_id):
        assert is_valid_tax_id(tax_id) is True

    @pytest.mark.parametrize(
        "value",
        [
            "12345678",   # 檢查碼不符
            "1234567",    # 太短
            "123456789",  # 太長
            "2209913a",   # 含字母
            "",
            None,
        ],
    )
    def test_無效輸入被拒(self, value):
        assert is_valid_tax_id(value) is False

    def test_全形數字可接受(self):
        assert is_valid_tax_id("２２０９９１３１") is True


class TestExtractTaxIds:
    def test_從內文抽出(self):
        text = "台積電（統編 22099131）與鴻海（統編 04541302）皆列名。"
        assert extract_tax_ids(text) == ["22099131", "04541302"]

    def test_過濾未通過檢查碼的八位數(self):
        """新聞內文常有金額、電話等八位數字，不驗證檢查碼會產生大量假實體。"""
        text = "本案金額達 12345678 元，涉案公司統編 22099131。"
        assert extract_tax_ids(text) == ["22099131"]

    def test_不擷取九位以上數字的片段(self):
        assert extract_tax_ids("流水號 220991311 並非統編") == []

    def test_重複者只留一次且保持順序(self):
        text = "22099131 ... 04541302 ... 22099131"
        assert extract_tax_ids(text) == ["22099131", "04541302"]

    def test_空輸入(self):
        assert extract_tax_ids(None) == []


class TestNormalizeTenderId:
    def test_全形與空白正規化(self):
        assert normalize_tender_id(" ａｂｃ-１２３ ") == "ABC-123"

    def test_各種連字號統一(self):
        assert normalize_tender_id("A–1") == "A-1"
        assert normalize_tender_id("A—1") == "A-1"
        assert normalize_tender_id("A_1") == "A-1"

    def test_空輸入(self):
        assert normalize_tender_id(None) is None
        assert normalize_tender_id("   ") is None
