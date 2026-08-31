"""司法院月封存檔下載與逐案抽取測試（任務 33 進階）。

網路請求與 ``unrar`` 子程序一律 mock——這裡要驗證的是「兩種失效
模式有沒有分清楚」「案號格式有沒有跟系統其他地方一致」，不是驗證
`requests` 或 `unrar` 本身的行為。
"""
import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from apps.ingest.judicial_opendata import (
    MonthlyArchive, SessionExpired, _parse_dataset_date,
    case_json_to_parsed_document, download_archive, find_case_in_archive,
    list_monthly_archives,
)
from apps.ingest.models import ExternalSession

UTC = dt.timezone.utc

SAMPLE_CASE = {
    "JID": "TPDM,113,金訴,51,20260326,1",
    "JYEAR": "113",
    "JCASE": "金訴",
    "JNO": "51",
    "JDATE": "20260326",
    "JTITLE": "違反貪污治罪條例等",
    "JFULL": "臺灣臺北地方法院刑事判決……（全文略）",
    "JPDF": "https://data.judicial.gov.tw/opendl/JDocFile/TPDM/113%2c%e9%87%91%e8%a8%b4%2c51%2c20260326%2c1.pdf",
}


class TestCaseJsonToParsedDocument:
    def test_轉出的案號格式與系統其他地方一致(self):
        """必須是 Event.case_numbers 與 identifier_match() 比對時
        採用的正規形式——各自組字串遲早會因格式 drift 而比對失敗。"""
        doc = case_json_to_parsed_document(SAMPLE_CASE)
        assert doc.extra["case_number"] == "113年度金訴字第51號"
        assert "113年度金訴字第51號" in doc.title

    def test_url使用官方pdf直連(self):
        """JPDF 免登入即可存取，比封存檔本身更適合當長期原文連結。"""
        doc = case_json_to_parsed_document(SAMPLE_CASE)
        assert doc.url == SAMPLE_CASE["JPDF"]

    def test_全文進入body(self):
        doc = case_json_to_parsed_document(SAMPLE_CASE)
        assert doc.body == SAMPLE_CASE["JFULL"]

    def test_日期正確解析(self):
        doc = case_json_to_parsed_document(SAMPLE_CASE)
        assert doc.published_at == dt.datetime(2026, 3, 26, tzinfo=UTC)

    def test_依法不公開案件排除且回傳None(self):
        """任務 35 的排除閘門接在這裡——回傳 None 代表不入庫，
        呼叫端不能把它當成解析失敗。"""
        case = {**SAMPLE_CASE, "JTITLE": "聲請核發通常保護令", "JCASE": "家護"}
        assert case_json_to_parsed_document(case) is None

    def test_缺少pdf連結不入庫(self):
        case = {**SAMPLE_CASE, "JPDF": ""}
        assert case_json_to_parsed_document(case) is None

    def test_日期格式異常不崩潰(self):
        case = {**SAMPLE_CASE, "JDATE": ""}
        doc = case_json_to_parsed_document(case)
        assert doc.published_at is None


class TestParseDatasetDate:
    """實測發現：本平台的 API 回應秒的小數部分只有 1-2 位數（如
    ``.95``），Python 3.10 的 ``datetime.fromisoformat`` 只接受 3 或
    6 位數的微秒（3.11 起才放寬）——本專案跑在 3.10，正式環境對
    真實資料直接呼叫 fromisoformat 會炸掉，這正是 list_monthly_archives
    第一次對真實 API 執行時發生的事。"""

    def test_實測真實格式的短小數秒(self):
        result = _parse_dataset_date("2026-08-16T05:38:59.95+08:00")
        assert result == dt.datetime(2026, 8, 16, 5, 38, 59, 950000, tzinfo=result.tzinfo)

    def test_單位數小數秒(self):
        result = _parse_dataset_date("2026-08-16T05:38:59.9+08:00")
        assert result.microsecond == 900000

    def test_標準六位數微秒不受影響(self):
        result = _parse_dataset_date("2026-08-16T05:38:59.950000+08:00")
        assert result.microsecond == 950000

    def test_無小數秒不受影響(self):
        result = _parse_dataset_date("2026-08-16T05:38:59+08:00")
        assert result.microsecond == 0


