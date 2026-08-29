"""事件本體與生命週期（規格 §4.2、§4.4，ADR-0005）。

**事件是資料庫的一等公民，不是查詢時臨時湊出來的。** 這是規格 §4.2 的
三個決定性設計選擇之一：事件有自己的 ID、狀態、時間線與實體集合。
若只做「大向量庫 + 問答」，系統將無從知道某事件是否仍在進行、
上次進展是何時、是否該喚醒——那會得到新聞搜尋引擎，而非事件追蹤器。

生命週期的關鍵約束（規格 §4.4）：

    active   審核通過       新聞每日   官方源每日
    dormant  連續 30 日無新進展  新聞每週   官方源**維持每日**
    closed   人工標記確定     停止      每季（防翻案／再審）

**沉寂事件的官方源檢查頻率刻意不降低。** 新聞沉寂期正是判決出爐的時期；
降低官方源頻率等於放棄本系統的核心功能。
"""
from __future__ import annotations

import datetime as dt

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class EventStatus(models.TextChoices):
    CANDIDATE = "candidate", "候選（自動偵測產生）"
    DRAFT = "draft", "草稿（待審核）"
    ACTIVE = "active", "追蹤中"
    DORMANT = "dormant", "沉寂（新聞停報，官方源續查）"
    CLOSED = "closed", "已結案"
    REJECTED = "rejected", "審核駁回"


class EventVisibility(models.TextChoices):
    PRIVATE = "private", "未公開"
    PUBLIC = "public", "已公開"


class EventDomain(models.TextChoices):
    JUDICIAL = "judicial", "司法案件"
    CORRUPTION = "corruption", "政商弊案"
    GENERIC = "generic", "其他"


class RiskTier(models.TextChoices):
    """依**法律風險**分級，非事件重要性（規格 §4.3.1）。

    人工審核以形式確認為主，因此安全性由自動閘門承擔，
    而分級決定哪些必須逐條確認。
    """

    HIGH = "high", "高（具名自然人且未判決確定）"
    MEDIUM = "medium", "中（法人／機關，或已判決確定）"
    LOW = "low", "低（無指名對象）"


#: 合法的狀態轉換。以明確的白名單表達，而非在程式中散落 if 判斷——
#: 狀態機的正確性直接影響「事件是否被持續追蹤」，必須可一眼看完。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    EventStatus.CANDIDATE: {EventStatus.DRAFT, EventStatus.REJECTED},
    EventStatus.DRAFT: {EventStatus.ACTIVE, EventStatus.REJECTED},
    # active 可直接結案（如判決確定），也可因新聞停報而沉寂
    EventStatus.ACTIVE: {EventStatus.DORMANT, EventStatus.CLOSED},
    # dormant 可被官方進展喚醒——這是本系統的核心行為
    EventStatus.DORMANT: {EventStatus.ACTIVE, EventStatus.CLOSED},
    # closed 仍可因翻案或再審而重啟
    EventStatus.CLOSED: {EventStatus.ACTIVE},
    EventStatus.REJECTED: set(),
}

#: 沉寂判定門檻（日）。做成設定值而非常數，需依實際新聞週期校準。
DORMANT_AFTER_DAYS = 30


class InvalidTransition(ValidationError):
    """不合法的狀態轉換。

    刻意拋錯而非靜默忽略：狀態被錯誤地改動不會有外顯徵狀，
    但事件會停止被追蹤——那正是本系統要防止的事。
    """


class WordingGateFailed(InvalidTransition):
    """措辭檢查未通過，無法進入 draft 或發布（任務 39）。

    獨立於 ``InvalidTransition`` 命名（雖繼承自它，行為一致），
    讓呼叫端可以區分「流程順序錯了」與「內容違反無罪推定原則」——
    兩者給使用者看的訊息與該做的事完全不同：前者是操作錯誤，
    後者是要求回去修改文字。
    """


