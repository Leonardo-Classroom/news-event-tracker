"""回填既有爬蟲的歷史語料（Scope 1 任務 12）。

來源為 ``news report/dataset/``，既有爬蟲以「每記者一個目錄」的結構
存成 txt。檔案含結構化表頭（標題、記者、新聞網、日期、網址），
其中**網址**正好能接上 ``Document.url`` 的唯一約束，使回填天然冪等。

**不匯入時尚三站**（GQ、VOGUE、COOL-STYLE，共 87,771 篇）。
ADR-0009 的「未通過過濾者仍入庫但不處理」是針對日常管線——事前無法
得知單篇文件的相關性。一次性回填一批**已知全數無關**的語料則不同：
匯入只會多佔空間、拖慢每次查詢，且沒有任何情境會用到它們。

**效能**：實測掃描可忽略、讀取約 30 分鐘、衍生欄位計算約 14 分鐘
（25 萬篇，9P 檔案系統）。因此設計為可續跑的批次作業，
中斷後重跑會跳過已匯入者，而非從頭來過。
"""
from __future__ import annotations

import datetime as dt
import os
import re
import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.text.bigram import to_tsvector_input
from apps.core.text.simhash import simhash
from apps.ingest.models import ContentClass, Document, Source, SourceType, to_signed64

DATASET_ROOT = "/mnt/d/OneDrive/Desktop/Alan/news report/dataset"

#: 目錄名 → 來源 slug。時尚三站刻意不列入。
SITE_TO_SLUG = {
    "聯合新聞網": "udn",
    "中時新聞網": "chinatimes",
    "工商時報": "ctee",
}

_HEADER_SEPARATOR = "-" * 40

#: 內文中的訂閱推廣文字。UDN 的文章尾端會夾帶付費牆宣傳，
#: 那不是報導內容，留著會汙染去重與抽取。
_PROMO_MARKERS = (
    "你今年最好的選擇",
    "聯合報每天報版內容",
    "付費訂戶專屬深度報導",
    "《紐約時報》數位內容訂戶專屬權益",
)


