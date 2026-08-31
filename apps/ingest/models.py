"""採集層（L0）的資料模型：來源註冊與原始文件。

`Document` 是系統中所有原始資料的統一型別——新聞、裁判書、議案、
糾正案、決標公告皆存於此表，以 `content_class` 區分著作權狀態。
這個統一是刻意的：事件歸屬與時間線建構不該關心資料來自哪一類來源。
"""
from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from pgvector.django import HalfVectorField, HnswIndex
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import F, Func, Value
from django.utils import timezone

from apps.core.identifiers import extract_case_numbers, extract_tax_ids
from apps.core.text.bigram import to_tsvector_input
from apps.core.text.simhash import simhash

# PostgreSQL 的 bigint 是有號 64 位元，而 SimHash 是無號 64 位元。
# 儲存時轉為有號、讀取時轉回無號，避免溢位。
_INT64_OFFSET = 1 << 64
_INT63 = 1 << 63


def to_signed64(value: int) -> int:
    return value - _INT64_OFFSET if value >= _INT63 else value


def to_unsigned64(value: int) -> int:
    return value + _INT64_OFFSET if value < 0 else value


class SourceType(models.TextChoices):
    NEWS_RSS = "news_rss", "新聞 RSS"
    NEWS_SCRAPE = "news_scrape", "新聞爬蟲"
    JUDICIAL_API = "judicial_api", "司法院裁判書 API"
    LY_API = "ly_api", "立法院開放資料"
    CY_SCRAPE = "cy_scrape", "監察院爬蟲"
    PCC_DATASET = "pcc_dataset", "政府採購網資料集"


class ContentClass(models.TextChoices):
    """決定文件全文能否對外呈現——這是法律約束，不是偏好設定。

    COPYRIGHTED  新聞報導。記者撰寫的內容是受保護的語文著作，
                 全文永不對外呈現，公開頁僅放自製摘要與原文連結。
    PUBLIC_RECORD 公文。依著作權法第 9 條不得為著作權之標的，
                 可全文呈現。這是官方源相對新聞源的實質優勢。
    """

    COPYRIGHTED = "copyrighted", "受著作權保護（僅內部）"
    PUBLIC_RECORD = "public_record", "公文（可公開）"


class Source(models.Model):
    """採集來源的註冊與健康狀態。"""

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    type = models.CharField(max_length=32, choices=SourceType.choices)
    base_url = models.URLField(max_length=512)
    feed_url = models.URLField(max_length=512, blank=True)

    content_class = models.CharField(
        max_length=32,
        choices=ContentClass.choices,
        default=ContentClass.COPYRIGHTED,
        help_text="此來源產出的文件預設歸類；決定全文能否公開",
    )

    enabled = models.BooleanField(default=True)
    poll_interval_minutes = models.PositiveIntegerField(default=60)

    # 部分官方來源有服務時段限制（司法院 API 僅 00:00–06:00 開放）
    service_window_start_hour = models.PositiveSmallIntegerField(null=True, blank=True)
    service_window_end_hour = models.PositiveSmallIntegerField(null=True, blank=True)

    # 健康度監測（ADR-0007）：fixture 測試偵測不到「網站改版了」，
    # 只有生產環境的連續失敗計數能發現。
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    # 歷史回補（任務 61）。與即時輪詢的健康度分開——歷史清單 404
    # 不該讓「立即爬取」被記成連續失敗。
    historical_status = models.CharField(
        max_length=16, default="idle",
        help_text="idle / running / done / error",
    )
    historical_cursor = models.TextField(
        blank=True,
        help_text="回補進度（日期、頁碼或 offset 的 JSON）",
    )
    historical_started_at = models.DateTimeField(null=True, blank=True)
    historical_updated_at = models.DateTimeField(null=True, blank=True)
    historical_finished_at = models.DateTimeField(null=True, blank=True)
    historical_error = models.TextField(blank=True)
    archive_earliest_on_site = models.DateField(
        null=True, blank=True,
        help_text="站台歷史清單實測可回溯的最早日期",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self) -> str:
        return f"{self.name}（{self.get_type_display()}）"

    def is_within_service_window(self, now=None) -> bool:
        """來源是否在可用時段內。未設定時段者一律視為可用。"""
        if self.service_window_start_hour is None or self.service_window_end_hour is None:
            return True
        hour = (now or timezone.now()).astimezone(timezone.get_current_timezone()).hour
        start, end = self.service_window_start_hour, self.service_window_end_hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end   # 跨午夜


