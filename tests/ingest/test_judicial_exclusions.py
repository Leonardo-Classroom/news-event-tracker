"""案件類型排除測試（任務 35，規格 §4.6）。"""
from apps.ingest.judicial_exclusions import is_excluded_case


class TestIsExcludedCase:
    def test_一般民事案件不排除(self):
        assert not is_excluded_case(case_title="損害賠償(交通)", case_category="訴")

    def test_一般家事案件不排除(self):
        """多數家事案件（繼承、分割遺產）本來就公開，
        不該因為『家事』兩個字就整類排除。"""
        assert not is_excluded_case(case_title="分割遺產", case_category="家繼")
        assert not is_excluded_case(case_title="酌定扶養費", case_category="家親")

    def test_保護令排除(self):
        assert is_excluded_case(case_title="聲請核發通常保護令", case_category="家護")
        assert is_excluded_case(case_title="聲請暫時保護令", case_category="家護")

    def test_少年事件排除(self):
        assert is_excluded_case(case_title="少年竊盜保護事件", case_category="少調")

    def test_性侵害案件排除(self):
        assert is_excluded_case(case_title="妨害性自主等", case_category="訴")
        assert is_excluded_case(case_title="性騷擾防治法", case_category="訴")

    def test_家庭暴力案件排除(self):
        assert is_excluded_case(case_title="家庭暴力傷害案件", case_category="訴")

    def test_僅字別命中也排除(self):
        """有些案由文字本身不明顯，但字別代碼已標示案件類型。"""
        assert is_excluded_case(case_title="", case_category="少調")

    def test_空白輸入不排除(self):
        assert not is_excluded_case()
