from __future__ import annotations

import os
from dataclasses import fields
from typing import Any

import httpx

from .schema import CrawlResult
from .search import SearchAgentError


DEFAULT_GATEWAY_URL = "http://127.0.0.1:9101"


def gateway_url() -> str:
    return os.getenv("WEB_SEARCH_GATEWAY_URL", DEFAULT_GATEWAY_URL).rstrip("/")


def gateway_disabled() -> bool:
    return os.getenv("WEB_SEARCH_GATEWAY_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def should_use_gateway(local: bool = False) -> bool:
    return bool(os.getenv("WEB_SEARCH_GATEWAY_URL", "").strip()) and not local and not gateway_disabled()


def post_gateway(path: str, payload: dict[str, object], *, timeout: float = 180.0) -> dict[str, Any]:
    url = f"{gateway_url()}{path}"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        raise SearchAgentError(f"gateway request failed: {url}: {exc}") from exc
    if not isinstance(data, dict):
        raise SearchAgentError(f"gateway returned non-object payload: {url}")
    return data


def gateway_search(
    query: str,
    *,
    provider: str,
    limit: int,
    start: int = 1,
    intent: str | None = None,
) -> dict[str, Any]:
    return post_gateway(
        "/api/search",
        {
            "provider": provider,
            "query": query,
            "limit": limit,
            "start": start,
            "intent": intent or "",
        },
        timeout=60.0,
    )


def gateway_crawl(
    url: str,
    *,
    crawler: str,
    respect_robots: bool = True,
    browser: str = "chromium",
    allow_selenium_fallback: bool = False,
    base_delay_seconds: float = 2.0,
    government_delay_seconds: float = 10.0,
) -> dict[str, Any]:
    return post_gateway(
        "/api/crawl",
        {
            "url": url,
            "crawler": crawler,
            "respect_robots": respect_robots,
            "browser": browser,
            "allow_selenium_fallback": allow_selenium_fallback,
            "base_delay_seconds": base_delay_seconds,
            "government_delay_seconds": government_delay_seconds,
        },
        timeout=240.0,
    )


def crawl_result_from_payload(payload: dict[str, Any]) -> CrawlResult:
    names = {field.name for field in fields(CrawlResult)}
    clean = {key: value for key, value in payload.items() if key in names}
    return CrawlResult(**clean)
