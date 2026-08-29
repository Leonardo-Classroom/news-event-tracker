"""來源健康度報表的測試。

這支指令是「連續失敗計數」的查詢介面。那個數字若沒有人看等於不存在——
fixture 測試偵測不到網站改版，只有生產環境的失敗計數能發現。
"""
import datetime as dt
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.ingest.models import Source
from apps.ingest.services import FAILURE_THRESHOLD

pytestmark = pytest.mark.medium


def run() -> tuple[str, int]:
    out = StringIO()
    try:
        call_command("source_health", stdout=out)
        return out.getvalue(), 0
    except SystemExit as exc:
        return out.getvalue(), exc.code


class TestSourceHealth:
    def test_健康來源以零碼結束(self, source):
        source.last_success_at = timezone.now()
        source.save(update_fields=["last_success_at"])
        text, code = run()
        assert code == 0
        assert "全部" in text

    def test_連續失敗達門檻時以非零碼結束(self, source):
        """可直接接到監測系統——非零結束碼即為告警訊號。"""
        source.last_success_at = timezone.now()
        source.consecutive_failures = FAILURE_THRESHOLD
        source.save(update_fields=["last_success_at", "consecutive_failures"])
        text, code = run()
        assert code == 1
        assert "連續失敗" in text
        assert "已降頻" in text

    def test_從未成功者被標記(self, source):
        assert source.last_success_at is None
        text, code = run()
        assert code == 1
        assert "從未成功" in text

    def test_長時間未成功者被標記(self, source):
        """抓出「沒有累計失敗、但也一直沒有新內容」的靜默失效。"""
        source.poll_interval_minutes = 20
        source.last_success_at = timezone.now() - dt.timedelta(hours=5)
        source.save(update_fields=["poll_interval_minutes", "last_success_at"])
        text, code = run()
        assert code == 1
        assert "未成功" in text

    def test_停用的來源不納入(self, source):
        source.enabled = False
        source.save(update_fields=["enabled"])
        text, code = run()
        assert code == 0
        assert source.slug not in text
