"""既有語料回填的測試。

parse_file 是純函式（small）。它處理的是既有爬蟲產出的固定格式，
格式若變動會靜默丟失資料，因此邊界要測得完整。
"""
import datetime as dt

import pytest

from apps.ingest.management.commands.backfill_dataset import parse_file

SEP = "-" * 40

SAMPLE = f"""標題：負油價是小確幸？ 背後隱憂你我生活都躲不過
記者：仝澤蓉
新聞網：聯合新聞網
日期：2020-04-21
網址：https://udn.com/news/story/120885/4507902
{SEP}
新冠肺炎疫情造成石油需求減緩，5月美國西德州中級原油期貨結算價格
來到每桶負37.63美元，出現史上首見的負油價。
"""


class TestParseFile:
    def test_解析完整欄位(self):
        result = parse_file(SAMPLE)
        assert result["title"] == "負油價是小確幸？ 背後隱憂你我生活都躲不過"
        assert result["author"] == "仝澤蓉"
        assert result["url"] == "https://udn.com/news/story/120885/4507902"
        assert "負37.63美元" in result["raw_body"]

    def test_日期以正午為代表時間(self):
        """既有語料只有日期精度。用午夜會讓這批文件在依時間排序時
        系統性排在同日新抓取者之前——曾因此誤判轉載方向。"""
        result = parse_file(SAMPLE)
        assert result["published_at"] == dt.datetime(
            2020, 4, 21, 12, tzinfo=dt.timezone.utc)

    def test_移除查詢參數(self):
        text = SAMPLE.replace(
            "story/120885/4507902", "story/120885/4507902?from=udn_ch2")
        assert parse_file(text)["url"].endswith("4507902")

    def test_剝除付費牆推廣文字(self):
        """UDN 的文章尾端夾帶訂閱宣傳，那不是報導內容，
        留著會汙染去重與後續抽取。"""
        text = SAMPLE.rstrip() + "\n\n你今年最好的選擇\n聯合報每天報版內容。\n"
        result = parse_file(text)
        assert "你今年最好的選擇" not in result["raw_body"]
        assert "負37.63美元" in result["raw_body"]

    @pytest.mark.parametrize("missing", ["標題", "網址"])
    def test_缺必要欄位回傳_None(self, missing):
        lines = [ln for ln in SAMPLE.splitlines()
                 if not ln.startswith(f"{missing}：")]
        assert parse_file("\n".join(lines)) is None

    def test_無分隔線回傳_None(self):
        assert parse_file("標題：無分隔線\n網址：https://x.test/1") is None

    def test_日期格式不符時為_None_而非拋錯(self):
        """單筆日期異常不該中斷整批回填。"""
        text = SAMPLE.replace("2020-04-21", "民國109年4月21日")
        result = parse_file(text)
        assert result is not None
        assert result["published_at"] is None

    def test_空字串(self):
        assert parse_file("") is None
