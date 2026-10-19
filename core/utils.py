from __future__ import annotations

import hashlib
import html
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup


T = TypeVar("T")
R = TypeVar("R")


TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "spm",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}

PII_PATTERNS = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "phone": re.compile(r"(?:\+?\d{1,3}[-.\s]?)?(?:\(?0\d{1,2}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{4}"),
    "rrn_like": re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b"),
}


def ordered_parallel_map(callback: Callable[[T], R], items: list[T], *, max_workers: int = 4) -> list[R]:
    if len(items) < 2:
        return [callback(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as executor:
        return list(executor.map(callback, items))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    return compact_text(BeautifulSoup(html.unescape(value), "html.parser").get_text(" "))


def canonicalize_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def make_result_id(source: str, canonical_url: str, retrieved_at: str) -> str:
    date_part = retrieved_at.split("T", 1)[0]
    payload = f"{source}|{canonical_url}|{date_part}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def detect_pii(text: str) -> list[str]:
    found = [name for name, pattern in PII_PATTERNS.items() if pattern.search(text or "")]
    return sorted(set(found))


def policy_risk_for_text(text: str, url: str = "") -> str:
    lowered_url = (url or "").lower()
    if any(token in lowered_url for token in ("login", "signin", "account", "member", "captcha")):
        return "high"
    pii_flags = detect_pii(text)
    if "rrn_like" in pii_flags:
        return "high"
    if pii_flags:
        return "medium"
    return "low"


def soup_title(soup: BeautifulSoup, fallback: str = "") -> str:
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string
    if not title:
        meta = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "title"})
        title = str(meta.get("content") or "") if meta else ""
    return compact_text(title) or fallback


def soup_main_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "canvas"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return compact_text(main.get_text(" "))


def looks_js_heavy(html_text: str, text: str) -> bool:
    lowered = (html_text or "").lower()
    script_count = lowered.count("<script")
    root_markers = ("id=\"root\"", "id=\"app\"", "__next", "nuxt", "data-reactroot")
    return len(text or "") < 500 and (script_count >= 8 or any(marker in lowered for marker in root_markers))


def slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^0-9A-Za-z._-]+", "-", value or "").strip("-._")
    return slug[:90] or fallback


def read_secret_file(path: str | Path | None) -> str | None:
    if not path:
        return None
    target = Path(path).expanduser()
    if not target.exists() or not target.is_file():
        return None
    value = target.read_text(encoding="utf-8", errors="ignore").strip()
    return value or None


def env_or_file(env_name: str, file_env_name: str | None = None, candidates: list[Path] | None = None) -> str | None:
    value = os.getenv(env_name)
    if value:
        return value
    if file_env_name:
        value = read_secret_file(os.getenv(file_env_name))
        if value:
            return value
    for candidate in candidates or []:
        value = read_secret_file(candidate)
        if value:
            return value
    return None


def redact_env_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    for item in command:
        if len(item) > 24 and re.search(r"(key|token|secret)", item, re.I):
            redacted.append("<redacted>")
        else:
            redacted.append(item)
    return redacted