class EventQuerySet(models.QuerySet):
    def tracking(self):
        """仍在追蹤中者（active 或 dormant）。"""
        return self.filter(status__in=[EventStatus.ACTIVE, EventStatus.DORMANT])

    def public(self):
        return self.filter(visibility=EventVisibility.PUBLIC)

    def awaiting_review(self):
        return self.filter(status=EventStatus.DRAFT)

    def maybe_due_for_official_check(self, now=None):
        """SQL 端的**寬鬆**預篩——只用最短的檢查間隔（1 天）排除掉明顯
        還沒到期的事件，精確判斷（依狀態各自的間隔）交給
        ``Event.due_for_official_check()`` 在 Python 端做第二輪過濾。

        分兩階段是因為精確間隔依狀態而異（active/dormant 官方源皆
        1 天、closed 90 天），若要在單一 SQL 查詢裡表達完整規則，
        條件會變得難以驗證是否正確；而事件數量級（數十到數百）
        遠不到需要在資料庫端做到位的規模。
        """
        now = now or timezone.now()
        return self.filter(
            status__in=[EventStatus.ACTIVE, EventStatus.DORMANT, EventStatus.CLOSED],
        ).filter(
            models.Q(last_official_check_at__isnull=True)
            | models.Q(last_official_check_at__lte=now - dt.timedelta(days=1))
        )


