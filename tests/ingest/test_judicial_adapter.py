"""司法院裁判書 adapter 測試（任務 33）。

用 Scope 0 任務 2 人工穿刺驗證時取得的真實裁判書 HTML
（`fixtures/html/judgment_sample.html`，臺灣臺北地方法院 113 年度
金訴字第 51 號，柯文哲京華城案，約 30 萬字全文）——不是合成資料，
是實際驗證過「能乾淨對應回事件」的那份文件本身。
"""
from pathlib import Path

import pytest

from apps.ingest.adapters.judicial import JudicialAdapter, parse_judgment_html

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "html" / "judgment_sample.html"


@pytest.fixture(scope="module")
def real_judgment_html():
    return FIXTURE.read_text(encoding="utf-8", errors="ignore")


class TestParseJudgmentHtml:
    def test_解析出正確案號(self, real_judgment_html):
        data = parse_judgment_html(real_judgment_html)
        assert data.case_number == "113年度金訴字第51號"

    def test_解析出法院全名不含案由字樣(self, real_judgment_html):
        """法院名稱不該混入「刑事」「判決」等案由字樣——
        apps.extract.courts.normalize_court 的別名表是以純法院名稱比對，
        混入案由字樣會讓正規化查表失敗。"""
        data = parse_judgment_html(real_judgment_html)
        assert data.court == "臺灣臺北地方法院"

    def test_解析出被告名單(self, real_judgment_html):
        data = parse_judgment_html(real_judgment_html)
        for name in ["柯文哲", "沈慶京", "應曉薇", "黃景茂"]:
            assert name in data.defendants

    def test_解析出宣示日期(self, real_judgment_html):
        """本文件含 3 個『中華民國 X 年 X 月 X 日』格式的匹配：
        事發日期（案情敘述）、宣示日期（法官簽名前）、附表目錄裡剛好
        像日期格式的文字。只有取「緊接法官／審判長」的那個才對——
        原本寫成取最後一個，會誤取到附表目錄那段。"""
        data = parse_judgment_html(real_judgment_html)
        import datetime as dt
        assert data.decided_on == dt.date(2026, 3, 26)

    def test_全文長度符合預期(self, real_judgment_html):
        """ADR-0005 記載全文約 30 萬字，用長度下限確認抓到的是全文
        而非只有頁首（真正的內容遺漏會讓長度差一個數量級，容易發現）。"""
        data = parse_judgment_html(real_judgment_html)
        assert len(data.full_text) > 200_000

    def test_找不到案號時回傳None(self):
        assert parse_judgment_html("<html><body>無關頁面</body></html>") is None

    def test_空輸入不崩潰(self):
        assert parse_judgment_html("") is None


class TestJudicialAdapter:
    def test_符合Adapter協定回傳單篇文件(self, real_judgment_html):
        docs = JudicialAdapter().list_documents(
            real_judgment_html, base_url="https://judgment.judicial.gov.tw/x")
        assert len(docs) == 1
        doc = docs[0]
        assert doc.external_id == "113年度金訴字第51號"
        assert doc.extra["court"] == "臺灣臺北地方法院"
        assert "柯文哲" in doc.extra["defendants"]

    def test_無案號的頁面回傳空清單(self):
        docs = JudicialAdapter().list_documents(
            "<html>查無資料</html>", base_url="https://judgment.judicial.gov.tw/x")
        assert docs == []