def parse_file(text: str) -> dict | None:
    """解析既有爬蟲的 txt 格式。缺必要欄位時回傳 ``None``。"""
    parts = text.split(_HEADER_SEPARATOR, 1)
    if len(parts) != 2:
        return None
    header_text, body = parts

    header = {}
    for line in header_text.splitlines():
        if "：" in line:
            key, _, value = line.partition("：")
            header[key.strip()] = value.strip()

    url = header.get("網址", "")
    title = header.get("標題", "")
    if not url or not title:
        return None

    published = None
    raw_date = header.get("日期", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        # 既有爬蟲只保留到日期精度，時間資訊已不可考。
        # 以當日正午為代表值，而非午夜——午夜會讓這批文件在依時間
        # 排序時系統性地排在同日新抓取者之前（曾因此誤判轉載方向）。
        published = dt.datetime.strptime(raw_date, "%Y-%m-%d").replace(
            hour=12, tzinfo=dt.timezone.utc)

    for marker in _PROMO_MARKERS:
        index = body.find(marker)
        if index > 0:
            body = body[:index]

    return {
        "url": url.split("?")[0],
        "title": title[:512],
        "author": header.get("記者", "")[:128],
        "published_at": published,
        "raw_body": body.strip(),
    }


def iter_files(site_dir: str):
    """串流走訪。**不可用 glob 或 list(iterdir())**——
    在 9P 檔案系統上一次 materialize 數千個項目會停滯數十分鐘。"""
    for reporter in os.scandir(site_dir):
        if not reporter.is_dir():
            continue
        for entry in os.scandir(reporter.path):
            if entry.name.endswith(".txt"):
                yield entry.path


class Command(BaseCommand):
    help = "回填 news report/dataset 的歷史語料（冪等、可續跑）"

    def add_arguments(self, parser):
        parser.add_argument("--site", default="", help="只處理指定站台目錄")
        parser.add_argument("--limit", type=int, default=0, help="最多處理幾篇（0 為不限）")
        parser.add_argument("--batch", type=int, default=500)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        sites = ([options["site"]] if options["site"]
                 else list(SITE_TO_SLUG))
        started = time.time()
        totals = {"scanned": 0, "parsed": 0, "created": 0, "skipped": 0, "bad": 0}

        for site in sites:
            slug = SITE_TO_SLUG.get(site)
            if not slug:
                self.stderr.write(f"未知站台：{site}（已知：{', '.join(SITE_TO_SLUG)}）")
                continue
            site_dir = os.path.join(DATASET_ROOT, site)
            if not os.path.isdir(site_dir):
                self.stderr.write(f"目錄不存在：{site_dir}")
                continue

            source = self._get_source(slug, site)
            self.stdout.write(f"\n=== {site} → {slug} ===")
            self._backfill_site(site_dir, source, options, totals, started)

        elapsed = time.time() - started
        self.stdout.write(self.style.SUCCESS(
            f"\n完成：掃描 {totals['scanned']:,}、解析 {totals['parsed']:,}、"
            f"新增 {totals['created']:,}、已存在 {totals['skipped']:,}、"
            f"格式不符 {totals['bad']:,}（{elapsed/60:.1f} 分鐘）"
        ))

    # ---------------------------------------------------------------- 內部

    def _get_source(self, slug: str, site: str) -> Source:
        source, _ = Source.objects.get_or_create(
            slug=slug,
            defaults={
                "name": site,
                "type": SourceType.NEWS_SCRAPE,
                "base_url": f"https://{slug}.example",   # 回填來源無需真實網址
                "content_class": ContentClass.COPYRIGHTED,
                "enabled": False,
            },
        )
        return source

    def _backfill_site(self, site_dir, source, options, totals, started):
        batch_size, limit, dry_run = options["batch"], options["limit"], options["dry_run"]
        batch: list[dict] = []
        now = timezone.now()

        for path in iter_files(site_dir):
            totals["scanned"] += 1
            if limit and totals["parsed"] >= limit:
                break

            try:
                with open(path, encoding="utf-8", errors="ignore") as handle:
                    parsed = parse_file(handle.read())
            except OSError:
                totals["bad"] += 1
                continue

            if not parsed or len(parsed["raw_body"]) < 30:
                totals["bad"] += 1
                continue

            totals["parsed"] += 1
            batch.append(parsed)

            if len(batch) >= batch_size:
                self._flush(batch, source, now, totals, dry_run)
                batch = []
                self._progress(totals, started)

        if batch:
            self._flush(batch, source, now, totals, dry_run)
        self._progress(totals, started)

    def _flush(self, batch, source, now, totals, dry_run):
        urls = [item["url"] for item in batch]
        existing = set(
            Document.objects.filter(url__in=urls).values_list("url", flat=True)
        )
        fresh = [item for item in batch if item["url"] not in existing]
        totals["skipped"] += len(batch) - len(fresh)

        if not fresh:
            return
        if dry_run:
            totals["created"] += len(fresh)
            return

        documents = []
        for item in fresh:
            combined = f"{item['title']}\n{item['raw_body']}"
            documents.append(Document(
                source=source,
                url=item["url"],
                title=item["title"],
                author=item["author"],
                published_at=item["published_at"],
                fetched_at=now,
                raw_body=item["raw_body"],
                content_class=source.content_class,
                # bulk_create 不會呼叫 save()，衍生欄位必須自行計算
                simhash=to_signed64(simhash(combined)),
                search_text=to_tsvector_input(combined),
            ))

        # bulk_create(ignore_conflicts=True) 會回傳全部物件，包含因衝突而
        # 未實際寫入者，因此不能用回傳長度當新增數——那會高估。
        # 語料內部本身就有重複網址（同一篇被歸在多位記者名下）。
        with transaction.atomic():
            before = Document.objects.filter(source=source).count()
            Document.objects.bulk_create(documents, ignore_conflicts=True)
            after = Document.objects.filter(source=source).count()
        totals["created"] += after - before
        totals["skipped"] += len(fresh) - (after - before)

    def _progress(self, totals, started):
        elapsed = time.time() - started
        rate = totals["parsed"] / elapsed if elapsed else 0
        self.stdout.write(
            f"  掃描 {totals['scanned']:>7,}  新增 {totals['created']:>7,}  "
            f"已存在 {totals['skipped']:>6,}  不符 {totals['bad']:>5,}  "
            f"{rate:>5.0f} 篇/秒  {elapsed/60:>5.1f} 分",
            ending="\r" if totals["scanned"] % 5000 else "\n",
        )
