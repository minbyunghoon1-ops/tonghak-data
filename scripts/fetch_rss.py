#!/usr/bin/env python3
"""Fetch RSS feeds and write a compact, deduplicated articles.json."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlsplit, urlunsplit

import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = ROOT / "articles.json"

# 각 항목: RSS 주소 | 분야 | 출처명
# 분야가 여러 개인 경우 쉼표로 구분합니다.
FEEDS = [
    ("https://www.hankyung.com/feed/all-news", ["경제"], "한국경제"),
    ("https://www.mk.co.kr/rss/40300001/", ["경제"], "매일경제"),
    ("https://rss.etnews.com/Section901.xml", ["IT"], "전자신문"),
    ("https://feeds.feedburner.com/zdkorea", ["IT"], "지디넷 코리아"),
    ("https://byline.network/feed/", ["IT"], "바이라인네트워크"),
    ("https://cdn.bloter.net/rss/gns_allArticle.xml", ["IT"], "블로터"),
    ("https://www.sedaily.com/rss/newsall", ["경제"], "서울경제"),
    ("https://www.khan.co.kr/rss/rssdata/total_news.xml", ["시사"], "경향신문"),
    ("https://www.sisain.co.kr/rss/allArticle.xml", ["시사", "인문교양"], "시사IN"),
    ("https://h21.hani.co.kr/rss/", ["시사", "인문교양"], "한겨레21"),
    ("https://www.pressian.com/api/v3/site/rss/news", ["인문교양"], "프레시안"),
    ("https://www.technologyreview.com/feed/", ["IT"], "MIT Technology Review"),
    ("https://www.wired.com/feed/rss", ["IT"], "WIRED"),
    # 아래 3개 주소는 사용자가 제공한 홈페이지 주소입니다.
    # RSS/Atom 피드 주소가 아니면 해당 소스만 실패 로그에 남기고 건너뜁니다.
    ("https://www.economist.com/", ["경제"], "The Economist"),
    ("https://www.ft.com/", ["경제"], "Financial Times"),
    ("https://feeds.arstechnica.com/arstechnica/features", ["IT"], "Ars Technica"),
    ("https://stratechery.com/feed/", ["IT"], "Stratechery"),
    ("https://www.kdi.re.kr/", ["경제"], "KDI"),
]

DAYS_TO_KEEP = 60
MAX_ARTICLES = 200
MAX_ENTRIES_PER_FEED = 40
REQUEST_TIMEOUT = (8, 18)
USER_AGENT = (
    "Mozilla/5.0 (compatible; StudentReadingRSSBot/1.0; "
    "+https://github.com/)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("rss-fetcher")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def clean_text(value: Any) -> str:
    """Convert HTML or arbitrary feed values to normalized plain text."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    soup = BeautifulSoup(str(value), "html.parser")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url: str) -> str:
    """Remove fragments while retaining query parameters."""
    url = (url or "").strip()
    if not url:
        return ""
    url, _fragment = urldefrag(url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def parse_published(entry: Any) -> datetime | None:
    """Read RSS date fields and return an aware UTC datetime."""
    for key in ("published", "updated", "created", "date"):
        value = entry.get(key)
        if not value:
            continue
        try:
            dt = date_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            pass

    # feedparser may have already parsed a date into a time.struct_time.
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
    return None


def fetch_feed(feed_url: str) -> Any:
    response = session.get(feed_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"RSS 파싱 실패: {parsed.bozo_exception}")
    if not parsed.entries:
        content_type = response.headers.get("Content-Type", "unknown")
        raise ValueError(
            f"피드 항목이 없습니다 (Content-Type: {content_type}). "
            "홈페이지 URL이거나 RSS 접근이 차단됐을 수 있습니다."
        )
    return parsed


def get_entry_text(entry: Any) -> tuple[str, str]:
    """Return (candidate full text, feed summary); never persist the full text."""
    full_text = ""

    contents = entry.get("content") or []
    if contents:
        full_text = max(
            (clean_text(item.get("value", "")) for item in contents),
            key=len,
            default="",
        )

    summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
    # A content:encoded field can be a full article, but some feeds only include
    # a short teaser. Use it as a body only when it is meaningfully longer.
    if full_text and len(full_text) < 500 and summary and len(summary) > len(full_text):
        full_text = ""

    return full_text, summary


def fetch_article_body(url: str) -> str:
    """Best-effort article extraction. Returned text is used only for counting."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            downloaded = response.text

        extracted = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            output_format="txt",
        )
        return clean_text(extracted)
    except Exception as exc:  # one article must not stop the feed
        logger.debug("본문 추출 실패 (%s): %s", url, exc)
        return ""


def make_summary(summary: str, body: str) -> str:
    """Use the feed's teaser first; otherwise use a short body excerpt."""
    candidate = summary or body
    candidate = clean_text(candidate)
    if not candidate:
        return "요약을 제공하지 않는 기사입니다. 원문에서 확인하세요."
    # Keep a single-line snippet and cap it at 100 Unicode characters.
    candidate = re.sub(r"[\r\n\t]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if len(candidate) > 100:
        candidate = candidate[:99].rstrip() + "…"
    return candidate


def estimate_minutes(body: str, summary: str) -> tuple[int, bool]:
    """500 characters/minute; fallback estimate is at least 3 minutes."""
    if body:
        return max(1, math.ceil(len(body) / 500)), False
    basis = summary.strip()
    return max(3, math.ceil(len(basis) / 500)), True


def article_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def collect_articles() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DAYS_TO_KEEP)
    collected: dict[str, dict[str, Any]] = {}

    for feed_url, categories, source_name in FEEDS:
        logger.info("RSS 수집 시작: %s (%s)", source_name, feed_url)
        try:
            parsed = fetch_feed(feed_url)
        except Exception as exc:
            logger.error("소스 실패 - %s: %s", source_name, exc)
            continue

        feed_count = 0
        for entry in parsed.entries[:MAX_ENTRIES_PER_FEED]:
            try:
                url = normalize_url(entry.get("link", ""))
                title = clean_text(entry.get("title", ""))
                published = parse_published(entry)

                if not url or not title:
                    logger.warning("필수 필드 누락 - %s: %s", source_name, title or "(제목 없음)")
                    continue
                if published is None:
                    logger.warning("발행일 확인 불가로 제외 - %s: %s", source_name, title)
                    continue
                if published < cutoff or published > now + timedelta(days=1):
                    continue

                # Avoid duplicate body downloads when multiple feeds syndicate
                # the same URL. First source encountered supplies metadata.
                if url in collected:
                    continue

                feed_body, feed_summary = get_entry_text(entry)
                body = feed_body
                if len(body) < 500:
                    extracted_body = fetch_article_body(url)
                    if len(extracted_body) > len(body):
                        body = extracted_body
                    # Be polite to publishers and reduce request bursts.
                    time.sleep(0.15)

                summary = make_summary(feed_summary, body)
                minutes, estimated = estimate_minutes(body, feed_summary or summary)

                article: dict[str, Any] = {
                    "id": article_id(url),
                    "title": title,
                    "category": categories,
                    "source": source_name,
                    "url": url,
                    "minutes": minutes,
                    "summary": summary,
                    "publishedAt": published.isoformat().replace("+00:00", "Z"),
                }
                if estimated:
                    article["estimated"] = True

                collected[url] = article
                feed_count += 1
            except Exception:
                logger.exception("기사 처리 실패 - 소스: %s", source_name)

        logger.info("소스 완료 - %s: %d개 기사 추가", source_name, feed_count)

    articles = list(collected.values())
    articles.sort(key=lambda item: item["publishedAt"], reverse=True)
    return articles[:MAX_ARTICLES]


def main() -> int:
    articles = collect_articles()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    serialized = json.dumps(
        articles,
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    old_content = OUTPUT_FILE.read_text(encoding="utf-8") if OUTPUT_FILE.exists() else None
    if old_content == serialized:
        logger.info("변경 사항 없음: articles.json을 수정하지 않았습니다.")
        return 0

    OUTPUT_FILE.write_text(serialized, encoding="utf-8")
    logger.info("완료: %d개 기사를 %s에 저장했습니다.", len(articles), OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("전체 작업 중 처리되지 않은 오류가 발생했습니다.")
        sys.exit(1)