class Event(models.Model):
    """一個被追蹤的事件。"""

    slug = models.SlugField(max_length=128, unique=True, allow_unicode=True)
    title = models.CharField(max_length=256)
    summary = models.TextField(blank=True, help_text="系統自製摘要，非新聞原文")

    #: 首屏要顯示的「目前進度」（規格 G1）。以文字保存而非即時計算——
    #: 它是經審核的對外敘述，不該隨資料變動而改變措辭。
    current_status_text = models.CharField(max_length=256, blank=True)

    status = models.CharField(
        max_length=16, choices=EventStatus.choices,
        default=EventStatus.CANDIDATE, db_index=True,
    )
    visibility = models.CharField(
        max_length=16, choices=EventVisibility.choices,
        default=EventVisibility.PRIVATE, db_index=True,
    )
    domain = models.CharField(
        max_length=16, choices=EventDomain.choices, default=EventDomain.GENERIC,
    )
    risk_tier = models.CharField(
        max_length=8, choices=RiskTier.choices, default=RiskTier.MEDIUM,
        help_text="由抽取結果自動判定，不由人工指定（規格 §4.3.1）",
    )

    #: 事件識別的核心實體。標註測試集實測顯示**單一關鍵字不足以界定事件**——
    #: 「大巨蛋」會帶進場館的所有體育新聞、「南方澳」會帶進當地的廟宇竊案。
    #: 事件需要更具體的識別依據：當事人、機關、案號。
    core_terms = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    case_numbers = ArrayField(models.CharField(max_length=64), default=list, blank=True)

    #: 最後一次有新進展的時間。沉寂判定與排程皆以此為準，
    #: 而非 updated_at——後者會被任何欄位修改觸發。
    last_progress_at = models.DateTimeField(null=True, blank=True, db_index=True)
    next_key_date = models.DateField(null=True, blank=True,
                                     help_text="下次開庭或關鍵期日")

    #: 上次「嘗試檢查」官方源的時間，與 last_progress_at 分開記錄——
    #: 後者只在真的有新進展時更新，前者每次檢查（不論有無新進展）
    #: 都要更新，否則「今天已經查過但沒有新東西」會被誤判為還沒查過，
    #: 導致同一天對官方源重複發出請求（任務 27）。
    last_official_check_at = models.DateTimeField(null=True, blank=True)

    first_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["-last_progress_at", "-created_at"]
        indexes = [
            models.Index(fields=["status", "-last_progress_at"], name="event_status_prog"),
            models.Index(fields=["visibility", "status"], name="event_vis_status"),
        ]
        verbose_name = "事件"
        verbose_name_plural = "事件"

    def __str__(self) -> str:
        return f"{self.title}（{self.get_status_display()}）"

    # ---------------------------------------------------------------- 狀態機

    def can_transition_to(self, target: str) -> bool:
        return target in ALLOWED_TRANSITIONS.get(self.status, set())

    def transition_to(self, target: str, *, save: bool = True) -> None:
        """變更狀態。不合法時拋 ``InvalidTransition``。

        公開（visibility）與狀態分開處理：事件可以是 active 但未公開
        （尚未審核發布），也可以已公開而後轉為 dormant。

        **進入 draft 前強制過措辭檢查器，不提供繞過參數**（任務 39，
        規格 §4.5）。這裡不是「建議先檢查」，是唯一路徑——呼叫端沒有
        辦法傳入任何參數跳過這道檢查，要通過就只能改文字重新呼叫。
        """
        if target == self.status:
            return
        if not self.can_transition_to(target):
            raise InvalidTransition(
                f"不可由 {self.status} 轉為 {target}。"
                f"允許的目標：{sorted(ALLOWED_TRANSITIONS.get(self.status, set())) or '無'}"
            )
        if target == EventStatus.DRAFT:
            self._enforce_wording_gate()
        self.status = target
        if save:
            self.save(update_fields=["status", "updated_at"])

    def _enforce_wording_gate(self) -> None:
        from apps.compliance.wording import check_wording

        # risk_tier == HIGH 正是「具名自然人且未判決確定」——與措辭檢查器
        # 的 is_final 判準是同一件事的兩種呈現，因此可以直接借用
        # （見 apps.compliance.risk 的設計說明）。
        is_final = self.risk_tier != RiskTier.HIGH
        for field_name, text in (("summary", self.summary),
                                 ("current_status_text", self.current_status_text)):
            if not text:
                continue
            result = check_wording(text, is_final=is_final)
            if not result.passed:
                reasons = "；".join(v.reason for v in result.violations)
                raise WordingGateFailed(
                    f"措辭檢查未通過，無法進入 draft（欄位：{field_name}）：{reasons}"
                )

    def recompute_risk_tier(self, *, save: bool = True) -> str:
        """依掛載文件的抽取結果重新判定風險分級（任務 40）。

        由抽取結果決定、不由人工指定——見 ``apps.compliance.risk`` 的
        設計說明。應在每次歸屬新文件後呼叫。
        """
        from apps.compliance.risk import determine_risk_tier_for_event

        self.risk_tier = determine_risk_tier_for_event(self)
        if save:
            self.save(update_fields=["risk_tier", "updated_at"])
        return self.risk_tier

    @property
    def allows_batch_review(self) -> bool:
        """高風險事件禁止批次通過，必須逐條確認（規格 §4.3.1）。"""
        return self.risk_tier != RiskTier.HIGH

    def publish(self, *, save: bool = True) -> None:
        """發布。僅 active 事件可公開——draft 尚未經審核，
        candidate 更是自動產生未經人看過。

        再過一次措辭檢查器，作為進入 draft 時那道檢查之外的第二層——
        summary／current_status_text 可能在 draft 之後、發布之前又被
        編輯過，不能只信任進入 draft 時的那一次結果。
        """
        if self.status != EventStatus.ACTIVE:
            raise InvalidTransition(f"僅 active 事件可公開，目前為 {self.status}")
        self._enforce_wording_gate()
        self.visibility = EventVisibility.PUBLIC
        if save:
            self.save(update_fields=["visibility", "updated_at"])

    def unpublish(self, *, save: bool = True) -> None:
        """撤下公開。更正／下架申訴須能立即生效（規格 §8.4，24 小時 SLA），
        因此不設任何狀態前提。"""
        self.visibility = EventVisibility.PRIVATE
        if save:
            self.save(update_fields=["visibility", "updated_at"])

    # ---------------------------------------------------------------- 生命週期

    def days_since_progress(self, now: dt.datetime | None = None) -> int | None:
        if self.last_progress_at is None:
            return None
        return (( now or timezone.now()) - self.last_progress_at).days

    def should_become_dormant(self, now: dt.datetime | None = None,
                              threshold_days: int = DORMANT_AFTER_DAYS) -> bool:
        if self.status != EventStatus.ACTIVE:
            return False
        elapsed = self.days_since_progress(now)
        return elapsed is not None and elapsed >= threshold_days

    def record_progress(self, moment: dt.datetime, *, save: bool = True) -> bool:
        """記錄新進展，必要時自動喚醒。

        回傳是否發生喚醒。**這是本系統的核心行為**：沉寂事件因官方紀錄
        出現新進展而重新活躍，正是「新聞停了，追蹤沒停」的具體實現。
        """
        woke = False
        if self.last_progress_at is None or moment > self.last_progress_at:
            self.last_progress_at = moment
        if self.status == EventStatus.DORMANT:
            self.status = EventStatus.ACTIVE
            woke = True
        if save:
            self.save(update_fields=["last_progress_at", "status", "updated_at"])
        return woke

    # ---------------------------------------------------------------- 排程

    def check_interval_days(self, *, official_source: bool) -> int | None:
        """該以多少天為間隔檢查此事件。``None`` 表示不再檢查。

        **沉寂事件的官方源頻率刻意不降低**（規格 §4.4）：
        新聞沉寂期正是判決出爐的時期，降低頻率等於放棄核心功能。
        """
        if self.status == EventStatus.ACTIVE:
            return 1
        if self.status == EventStatus.DORMANT:
            return 1 if official_source else 7
        if self.status == EventStatus.CLOSED:
            return 90 if official_source else None
        return None

    def due_for_official_check(self, now: dt.datetime | None = None) -> bool:
        """是否該對此事件的官方源做一次檢查（任務 27）。

        用 ``last_official_check_at`` 而非 ``last_progress_at`` 判斷
        「上次查過是什麼時候」——兩者意義不同：檢查了但沒查到新東西，
        ``last_progress_at`` 不會動，若拿它當「上次檢查時間」的依據，
        會被誤判成「一直沒查過」而每次排程都重新發request。
        """
        interval = self.check_interval_days(official_source=True)
        if interval is None:
            return False
        if self.last_official_check_at is None:
            return True
        now = now or timezone.now()
        return (now - self.last_official_check_at).days >= interval

    def record_official_check(self, moment: dt.datetime, *, save: bool = True) -> None:
        self.last_official_check_at = moment
        if save:
            self.save(update_fields=["last_official_check_at", "updated_at"])