class DocumentQuerySet(models.QuerySet):
    def news(self):
        return self.filter(content_class=ContentClass.COPYRIGHTED)

    def relevant(self):
        """通過相關性過濾者。未評估（null）不計入——
        「還沒判斷」不等於「相關」。"""
        return self.filter(relevant=True)

    def pending_relevance(self):
        return self.filter(relevant__isnull=True).exclude(raw_body="")

    def pending_extraction(self, prompt_version: str = ""):
        """已判定相關、但尚未以指定 prompt 版本抽取過的文件。

        以 prompt 版本為條件而非單純「有無抽取結果」——prompt 改動後
        需要重抽，而那時舊結果仍然存在。
        """
        queryset = self.relevant().exclude(raw_body="")
        if prompt_version:
            return queryset.exclude(
                extractions__prompt_version=prompt_version,
                extractions__succeeded=True,
            )
        return queryset.filter(extractions__isnull=True)

    def public_records(self):
        return self.filter(content_class=ContentClass.PUBLIC_RECORD)

    def canonical(self):
        """排除被歸併為轉載的文件，只留首發版本。"""
        return self.filter(canonical_of__isnull=True)


class Document(models.Model):
    """統一的原始文件。

    冪等性是硬性要求：ADR-0007 的 Celery 任務設 ``acks_late=True``，
    被硬殺的任務會重新入列並重跑，因此所有寫入必須為 upsert 而非 insert。
    ``url`` 的唯一約束是這個保證的基礎，同時也取代了既有爬蟲的
    ``done_urls.json`` 續爬機制（檔案狀態在多 worker 下有競爭條件）。
    """

    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="documents")

    url = models.URLField(max_length=1024, unique=True)
    external_id = models.CharField(
        max_length=128, blank=True,
        help_text="來源自身的識別碼，如裁判書的 JID",
    )

    title = models.CharField(max_length=512)
    raw_body = models.TextField(
        blank=True,
        help_text="原始全文。content_class=copyrighted 時永不對外序列化。",
    )

    content_class = models.CharField(max_length=32, choices=ContentClass.choices)

    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    fetched_at = models.DateTimeField(default=timezone.now, db_index=True)

    author = models.CharField(max_length=128, blank=True)

    # --- 近似去重（SimHash，見 apps.core.text.simhash）---
    simhash = models.BigIntegerField(null=True, blank=True, db_index=True)
    canonical_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reprints",
        help_text="若本文為轉載，指向首發版本",
    )

    # --- 識別碼（ADR-0001）---
    # **以規則抽取，不呼叫 LLM。** 案號與統編有明確格式，
    # 規則抽取既免費又能立刻覆蓋全部語料——不必等 L1 抽取完成。
    # 這一點很重要：ADR-0005 把識別碼精確比對列為事件歸屬的第一優先，
    # 若要等 LLM 抽取才有識別碼，歸屬就得跟著等。
    case_numbers = ArrayField(
        models.CharField(max_length=64), default=list, blank=True,
        help_text="正規化後的裁判書案號，如「111年度金重訴字第123號」",
    )
    tax_ids = ArrayField(
        models.CharField(max_length=8), default=list, blank=True,
        help_text="通過檢查碼驗證的統一編號",
    )

    # --- 相關性過濾（ADR-0009）---
    # null 表示尚未評估。刻意用三態而非布林預設 False——
    # 「還沒判斷」與「判斷為不相關」是兩件事，混為一談會讓
    # 「過濾是否已跑過」變得無法查詢。
    relevant = models.BooleanField(null=True, blank=True, db_index=True)
    relevance_signals = models.JSONField(default=dict, blank=True)

    # --- 向量檢索（ADR-0004、ADR-0011）---
    # halfvec（float16）而非 vector（float32）：儲存與 HNSW 索引記憶體減半，
    # 對召回率的影響可忽略。以 100 萬文件估算，向量本身約 2GB。
    embedding = HalfVectorField(dimensions=1024, null=True, blank=True)
    #: 產生此向量的模型版本。換模型時可辨識哪些需重算，支援漸進式遷移——
    #: 沒有它，換模型就只能全部重算，或含糊地混用兩種向量空間。
    embedding_version = models.CharField(max_length=32, blank=True, db_index=True)

    # --- 關鍵字檢索（ADR-0001：bigram + PostgreSQL 內建 FTS）---
    # 切分在 Python 完成（bigram 邏輯無法以 SQL 乾淨表達），
    # tsvector 轉換與索引交給資料庫。
    search_text = models.TextField(blank=True, editable=False)
    search_vector = models.GeneratedField(
        expression=Func(
            Value("simple"), F("search_text"), function="to_tsvector",
        ),
        output_field=SearchVectorField(),
        db_persist=True,
    )

    objects = DocumentQuerySet.as_manager()

    class Meta:
        ordering = ["-published_at", "-fetched_at"]
        indexes = [
            GinIndex(fields=["search_vector"], name="document_search_gin"),
            # 陣列欄位用 GIN：查「哪些文件含此案號」是事件歸屬的
            # 第一優先路徑，必須是索引查詢而非全表掃描
            GinIndex(fields=["case_numbers"], name="document_case_gin"),
            GinIndex(fields=["tax_ids"], name="document_taxid_gin"),
            # HNSW 而非 IVFFlat：資料持續新增且永不刪除，IVFFlat 的分群
            # 在建索引時固定，新增資料若分布偏移則召回率逐漸衰退、需定期重建；
            # HNSW 支援增量插入且召回率較高（ADR-0004）。
            HnswIndex(
                name="document_embedding_hnsw",
                fields=["embedding"],
                m=16, ef_construction=64,
                opclasses=["halfvec_cosine_ops"],
            ),
            models.Index(fields=["source", "-published_at"], name="document_source_pub"),
            models.Index(fields=["content_class"], name="document_content_class"),
        ]

    def __str__(self) -> str:
        return self.title[:80]

    # ---------------------------------------------------------------- 衍生欄位

    def refresh_derived_fields(self) -> None:
        """重算 SimHash 與 bigram 檢索文字。

        以標題加內文計算，因為轉載常改標題但保留內文，
        兩者一起算能同時涵蓋「改標題轉載」與「同標題不同內文」兩種情況。
        """
        combined = f"{self.title}\n{self.raw_body}"
        self.simhash = to_signed64(simhash(combined))
        self.search_text = to_tsvector_input(combined)
        self.case_numbers = extract_case_numbers(combined)
        self.tax_ids = extract_tax_ids(combined)

    @property
    def simhash_unsigned(self) -> int | None:
        return None if self.simhash is None else to_unsigned64(self.simhash)

    #: 衍生欄位的來源；其中任一變動就必須重算
    DERIVED_SOURCES = frozenset({"title", "raw_body"})
    DERIVED_FIELDS = ("simhash", "search_text", "case_numbers", "tax_ids")

    def save(self, *args, **kwargs):
        if not self.content_class:
            self.content_class = self.source.content_class

        update_fields = kwargs.get("update_fields")

        if update_fields is None:
            # 完整儲存：一律重算
            self.refresh_derived_fields()
        elif self.DERIVED_SOURCES & set(update_fields):
            # 部分儲存且來源欄位有變：重算，並把衍生欄位補進 update_fields，
            # 否則它們只在記憶體中更新、資料庫留著過期值——
            # 這種不一致不會報錯，只會讓去重與檢索靜默失準。
            self.refresh_derived_fields()
            kwargs["update_fields"] = list(dict.fromkeys(
                list(update_fields) + list(self.DERIVED_FIELDS)
            ))
        # 來源欄位未變則不重算，避免無謂的 SimHash 與 bigram 計算

        super().save(*args, **kwargs)