def as_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): as_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [as_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def rank_image_candidates(candidates: list[dict[str, object]], base_url: str = "") -> list[str]:
    """Rank public image URLs using metadata, layout, dimensions, and names."""
    ranked: dict[str, tuple[int, int]] = {}
    base_host = urlparse(base_url).netloc.lower()
    positive = re.compile(r"(hero|cover|featured|main|article|post|content|thumbnail)", re.I)
    negative = re.compile(r"(logo|icon|avatar|sprite|spacer|pixel|tracking|spinner|loading|advert|banner)", re.I)

    for order, candidate in enumerate(candidates):
        raw_url = str(candidate.get("url") or "").strip()
        image_url = urljoin(base_url, raw_url)
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue

        context = str(candidate.get("context") or "")
        searchable = f"{parsed.path} {context}"
        score = int(candidate.get("priority") or 0) + max(0, 50 - order)
        if positive.search(searchable):
            score += 80
        if negative.search(searchable):
            score -= 800
        if re.search(r"\b(main|article|figure)\b", context, re.I):
            score += 140
        if re.search(r"\b(header|nav|footer|aside)\b", context, re.I):
            score -= 140

        try:
            width = int(float(candidate.get("width") or 0))
            height = int(float(candidate.get("height") or 0))
        except (TypeError, ValueError):
            width = height = 0
        if width >= 600 and height >= 300:
            score += 160
        elif width >= 300 and height >= 180:
            score += 90
        elif width and height and (width < 120 or height < 90):
            score -= 300
        if width and height and (width / height > 5 or height / width > 5):
            score -= 120

        if base_host and parsed.netloc.lower() == base_host:
            score += 20
        if parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
            score += 20
        if parsed.path.lower().endswith((".svg", ".gif")):
            score -= 80

        previous = ranked.get(image_url)
        if previous is None or score > previous[0]:
            ranked[image_url] = (score, order)

    return [url for url, _ in sorted(ranked.items(), key=lambda item: (-item[1][0], item[1][1]))[:100]]


def extract_image_page_links(html_text: str | None, base_url: str = "") -> list[str]:
    """Extract and rank representative image URLs from HTML."""
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    candidates: list[dict[str, object]] = []

    meta_selectors = (
        ('meta[property="og:image"], meta[property="og:image:url"]', 1000, "open-graph"),
        ('meta[name="twitter:image"], meta[name="twitter:image:src"]', 950, "twitter-card"),
        ('link[rel~="image_src"]', 900, "image-src"),
    )
    for selector, priority, context in meta_selectors:
        for tag in soup.select(selector):
            candidates.append(
                {
                    "url": tag.get("content") or tag.get("href"),
                    "priority": priority,
                    "context": context,
                }
            )

    for img in soup.find_all("img"):
        container = img.find_parent(["main", "article", "figure", "header", "nav", "footer", "aside"])
        context = " ".join(
            str(value)
            for value in (
                container.name if container else "",
                " ".join(img.get("class") or []),
                img.get("id"),
                img.get("alt"),
                img.get("title"),
            )
            if value
        )
        common = {
            "priority": 100,
            "context": context,
            "width": img.get("width"),
            "height": img.get("height"),
        }
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if src:
            candidates.append({"url": src, **common})
        for part in str(img.get("srcset") or "").split(","):
            srcset_url = part.strip().split(" ", 1)[0]
            if srcset_url:
                candidates.append({"url": srcset_url, **common, "priority": 110})

    for tag in soup.find_all(style=True):
        context = f"{tag.name} {' '.join(tag.get('class') or [])}"
        for image_url in re.findall(r'url\(["\']?(.*?)["\']?\)', str(tag.get("style") or ""), re.I):
            candidates.append({"url": image_url.strip(), "priority": 20, "context": context})

    return rank_image_candidates(candidates, base_url)


def extract_snapshot_image_links(elements: list[dict[str, Any]] | None, base_url: str = "") -> list[str]:
    """Extract visible image candidates from a stealth-browser element snapshot."""
    candidates: list[dict[str, object]] = []
    for item in elements or []:
        if str(item.get("tag") or "").lower() != "img":
            continue
        attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
        bbox = item.get("bbox") if isinstance(item.get("bbox"), dict) else {}
        candidates.append(
            {
                "url": attrs.get("src") or attrs.get("data-src") or attrs.get("data-original"),
                "priority": 150,
                "context": " ".join(
                    str(value)
                    for value in (item.get("css"), item.get("xpath"), attrs.get("alt"), item.get("text"))
                    if value
                ),
                "width": bbox.get("w"),
                "height": bbox.get("h"),
            }
        )
    return rank_image_candidates(candidates, base_url)
