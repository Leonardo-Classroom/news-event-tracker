"""措辭檢查器測試，含 M6 驗收（未定案違規表述攔截率 ≥95%）。

違規句取材自真實語料的用語模式（見 apps/compliance/wording.py 的設計
說明），但改寫為「假設案件尚未定讞」的情境——真實標題本身多半陳述的
是已定讞案件，不能直接當違規範例用。
"""
import pytest

from apps.compliance.wording import (
    CONDITIONAL_TERMS, PROCEDURAL_SAFE_TERMS, UNCONDITIONAL_VIOLATION_TERMS,
    check_wording,
)

#: (文字, 是否應通過)。取自真實新聞用語模式，見任務 38 設計紀錄。
CASES: list[tuple[str, bool]] = [
    # ── 無條件違規：未定讞時使用終結性用語
    ("柯文哲涉貪案定讞，處有期徒刑8年", False),
    ("沈慶京收賄案判刑確定", False),
    ("京華城案主謀已伏法", False),
    ("應曉薇貪污犯身分確立", False),
    ("被告身陷囹圄，難逃法律制裁", False),
    ("該詐欺犯已入獄服刑", False),
    ("圖利罪證確鑿，法界人士咸認", False),
    ("這起弊案的詐騙犯終於鋃鐺入獄", False),
    ("涉案人已入獄，社會譁然", False),
    ("貪污犯遭起底，議員痛批", False),
    # ── 條件式：無法院動作標記
    ("沈慶京有罪，社會觀感不佳", False),
    ("應曉薇定罪，議員連署要求下台", False),
    ("柯文哲服刑，支持者難以接受", False),
    ("黃景茂有罪，工程界一片譁然", False),
    # ── 通過：程序用語
    ("柯文哲遭起訴涉嫌圖利", True),
    ("沈慶京被控收賄，檢方複訊後聲押", True),
    ("應曉薇涉嫌收賄，遭羈押禁見", True),
    ("北檢傳喚證人到案說明", True),
    ("該案一審判決被告有期徒刑8年，可上訴", True),
    ("被告經法院裁定交保", True),
    ("網紅認罪，一審判決出爐", True),
    ("被告不服上訴，案件進入二審", True),
    ("檢方複訊後諭知限制出境", True),
    ("該案更審後仍在審理中", True),
    # ── 通過：條件式用語搭配法院動作標記
    ("法院一審判決被告有罪，處有期徒刑8年", True),
    ("台北地院依法判決柯文哲有罪", True),
    ("更一審宣判，法院認定被告有罪", True),
    ("裁定被告須入監服刑，尚未確定", True),
    # ── 通過：is_final=True 時終結性用語為真實敘述
    ("台灣藍海董事長判10年半定讞", True),
    ("該案判刑確定，被告已入獄服刑", True),
    ("三體宇宙前執行長伏法，執行死刑", True),
    # ── 邊界：不含任何關鍵詞的中性敘述
    ("台北地檢署今偵結京華城容積案", True),
    ("監察院糾正新竹市府，指驗收把關不周", True),
]


class TestCheckWording:
    @pytest.mark.parametrize("text,expected_pass", [c for c in CASES if not c[1]])
    def test_攔截違規表述(self, text, expected_pass):
        result = check_wording(text, is_final=False)
        assert result.passed == expected_pass, f"應攔截但通過了：{text}"

    @pytest.mark.parametrize("text,expected_pass", [c for c in CASES if c[1]])
    def test_放行合規表述(self, text, expected_pass):
        # 通過案例中，含「定讞／判刑確定／伏法」者本質是已定讞案件的
        # 真實敘述，需搭配 is_final=True；其餘案例本身在任何狀態下都合規
        is_final = any(t in text for t in ("定讞", "判刑確定", "伏法"))
        result = check_wording(text, is_final=is_final)
        assert result.passed == expected_pass, (
            f"應放行但被攔截了：{text}\n"
            f"理由：{[v.reason for v in result.violations]}"
        )

    def test_M6驗收_未定案違規表述攔截率(self):
        """規格 M6：以人工標註測試集驗證，未定案個人的違規表述攔截率 ≥95%。"""
        violations = [c for c in CASES if not c[1]]
        caught = sum(1 for text, _ in violations if not check_wording(text, is_final=False))
        rate = caught / len(violations)
        assert rate >= 0.95, f"攔截率 {rate:.0%}，未達規格 M6 的 95%"

    def test_is_final為真時一律放行(self):
        """已定讞案件使用終結性用語是陳述事實，不應攔截。"""
        assert check_wording("被告判刑確定，已入獄服刑", is_final=True).passed

    def test_預設值保守(self):
        """不知道是否定讞時，預設 is_final=False——寧可誤擋不可誤放。"""
        result = check_wording("柯文哲貪污犯定讞")
        assert not result.passed

    def test_攔截結果附帶原因與位置(self):
        result = check_wording("沈慶京貪污犯身分確立", is_final=False)
        assert not result.passed
        v = result.violations[0]
        assert v.term == "貪污犯"
        assert "沈慶京" in v.context
        assert v.reason

    def test_認罪不是違規詞(self):
        """認罪是被告在法庭上的正式程序行為，是陳述事實不是定罪斷定。"""
        assert "認罪" not in UNCONDITIONAL_VIOLATION_TERMS
        assert "認罪" not in CONDITIONAL_TERMS
        assert check_wording("被告認罪，一審判決出爐", is_final=False).passed

    def test_安全詞表與違規詞表不重疊(self):
        assert not set(PROCEDURAL_SAFE_TERMS) & set(UNCONDITIONAL_VIOLATION_TERMS)
        assert not set(PROCEDURAL_SAFE_TERMS) & set(CONDITIONAL_TERMS)

    def test_多重違規全數列出而非只回報第一個(self):
        result = check_wording("柯文哲貪污犯，沈慶京詐欺犯，兩人皆已定讞",
                               is_final=False)
        terms = {v.term for v in result.violations}
        assert terms == {"貪污犯", "詐欺犯", "定讞"}
