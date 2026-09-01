"""歷史清單規格與 JSON／HTML 解析的 small 測試。不連網、不碰資料庫。"""
import datetime as dt
import json

from apps.ingest.archives import (
    TAIPEI, Cursor, advance_cursor, apply_archive_date,
    build_request, fingerprint_urls, get_archive_spec, initial_cursor,
    parse_archive_documents, parse_cna_json, parse_ltn_json,
    parse_sitemap_xml, parse_twreporter_json, tagged_post_url,
)
from apps.ingest.adapters import get_adapter
from apps.ingest.adapters.base import ParsedDocument


def test_聯合日檔網址用YYYYMMDD():
    spec = get_archive_spec("udn")
    cursor = Cursor(date=dt.date(2016, 7, 1))
    req = build_request(spec, cursor)
    assert req.url == "https://udn.com/news/archive?date=20160701"
    assert req.body is None


def test_ETtoday日清單網址含連字號日期():
    spec = get_archive_spec("ettoday")
    cursor = Cursor(date=dt.date(2012, 1, 1))
    assert "news-list-2012-01-01-0.htm" in build_request(spec, cursor).url


SITEMAP_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.mirrormedia.mg/story/20260901edi005</loc>
    <lastmod>2026-09-01T11:13:36+08:00</lastmod></url>
  <url><loc>https://www.mirrormedia.mg/story/20260822-194ent-021411</loc>
    <lastmod>2026-08-22T09:00:00+08:00</lastmod></url>
  <url><lastmod>2026-08-01T00:00:00+08:00</lastmod></url>