class ExternalSession(models.Model):
    """人工登入後留存的第三方網站 session（如司法院資料開放平台）。

    **為什麼需要這個模型。** 部分官方開放資料需要會員登入才能下載，
    而登入頁掛了 Cloudflare Turnstile——實測自動化瀏覽器（Playwright
    headless）填完帳密後，Turnstile 的隱藏 token 欄位在 15 秒內
    始終是空的，登入無法送出。這不是能靠「換個 headless 參數」解決
    的小問題，是 Turnstile 刻意要擋自動化登入。

    刻意不用自動化手法硬闖（偽裝瀏覽器指紋、無頭偵測規避套件等）——
    即使帳號是使用者自己的，刻意規避防護機制仍是規避，不是單純的
    「模擬瀏覽器操作」。改為：人親自登入一次（Turnstile 對真人瀏覽器
    完全不是問題），把登入後的 session cookie 存起來，後續的下載
    請求重放這個 cookie。cookie 有效期有限，過期需要重新登入。
    """

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    login_url = models.URLField(max_length=512)
    #: 瀏覽器開發者工具「Cookie」標頭的完整字串（分號分隔的
    #: name=value 對），不是單一 cookie 值——登入後的驗證狀態通常
    #: 分散在多個 cookie（框架的驗證票證 + Cloudflare 的 cf_clearance）。
    cookie_header = models.TextField(blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    #: 使用者自述的有效期（小時），用於介面提示「可能已過期」——
    #: 無法從 cookie 本身讀出真實到期時間，這只是保守的顯示用估計值。
    expected_valid_hours = models.PositiveIntegerField(default=24)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "外部平台登入 session"
        verbose_name_plural = "外部平台登入 session"

    def __str__(self) -> str:
        return self.name

    @property
    def is_set(self) -> bool:
        return bool(self.cookie_header.strip())

    @property
    def likely_expired(self) -> bool:
        if not self.captured_at:
            return True
        from django.utils import timezone
        age = timezone.now() - self.captured_at
        return age.total_seconds() > self.expected_valid_hours * 3600
