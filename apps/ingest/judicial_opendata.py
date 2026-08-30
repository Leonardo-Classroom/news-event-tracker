"""司法院資料開放平台的月封存檔下載與逐案抽取（任務 33 進階）。

## 為什麼是「重放登入 session」而非自動化登入

登入頁掛了 Cloudflare Turnstile，自動化瀏覽器（含 Playwright headed
模式）填完帳密後拿不到有效的驗證 token——這不是能調參數解決的問題，
是 Turnstile 刻意要擋自動化登入，headless 或 headed 對它而言沒有差異
（偵測依據是 ``navigator.webdriver``、CDP 協定痕跡等自動化特徵，不是
「看不看得到視窗」）。因此改為人工登入一次，把登入後的
``ExternalSession.cookie_header`` 存起來重放；下載檔案本身沒有這層
防護（實測用純 ``requests`` 帶 cookie 就能下載 270MB 的封存檔）。

## 為什麼不整包解壓

一份月封存檔上萬個案件、常見 200MB+，而每次「檢查某個追蹤中案件有
沒有新進展」只需要其中一筆。整包解壓再篩選，等於為了一筆資料付出
處理全部案件的時間與磁碟空間。改用 ``unrar lb``（僅列出檔名，讀取
壓縮檔的目錄結構，不解壓內容）先定位，再用 ``unrar x`` 只解壓命中的
單一檔案。

## 為什麼是「檢查最近幾個月」而非「查全部 30 年」

案號比對只在乎「這個追蹤中的案件最近有沒有新的司法動作」，不是
建立全庫索引（規格的 Non-Goal）。逐月從最新的往回查，找到即停止，
是這個用途該有的搜尋順序。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

import requests

from apps.core.identifiers import CaseNumber
from apps.ingest.adapters.base import ParsedDocument
from apps.ingest.judicial_exclusions import is_excluded_case
from apps.ingest.models import ExternalSession

logger = logging.getLogger(__name__)

__all__ = [
    "SessionExpired", "MonthlyArchive", "list_monthly_archives",
    "download_archive", "find_case_in_archive", "case_json_to_parsed_document",
]

DATASET_SEARCH_URL = "https://opendata.judicial.gov.tw/api/Datasets"
FILE_DOWNLOAD_URL = "https://opendata.judicial.gov.tw/api/FilesetLists/{fileset_id}/file"

#: 這個類別代表「公開、免登入即可下載」，見 apps.web.views 對
#: categoryDataset 的實測記錄：A=公開、B=會員限定。裁判書月封存檔
#: 全部是 B。
_GATED_CATEGORY = "B"

#: 秒的小數部分只有 1–2 位數，如 ".95"——Python 3.10 的
#: ``datetime.fromisoformat`` 只接受 3 或 6 位數的微秒（3.11 起才放寬），
#: 而本專案跑在 3.10。實測本平台的 API 回應確實是這種不合規格的格式，
#: 直接呼叫 fromisoformat 會在正式環境對真實資料炸掉——先补 0 到 6 位。
_SHORT_FRACTION_RE = re.compile(r"(\.\d{1,5})(?=[+-]\d{2}:\d{2}$|Z$|$)")


def _parse_dataset_date(value: str) -> dt.datetime:
    match = _SHORT_FRACTION_RE.search(value)
    if match:
        padded = match.group(1)[1:].ljust(6, "0")
        value = value[:match.start()] + "." + padded + value[match.end():]
    return dt.datetime.fromisoformat(value)


class SessionExpired(Exception):
    """``ExternalSession`` 的 cookie 已失效或尚未設定，需要重新登入。

    **與「這個月沒有這個案子」是不同的失效模式，必須分開處理**：
    未登入時下載端點回傳的錯誤格式與「查無資料」相同（皆為 HTTP 500
    夾帶 JSON 錯誤訊息），若不特別檢查，session 過期會被誤判成
    「案子還沒判」，而不會被察覺、也不會有人去重新登入。
    """


@dataclasses.dataclass(frozen=True)
class MonthlyArchive:
    dataset_id: int
    title: str
    fileset_id: int
    published_at: dt.datetime | None


def list_monthly_archives(*, limit: int = 24) -> list[MonthlyArchive]:
    """查詢最近的裁判書月封存檔中繼資料。**這一步免登入**——只有
    實際下載檔案內容才需要 session，搜尋本身是公開 API。
    """
    resp = requests.get(
        DATASET_SEARCH_URL,
        params={"keyword": "裁判書", "pageSize": 100, "page": 1,
               "sort.publishedDate.order": "desc"},
        headers={"Accept": "application/json"}, timeout=20,
    )
    resp.raise_for_status()
    items = resp.json()["pagedList"]["items"]

    archives = []
    for it in items:
        if it["categoryDataset"] != _GATED_CATEGORY or not it["filesetLists"]:
            continue
        published_at = None
        if it.get("publishedDate"):
            published_at = _parse_dataset_date(it["publishedDate"])
        archives.append(MonthlyArchive(
            dataset_id=it["datasetId"], title=it["title"],
            fileset_id=it["filesetLists"][0]["fileSetId"],
            published_at=published_at,
        ))
    archives.sort(key=lambda a: a.published_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                  reverse=True)
    return archives[:limit]


def download_archive(archive: MonthlyArchive, *, session: ExternalSession) -> bytes:
    """用登入 session 下載一份月封存檔。

    session 未設定或已過期時拋 ``SessionExpired``——用 Content-Type
    區分：正常下載是 ``application/octet-stream``，未授權時平台回傳
    JSON 錯誤（見本模組說明的「兩種失效模式」）。
    """
    if not session.is_set:
        raise SessionExpired(f"{session.name} 尚未登入，無法下載月封存檔")

    resp = requests.get(
        FILE_DOWNLOAD_URL.format(fileset_id=archive.fileset_id),
        headers={"Cookie": session.cookie_header, "Accept": "*/*"},
        timeout=180,
    )
    content_type = resp.headers.get("Content-Type", "")
    if resp.status_code != 200 or "json" in content_type:
        raise SessionExpired(
            f"{session.name} 的登入狀態可能已過期"
            f"（HTTP {resp.status_code}，Content-Type: {content_type}）"
        )
    return resp.content


def find_case_in_archive(archive_bytes: bytes, *, roc_year: int, category: str,
                         number: int) -> dict | None:
    """在封存檔裡定位並解析出符合案號的 JSON，不整包解壓。

    案件在封存檔內的檔名格式為
    ``{法院代碼},{民國年},{字別},{號},{裁判日期},{版本}.json``——
    以「,{年},{字別},{號},」組成的中段字串比對，能不靠法院代碼
    （未知）就在任何法院資料夾裡定位到目標案件。
    """
    pattern = f",{roc_year},{category},{number},"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        archive_path = tmp / "archive.rar"
        archive_path.write_bytes(archive_bytes)

        listing = subprocess.run(
            ["unrar", "lb", str(archive_path)],
            capture_output=True, text=True, timeout=60,
        )
        matches = [line for line in listing.stdout.splitlines() if pattern in line]
        if not matches:
            return None

        subprocess.run(
            ["unrar", "x", "-y", str(archive_path), matches[0], str(tmp) + "/"],
            capture_output=True, timeout=60, check=True,
        )
        extracted = tmp / matches[0]
        return json.loads(extracted.read_text(encoding="utf-8"))


def case_json_to_parsed_document(case: dict) -> ParsedDocument | None:
    """把封存檔裡的單筆案件 JSON 轉成 ``ParsedDocument``。

    案件類型排除（任務 35）在這裡執行，回傳 ``None`` 時代表依法不公開
    ——呼叫端必須把 ``None`` 當成「不入庫」，不是「解析失敗」。

    ``JPDF`` 存進 ``ParsedDocument.url``：這是司法院官方 PDF 的公開
    直連（免登入即可存取，已實測），比封存檔本身更適合作為長期保存
    的原文連結——公開頁的讀者點了就能看到官方原始 PDF，不需要任何
    帳號。
    """
    title = case.get("JTITLE", "")
    category = case.get("JCASE", "")
    if is_excluded_case(case_title=title, case_category=category):
        logger.info("案件 %s 依規則排除（案由：%s，字別：%s），不入庫",
                   case.get("JID"), title, category)
        return None

    url = case.get("JPDF", "")
    if not url:
        logger.warning("案件 %s 沒有 JPDF 連結，不入庫", case.get("JID"))
        return None

    published_at = None
    jdate = case.get("JDATE", "")
    if len(jdate) == 8:
        published_at = dt.datetime(
            int(jdate[:4]), int(jdate[4:6]), int(jdate[6:8]), tzinfo=dt.timezone.utc)

    # 用 CaseNumber.canonical() 而非自行組字串——這正是 Event.case_numbers
    # 與 identifier_match() 比對時採用的正規形式，兩處各自組字串遲早會
    # 因為格式drift（如全形／半形數字）而比對失敗。
    case_number = CaseNumber(
        year=int(case["JYEAR"]), category=category, number=int(case["JNO"]),
    ).canonical()
    court_title = f"{title}（{case_number}）" if title else case_number

    return ParsedDocument(
        url=url, title=court_title, body=case.get("JFULL", ""),
        published_at=published_at, external_id=case.get("JID", ""),
        extra={"case_number": case_number},
    )