class TestListMonthlyArchives:
    def test_只收月封存檔標題並依日期排序(self):
        """用語辭典也會被 keyword=裁判書 搜到，但它不是逐案 JSON。
        分類 A/B 不當作排除條件——公開的月封存檔同樣要收。"""
        fake_response = MagicMock()
        fake_response.json.return_value = {"pagedList": {"items": [
            {"datasetId": 1, "title": "裁判書用語辭典", "categoryDataset": "A",
             "filesetLists": [{"fileSetId": 100}], "publishedDate": "2024-06-04T16:00:00.95+08:00"},
            {"datasetId": 2, "title": "202605裁判書", "categoryDataset": "B",
             "filesetLists": [{"fileSetId": 200}], "publishedDate": "2026-07-01T00:00:00.5+08:00"},
            {"datasetId": 3, "title": "202606裁判書--(20260816Update)", "categoryDataset": "A",
             "filesetLists": [{"fileSetId": 300}], "publishedDate": "2026-08-16T00:00:00.123+08:00"},
        ]}}
        fake_response.raise_for_status = MagicMock()

        with patch("apps.ingest.judicial_opendata.requests.get", return_value=fake_response):
            archives = list_monthly_archives()

        assert [a.dataset_id for a in archives] == [3, 2]
        assert archives[0].gated is False
        assert archives[1].gated is True

    def test_無檔案的資料集被排除(self):
        fake_response = MagicMock()
        fake_response.json.return_value = {"pagedList": {"items": [
            {"datasetId": 1, "title": "空的", "categoryDataset": "B",
             "filesetLists": [], "publishedDate": "2026-08-01T00:00:00+08:00"},
        ]}}
        fake_response.raise_for_status = MagicMock()
        with patch("apps.ingest.judicial_opendata.requests.get", return_value=fake_response):
            assert list_monthly_archives() == []


class TestDownloadArchive:
    ARCHIVE = MonthlyArchive(dataset_id=1, title="t", fileset_id=100, published_at=None)

    def _json_denied(self):
        resp = MagicMock(status_code=500, content=b'{"succeeded":false}')
        resp.headers = {"Content-Type": "application/json"}
        return resp

    def _file_ok(self, payload=b"RAR-DATA"):
        resp = MagicMock(status_code=200, content=payload)
        resp.headers = {"Content-Type": "application/octet-stream"}
        return resp

    def test_公開檔不需session(self):
        with patch("apps.ingest.judicial_opendata.requests.get",
                   return_value=self._file_ok()) as mocked:
            result = download_archive(self.ARCHIVE, session=None)
        assert result == b"RAR-DATA"
        assert "Cookie" not in mocked.call_args.kwargs["headers"]

    def test_會員限定無session時拋SessionExpired(self, db):
        session = ExternalSession.objects.create(
            slug="t", name="測試平台", login_url="https://example.test/login")
        with patch("apps.ingest.judicial_opendata.requests.get",
                   return_value=self._json_denied()):
            with pytest.raises(SessionExpired):
                download_archive(self.ARCHIVE, session=session)

    def test_會員限定先裸抓失敗再帶cookie(self, db):
        session = ExternalSession.objects.create(
            slug="t", name="測試平台", login_url="https://example.test/login",
            cookie_header="valid=1",
        )
        with patch("apps.ingest.judicial_opendata.requests.get",
                   side_effect=[self._json_denied(), self._file_ok()]) as mocked:
            result = download_archive(self.ARCHIVE, session=session)
        assert result == b"RAR-DATA"
        assert "Cookie" not in mocked.call_args_list[0].kwargs["headers"]
        assert mocked.call_args_list[1].kwargs["headers"]["Cookie"] == "valid=1"

    def test_未授權回應視為session過期而非查無資料(self, db):
        """實測發現的兩種失效模式必須分清楚：未登入時下載端點回傳的
        錯誤格式跟『查無資料』一樣（HTTP 500 夾帶 JSON），
        混淆會讓 session 過期被誤判成『案子還沒判』，不會有人發現
        要重新登入。"""
        session = ExternalSession.objects.create(
            slug="t", name="測試平台", login_url="https://example.test/login",
            cookie_header="stale=1",
        )
        with patch("apps.ingest.judicial_opendata.requests.get",
                   return_value=self._json_denied()):
            with pytest.raises(SessionExpired):
                download_archive(self.ARCHIVE, session=session)


class TestFindCaseInArchive:
    def test_找不到案件時回傳None(self):
        with patch("apps.ingest.judicial_opendata.subprocess.run") as mocked_run:
            mocked_run.return_value = MagicMock(stdout="其他不相干的檔名.json\n")
            result = find_case_in_archive(b"fake", roc_year=113, category="金訴", number=51)
        assert result is None
        # 找不到就不該呼叫第二次（解壓）——lb 只列清單一次即可判斷
        assert mocked_run.call_count == 1

    def test_命中時解壓並解析內容(self, tmp_path):
        """用真實檔案系統驗證『解壓後讀取 JSON』這段邏輯，
        只有 unrar 子程序本身被 mock。"""
        import json as jsonlib

        target_name = "TPDM,113,金訴,51,20260326,1.json"

        def fake_run(cmd, **kwargs):
            if cmd[1] == "lb":
                return MagicMock(stdout=f"其他案件.json\n{target_name}\n")
            # x：把解壓目標寫到暫存目錄，模擬 unrar 真的解壓出檔案
            archive_path, filename, out_dir = cmd[3], cmd[4], cmd[5]
            (__import__("pathlib").Path(out_dir) / filename).write_text(
                jsonlib.dumps(SAMPLE_CASE, ensure_ascii=False), encoding="utf-8")
            return MagicMock()

        with patch("apps.ingest.judicial_opendata.subprocess.run", side_effect=fake_run):
            result = find_case_in_archive(b"fake", roc_year=113, category="金訴", number=51)

        assert result["JID"] == SAMPLE_CASE["JID"]
