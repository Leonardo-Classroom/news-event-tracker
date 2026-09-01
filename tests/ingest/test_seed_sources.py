"""``seed_sources`` 指令的測試。"""
import pytest
from django.core.management import call_command

from apps.ingest.models import Source, SourceType


@pytest.mark.medium
class TestSeedSources:
    def test_重跑不會覆蓋手動改過的enabled狀態(self, db):
        """2026-09-01 踩過的坑：``enabled`` 若放在每次都套用的
        ``defaults`` 裡，重跑這個號稱冪等的指令會把手動啟用的來源
        （聯合、中時）打回預設的停用狀態。"""
        call_command("seed_sources")
        Source.objects.filter(slug="udn").update(enabled=True)

        call_command("seed_sources")

        assert Source.objects.get(slug="udn").enabled is True

    def test_預設不啟用需playwright的來源(self, db):
        call_command("seed_sources")
        assert Source.objects.get(slug="chinatimes").enabled is False

    def test_有archive_spec的來源沒有feed_url(self, db):
        """網址單一真相留在 ARCHIVE_SPECS，避免這裡和那邊各存一份。"""
        call_command("seed_sources")
        source = Source.objects.get(slug="ltn")
        assert source.type == SourceType.NEWS_SCRAPE
        assert source.feed_url == ""

    def test_鏡週刊改走sitemap(self, db):
        """曾被判定為「無可用路徑」（SPA，/api/v2/posts 逾時），
        2026-09-01 實測 sitemap 純 HTTP 可取且比 RSS 多兩個數量級。"""
        call_command("seed_sources")
        source = Source.objects.get(slug="mirrormedia")
        assert source.type == SourceType.NEWS_SCRAPE
        assert source.feed_url == ""
