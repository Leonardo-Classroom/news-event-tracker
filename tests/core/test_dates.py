"""日期解析的 small 測試。

重點在民國年——官方文件（裁判書、公文、議案）幾乎一律民國紀年，
而官方源正是本系統的差異化來源。
"""
import datetime as dt

import pytest

from apps.core.dates import parse_date, roc_to_ad, to_halfwidth


class TestParseDateAD:
    @pytest.mark.parametrize(
        "raw",
        [
            "2026-03-05",
            "2026/3/5",
            "2026.03.05",
            "2026年3月5日",
            "發布時間：2026-03-05 14:30:00",
            "2026-03-05T14:30:00+08:00",
        ],
    )
    def test_西元各種格式(self, raw):
        assert parse_date(raw) == dt.date(2026, 3, 5)

    @pytest.mark.parametrize("raw", [None, "", "沒有日期", "第三季"])
    def test_無日期回傳_None(self, raw):
        assert parse_date(raw) is None

    def test_不合法日期回傳_None(self):
        """2 月 30 日不存在，應回傳 None 而非拋例外——批次處理不該被單筆中斷。"""
        assert parse_date("2026-02-30") is None


class TestParseDateROC:
    def test_明確標示民國(self):
        assert parse_date("民國111年3月5日") == dt.date(2022, 3, 5)

    def test_中華民國前綴(self):
        assert parse_date("中華民國111年3月5日") == dt.date(2022, 3, 5)

    def test_明確標示民國時不需_prefer_roc(self):
        """字面已寫「民國」，就算來源設定為新聞（prefer_roc=False）也該正確解析。"""
        assert parse_date("民國111年3月5日", prefer_roc=False) == dt.date(2022, 3, 5)

    def test_官方來源以_prefer_roc_解析裸年份(self):
        assert parse_date("111年3月5日", prefer_roc=True) == dt.date(2022, 3, 5)
        assert parse_date("111.03.05", prefer_roc=True) == dt.date(2022, 3, 5)

    def test_新聞來源不把裸兩三位數當民國年(self):
        """新聞的 prefer_roc=False。「111年」在新聞語境多半不是日期。"""
        assert parse_date("111年3月5日", prefer_roc=False) is None

    def test_四位數年份永遠是西元(self):
        """即使來源是官方文件，四位數年份也不該被當成民國年。"""
        assert parse_date("2026年3月5日", prefer_roc=True) == dt.date(2026, 3, 5)


class TestRocToAd:
    def test_換算(self):
        assert roc_to_ad(111) == 2022
        assert roc_to_ad(1) == 1912

    def test_非正數拋錯(self):
        with pytest.raises(ValueError):
            roc_to_ad(0)


class TestToHalfwidth:
    def test_全形數字轉半形(self):
        assert to_halfwidth("１２３") == "123"

    def test_非數字不受影響(self):
        assert to_halfwidth("台北２０２６") == "台北2026"


class TestParseDatetime:
    """回歸測試：曾因用 parse_date 解析 JSON-LD 的 datePublished，
    把完整時間戳截成日期後補午夜，導致 104 篇（約 23% 語料）的
    published_at 全部變成當日 00:00——看起來永遠最早發布，
    破壞了轉載歸併的方向判定與所有依時間排序的邏輯。
    """

    @pytest.mark.parametrize("raw,expected", [
        ("2026-08-29T19:24:00+08:00",
         dt.datetime(2026, 8, 29, 19, 24, tzinfo=dt.timezone(dt.timedelta(hours=8)))),
        ("2026-08-29T11:24:00Z",
         dt.datetime(2026, 8, 29, 11, 24, tzinfo=dt.timezone.utc)),
        ("2026-08-29 19:24:00",
         dt.datetime(2026, 8, 29, 19, 24, tzinfo=dt.timezone.utc)),
    ])
    def test_保留時間精度(self, raw, expected):
        from apps.core.dates import parse_datetime
        assert parse_datetime(raw) == expected

    def test_時間不得為午夜除非原本就是(self):
        """核心回歸：19:24 不可變成 00:00。"""
        from apps.core.dates import parse_datetime
        result = parse_datetime("2026-08-29T19:24:00+08:00")
        assert (result.hour, result.minute) == (19, 24)

    def test_一律回傳_aware_datetime(self):
        from apps.core.dates import parse_datetime
        assert parse_datetime("2026-08-29 19:24:00").tzinfo is not None

    @pytest.mark.parametrize("raw", [None, "", "不是時間戳", "2026-08-29"])
    def test_無法解析回傳_None(self, raw):
        from apps.core.dates import parse_datetime
        assert parse_datetime(raw) is None
