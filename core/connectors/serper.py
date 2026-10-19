from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..credentials import secret_for_env
from ..schema import EvidenceRecord
from ..usage import record_api_usage, usage_dir
from ..utils import canonicalize_url, strip_html
from .base import require_secret


SERPER_COMPANY_CACHE_TTL_SECONDS = 20 * 60 * 60
SERPER_PERSON_CACHE_TTL_SECONDS = 3 * 24 * 60 * 60
_SERPER_CACHE_DB_LOCK = threading.Lock()
# ponytail: striped locks avoid duplicate in-flight calls without retaining one lock per query forever.
_SERPER_CACHE_KEY_LOCKS = tuple(threading.Lock() for _ in range(64))


def _serper_cache_path() -> Path:
    return usage_dir().parent / "cache" / "serper.sqlite3"


def _cache_now() -> float:
    return time.time()


def _cache_ttl_seconds(query_intent: str | None) -> int:
    intent = (query_intent or "").strip()
    if intent.startswith(("person_evidence:", "linkedin_profile_finder")) or intent == (
        "noteapp_complete:profile_fallback"
    ):
        return SERPER_PERSON_CACHE_TTL_SECONDS
    return SERPER_COMPANY_CACHE_TTL_SECONDS


def _cache_key(endpoint: str, request_payload: dict[str, Any], ttl_seconds: int) -> str:
    raw = json.dumps(
        {"endpoint": endpoint, "payload": request_payload, "ttl_seconds": ttl_seconds},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_read(cache_key: str, *, ttl_seconds: int) -> tuple[dict[str, Any], float, bool] | None:
    path = _serper_cache_path()
    now = _cache_now()
    try:
        with _SERPER_CACHE_DB_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path, timeout=5.0) as database:
                database.execute(
                    "CREATE TABLE IF NOT EXISTS responses "
                    "(cache_key TEXT PRIMARY KEY, stored_at REAL NOT NULL, payload_json TEXT NOT NULL)"
                )
                row = database.execute(
                    "SELECT stored_at, payload_json FROM responses WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
                if row is None:
                    return None
                stored_at = float(row[0])
                payload = json.loads(str(row[1]))
            path.chmod(0o600)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    return (payload, stored_at, now - stored_at < ttl_seconds) if isinstance(payload, dict) else None


def _merge_result_payloads(
    cached_payload: dict[str, Any], fresh_payload: dict[str, Any], *, result_key: str
) -> dict[str, Any]:
    cached_items = cached_payload.get(result_key)
    fresh_items = fresh_payload.get(result_key)
    if not isinstance(cached_items, list) or not isinstance(fresh_items, list):
        return fresh_payload

    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in fresh_items + cached_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("link") or item.get("url") or "").strip()
        if not url:
            continue
        canonical_url = canonicalize_url(url)
        if canonical_url in seen_urls:
            continue
        seen_urls.add(canonical_url)
        merged.append(item)

    payload = dict(fresh_payload)
    payload[result_key] = merged
    return payload


def _cache_write(cache_key: str, payload: dict[str, Any], *, stored_at: float) -> None:
    path = _serper_cache_path()
    try:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with _SERPER_CACHE_DB_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path, timeout=5.0) as database:
                database.execute(
                    "CREATE TABLE IF NOT EXISTS responses "
                    "(cache_key TEXT PRIMARY KEY, stored_at REAL NOT NULL, payload_json TEXT NOT NULL)"
                )
                database.execute(
                    "INSERT OR REPLACE INTO responses(cache_key, stored_at, payload_json) VALUES (?, ?, ?)",
                    (cache_key, stored_at, serialized),
                )
                # ponytail: stale rows preserve cumulative results; add a row cap if DB growth becomes measurable.
            path.chmod(0o600)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return


