"""pytest 共用設定。

medium 測試需要真實 PostgreSQL——pgvector、GeneratedField、GIN 索引
在 SQLite 上都不存在，因此無法以輕量替身取代（見 ADR-0010）。
"""
import os

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()


@pytest.fixture
def source(db):
    from apps.ingest.models import ContentClass, Source, SourceType

    return Source.objects.create(
        slug="test-news",
        name="測試新聞網",
        type=SourceType.NEWS_RSS,
        base_url="https://example.test",
        content_class=ContentClass.COPYRIGHTED,
    )


@pytest.fixture
def official_source(db):
    from apps.ingest.models import ContentClass, Source, SourceType

    return Source.objects.create(
        slug="judicial",
        name="司法院裁判書",
        type=SourceType.JUDICIAL_API,
        base_url="https://opendata.judicial.gov.tw",
        content_class=ContentClass.PUBLIC_RECORD,
        service_window_start_hour=0,
        service_window_end_hour=6,
    )