</urlset>"""


def test_sitemap抽出網址與時間():
    docs = parse_sitemap_xml(SITEMAP_SAMPLE, base_url="https://www.mirrormedia.mg")
    assert len(docs) == 2          # 沒有 <loc> 的那筆要被跳過
    assert docs[0].url == "https://www.mirrormedia.mg/story/20260901edi005"
    assert docs[0].published_at is not None


def test_sitemap沒有標題不亂填():
    """sitemap 只有網址，標題要留白給內頁補齊——硬塞網址片段會污染
    轉載歸併的 bigram 指紋與檢索。"""
    docs = parse_sitemap_xml(SITEMAP_SAMPLE, base_url="https://www.mirrormedia.mg")
    assert all(d.title == "" for d in docs)


def test_鏡週刊改走sitemap不再是空路徑():
    spec = get_archive_spec("mirrormedia")
    assert spec.url_template.endswith("/rss/posts.xml")
    assert spec.list_parser == "sitemap"
    req = build_request(spec, Cursor(page=1))
    assert req.url == "https://www.mirrormedia.mg/rss/posts.xml"


def test_中央社POST帶分類與頁碼():
    spec = get_archive_spec("cna-society")
    req = build_request(spec, Cursor(page=2))
    assert req.body["category"] == "asoc"
    assert req.body["pageidx"] == 2
    assert "pageidx=2" in tagged_post_url(req.url, req.body)


def test_日期走訪往後一天():
    spec = get_archive_spec("udn")
    nxt = advance_cursor(spec, Cursor(date=dt.date(2016, 7, 1)))
    assert nxt.date == dt.date(2016, 7, 2)
    assert nxt.units_done == 1


def test_cursor往返json():
    raw = Cursor(date=dt.date(2016, 7, 1), page=3, first_fp="abc").to_json()
    back = Cursor.from_json(raw)
    assert back.date == dt.date(2016, 7, 1)
    assert back.page == 3
    assert back.first_fp == "abc"


def test_空cursor從站台最早日期起():
    spec = get_archive_spec("udn")
    cursor = initial_cursor(spec)
    assert cursor.date == dt.date(2016, 7, 1)


def test_指紋相同代表繞回():
    a = fingerprint_urls(["https://a.test/1", "https://a.test/2"])
    b = fingerprint_urls(["https://a.test/2", "https://a.test/1"])
    assert a == b
    assert a != fingerprint_urls(["https://a.test/1"])


def test_解析中央社清單JSON():
    raw = json.dumps({
        "Result": "Y",
        "ResultData": {
            "Items": [{
                "Id": "202608300180",
                "HeadLine": "三重男揮刀",
                "PageUrl": "https://www.cna.com.tw/news/asoc/202608300180.aspx",
                "CreateTime": "2026/08/30 22:09",
            }],
        },
    })
    docs = parse_cna_json(raw)
    assert len(docs) == 1
    assert docs[0].title == "三重男揮刀"
    assert docs[0].published_at == dt.datetime(2026, 8, 30, 22, 9, tzinfo=TAIPEI)


def test_解析自由時報JSON_list與dict():
    listed = json.dumps({"code": 200, "data": [{
        "no": "1", "title": "甲",
        "url": "https://news.ltn.com.tw/news/life/breakingnews/1",
        "time": "2026/08/30 23:58",
    }]})
    keyed = json.dumps({"code": 200, "data": {
        "20": {
            "no": "2", "title": "乙",
            "url": "https://news.ltn.com.tw/news/life/breakingnews/2",
            "time": "09:39",
        }
    }})
    a = parse_ltn_json(listed)
    b = parse_ltn_json("\ufeff" + keyed)
    assert len(a) == 1 and a[0].published_at.day == 30
    assert len(b) == 1 and b[0].published_at is None  # 只有時分，不亂填今天


def test_解析報導者offset_API():
    raw = json.dumps({
        "data": {
            "meta": {"total": 2, "offset": 0, "limit": 1},
            "records": [{
                "id": "abc",
                "slug": "about-us-footer",
                "title": "關於我們",
                "published_date": "2015-12-14T16:00:00Z",
            }],
        }
    })
    docs = parse_twreporter_json(raw)
    assert docs[0].url == "https://www.twreporter.org/a/about-us-footer"
    assert docs[0].published_at.year == 2015


def test_日期走訪缺時間時補當日中午():
    spec = get_archive_spec("udn")
    docs = [ParsedDocument(url="https://udn.com/news/story/1/2", title="甲")]
    filled = apply_archive_date(docs, spec, Cursor(date=dt.date(2016, 7, 1)))
    assert filled[0].published_at == dt.datetime(2016, 7, 1, 12, 0, tzinfo=TAIPEI)


def test_ETtoday清單adapter():
    html = """
    <div class="part_list_2">
      <h3><span class="date">2012/01/01 23:57</span>
          <a href="https://www.ettoday.net/news/1615415">國賓義賣</a></h3>
      <h3><span class="date">2012/01/01 22:00</span>
          <a href="https://finance.ettoday.net/news/1615416">產經消息</a></h3>
    </div>
    <a href="/news/news-list-2012-01-01-0.htm">當日清單</a>
    """
    docs = get_adapter("ettoday").list_documents(
        html, base_url="https://www.ettoday.net")
    urls = {d.url for d in docs}
    assert "https://www.ettoday.net/news/1615415" in urls
    assert "https://finance.ettoday.net/news/1615416" in urls
    assert all("news-list" not in u for u in urls)


def test_公視清單adapter():
    html = """
    <a href="https://news.pts.org.tw/article/1">德國南部飛機對撞</a>
    <a href="/article/100">烏克蘭修道院</a>
    <a href="/dailynews">即時</a>
    """
    docs = get_adapter("pts").list_documents(
        html, base_url="https://news.pts.org.tw")
    urls = {d.url for d in docs}
    assert "https://news.pts.org.tw/article/1" in urls
    assert "https://news.pts.org.tw/article/100" in urls


def test_HTML空頁在歷史路徑回空清單而非拋錯():
    spec = get_archive_spec("pts")
    docs = parse_archive_documents(
        spec, "<html><body><a href='/about'>關於</a></body></html>",
        base_url="https://news.pts.org.tw",
    )
    assert docs == []


def test_鏡週刊sitemap只有一頁不翻頁():
    """2026-09-01 起改走 sitemap（原本判定為「無可用路徑」）。
    sitemap 沒有分頁參數，max_units=1 讓走訪抓完就停。"""
    spec = get_archive_spec("mirrormedia")
    assert spec is not None
    assert spec.url_template
    assert spec.max_units == 1