class SerperSearchClient:
    provider = "serper"
    endpoint = "https://google.serper.dev/search"
    news_endpoint = "https://google.serper.dev/news"

    def __init__(self) -> None:
        self.api_key = require_secret(secret_for_env("SERPER_API_KEY"), "SERPER_API_KEY")
        self.timeout = 30.0

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        started = time.monotonic()
        num = max(1, min(limit, 10))
        page = max(1, ((max(1, start) - 1) // num) + 1)
        request_payload: dict[str, Any] = {"q": query, "num": num}
        news_mode = query_intent == "noteapp_complete:news"
        if news_mode:
            request_payload.update({"gl": "kr", "hl": "ko"})
        if page > 1:
            request_payload["page"] = page
        endpoint = self.news_endpoint if news_mode else self.endpoint
        cache_ttl_seconds = _cache_ttl_seconds(query_intent)
        cache_key = _cache_key(endpoint, request_payload, cache_ttl_seconds)
        cache_lock = _SERPER_CACHE_KEY_LOCKS[int(cache_key[:8], 16) % len(_SERPER_CACHE_KEY_LOCKS)]

        with cache_lock:
            cached = _cache_read(cache_key, ttl_seconds=cache_ttl_seconds)
            if cached is not None and cached[2]:
                payload, stored_at, _fresh = cached
                records = self._records_from_payload(
                    payload,
                    query=query,
                    query_intent=query_intent,
                    result_key="news" if news_mode else "organic",
                    retrieved_at=datetime.fromtimestamp(stored_at, timezone.utc).isoformat(),
                )
                record_api_usage(
                    "serper_cache",
                    "read",
                    ok=True,
                    result_count=len(records),
                    metadata={"managed_service": "local_sqlite_cache", "mode": "hit"},
                )
                return records

            cached_payload = cached[0] if cached is not None else None
            response: httpx.Response | None = None
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    response = client.post(
                        endpoint,
                        headers={
                            "X-API-KEY": self.api_key,
                            "Content-Type": "application/json",
                        },
                        json=request_payload,
                    )
                    response.raise_for_status()
                    payload = response.json()
                result_key = "news" if news_mode else "organic"
                if cached_payload is not None:
                    payload = _merge_result_payloads(cached_payload, payload, result_key=result_key)
                stored_at = _cache_now()
                records = self._records_from_payload(
                    payload,
                    query=query,
                    query_intent=query_intent,
                    result_key=result_key,
                    retrieved_at=datetime.fromtimestamp(stored_at, timezone.utc).isoformat(),
                )
            except Exception as exc:
                record_api_usage(
                    self.provider,
                    "search",
                    ok=False,
                    status_code=response.status_code if response is not None else None,
                    response_headers=response.headers if response is not None else None,
                    metadata={"error_type": type(exc).__name__},
                    search_audit={
                        "query": query,
                        "query_intent": query_intent,
                        "requested_limit": num,
                        "start": start,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
                raise

            _cache_write(cache_key, payload, stored_at=stored_at)
            record_api_usage(
                self.provider,
                "search",
                ok=True,
                status_code=response.status_code if response is not None else None,
                response_headers=response.headers if response is not None else None,
                result_count=len(records),
                units=self._credit_units(payload),
                metadata={"managed_service": "serper"},
                search_audit={
                    "query": query,
                    "query_intent": query_intent,
                    "requested_limit": num,
                    "start": start,
                    "result_urls": [record.url for record in records],
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            return records

    def _credit_units(self, payload: dict[str, Any]) -> int:
        try:
            return max(1, int(payload.get("credits") or 1))
        except (TypeError, ValueError):
            return 1

    def _records_from_payload(
        self,
        payload: dict[str, Any],
        *,
        query: str,
        query_intent: str | None,
        result_key: str = "organic",
        retrieved_at: str | None = None,
    ) -> list[EvidenceRecord]:
        organic = payload.get(result_key)
        if not isinstance(organic, list):
            organic = []

        records: list[EvidenceRecord] = []
        for item in organic:
            if not isinstance(item, dict):
                continue
            url = str(item.get("link") or item.get("url") or "").strip()
            if not url:
                continue
            records.append(
                EvidenceRecord.build(
                    source=self.provider,
                    source_type="search_engine_result",
                    title=strip_html(str(item.get("title") or url)),
                    url=url,
                    snippet=strip_html(str(item.get("snippet") or item.get("description") or "")),
                    raw_payload_json=item,
                    published_at=item.get("date") if isinstance(item.get("date"), str) else None,
                    retrieved_at=retrieved_at,
                    query=query,
                    query_intent=query_intent,
                    confidence=0.3,
                    confidence_reason="Serper Google SERP metadata; verify against source URL before final use",
                )
            )
        return records