class EventAlias(models.Model):
    """事件的別名與俗稱。

    同時服務兩件事：檢索（讀者用俗稱搜尋）與 SEO（規格 §4.7——
    大眾想起某案件時的行為是 Google 案件關鍵字，
    因此事件頁的搜尋能見度是本產品觸及使用者的主要管道）。
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="aliases")
    name = models.CharField(max_length=128, db_index=True)
    #: 曾使用過的 slug。事件合併或更名後舊網址須 301 導向，
    #: 否則既有的搜尋排名與外部連結會全部失效。
    is_former_slug = models.BooleanField(default=False)

    class Meta:
        unique_together = [("event", "name")]
        verbose_name = "事件別名"
        verbose_name_plural = "事件別名"

    def __str__(self) -> str:
        return self.name


class AssignmentMethod(models.TextChoices):
    IDENTIFIER = "identifier", "識別碼精確比對"
    LLM = "llm", "LLM 判定"
    MANUAL = "manual", "人工指定"
    IMPORT = "import", "標註集匯入"
    CLUSTER = "cluster", "自動叢集（新事件候選）"


class EventDocument(models.Model):
    """事件與文件的關聯。

    記錄判定方法與理由，而非只存一個布林值——歸屬錯誤是事件品質的
    主要風險，事後追查時需要知道「這篇為什麼被歸進來」。
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE,
                              related_name="event_documents")
    document = models.ForeignKey("ingest.Document", on_delete=models.CASCADE,
                                 related_name="event_documents")

    relevance_score = models.FloatField(default=0.0)
    method = models.CharField(max_length=16, choices=AssignmentMethod.choices)
    reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("event", "document")]
        indexes = [
            models.Index(fields=["event", "-relevance_score"], name="evdoc_event_score"),
        ]
        verbose_name = "事件文件關聯"
        verbose_name_plural = "事件文件關聯"

    def __str__(self) -> str:
        return f"{self.event_id} ← {self.document_id}（{self.get_method_display()}）"


class EventMergeLog(models.Model):
    """事件合併紀錄（任務 26）。

    歸屬階段的漏判必然產生重複事件——這不是附加功能，是 ADR-0005
    兩階段偵測（先歸屬既有事件、未歸屬者才叢集）下的必然結果。
    合併邏輯見 ``apps.events.merging.merge_events``。

    ``source_event`` 設 ``SET_NULL`` 而非 ``CASCADE``：合併會刪除來源
    事件（見 ``merging`` 模組的說明），若這裡用 CASCADE，來源事件一
    刪除，這筆合併紀錄就跟著消失——那正是最需要留存的稽核資訊。
    ``source_slug``／``source_title`` 因此獨立存一份，不依賴外鍵存活。
    """

    source_event = models.ForeignKey(
        Event, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="merge_logs_as_source",
    )
    source_slug = models.SlugField(max_length=128, allow_unicode=True)
    source_title = models.CharField(max_length=256)
    target_event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="merge_logs_as_target",
    )
    document_count = models.PositiveIntegerField(
        default=0, help_text="合併時由來源事件轉移過去的文件數")
    reason = models.TextField(blank=True)
    merged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-merged_at"]
        verbose_name = "事件合併紀錄"
        verbose_name_plural = "事件合併紀錄"

    def __str__(self) -> str:
        return f"{self.source_slug} → {self.target_event_id}（{self.document_count} 篇）"
