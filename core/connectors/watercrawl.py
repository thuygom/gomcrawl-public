from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..credentials import secret_for_env
from ..schema import EvidenceRecord
from ..usage import record_api_usage
from ..utils import compact_text, strip_html
from .base import MissingCredentialError, SearchAgentError, require_secret
from .serper import SerperSearchClient


TERMINAL_STATUSES = {"finished", "failed", "canceled", "canceling"}


def _int_env(name: str, default: int, *, lower: int, upper: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError:
        value = default
    return max(lower, min(value, upper))


def _float_env(name: str, default: float, *, lower: float, upper: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except ValueError:
        value = default
    return max(lower, min(value, upper))


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _items_from_page(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _first_text(value: Any) -> str:
    if isinstance(value, str):
        return compact_text(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if value is None:
        return ""
    return compact_text(json.dumps(value, ensure_ascii=False))


class WatercrawlApiClient:
    """Small client for a self-hosted or cloud WaterCrawl-compatible API."""

    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
        poll_interval_seconds: float | None = None,
        max_wait_seconds: int | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> None:
        resolved_url = (
            api_url
            or os.getenv("WATERCRAWL_API_URL")
            or os.getenv("WATERCRAWL_BASE_URL")
            or secret_for_env("WATERCRAWL_API_URL")
        )
        if not resolved_url:
            raise MissingCredentialError("WATERCRAWL_API_URL is missing")
        self.api_url = resolved_url.rstrip("/")
        self.api_key = require_secret(api_key or secret_for_env("WATERCRAWL_API_KEY"), "WATERCRAWL_API_KEY")
        self.timeout = timeout or _int_env("WATERCRAWL_TIMEOUT_SECONDS", 60, lower=5, upper=300)
        self.poll_interval_seconds = poll_interval_seconds or _float_env(
            "WATERCRAWL_POLL_INTERVAL_SECONDS",
            2.0,
            lower=0.1,
            upper=30.0,
        )
        self.max_wait_seconds = max_wait_seconds or _int_env("WATERCRAWL_MAX_WAIT_SECONDS", 180, lower=5, upper=900)
        self.transport = transport

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

    def _url(self, path: str) -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        return f"{self.api_url}{normalized}"

    def request_json(self, method: str, path: str, **kwargs: Any) -> tuple[Any, httpx.Response]:
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers=self.headers,
                transport=self.transport,
            ) as client:
                response = client.request(method, self._url(path), **kwargs)
                response.raise_for_status()
                if response.content:
                    return response.json(), response
                return {}, response
        except httpx.HTTPStatusError as exc:
            raise SearchAgentError(f"WaterCrawl HTTP {exc.response.status_code}: {compact_text(exc.response.text)[:800]}") from exc
        except httpx.HTTPError as exc:
            raise SearchAgentError(f"WaterCrawl request failed: {type(exc).__name__}: {exc}") from exc
        except ValueError as exc:
            raise SearchAgentError("WaterCrawl returned non-JSON response") from exc

    def poll_request(self, path: str, request_uuid: str) -> tuple[dict[str, Any], httpx.Response]:
        deadline = time.time() + self.max_wait_seconds
        last_payload: dict[str, Any] = {}
        last_response: httpx.Response | None = None
        while time.time() <= deadline:
            payload, response = self.request_json("GET", f"{path}/{request_uuid}/", params={"prefetched": "true"})
            last_response = response
            if isinstance(payload, dict):
                last_payload = payload
                status = str(payload.get("status") or "").lower()
                if status in TERMINAL_STATUSES:
                    return payload, response
            time.sleep(self.poll_interval_seconds)
        status = last_payload.get("status") or "unknown"
        raise SearchAgentError(f"WaterCrawl request {request_uuid} timed out after {self.max_wait_seconds}s; last status={status}")

    def create_crawl(self, url: str, *, max_depth: int = 0, page_limit: int = 1, include_html: bool = True) -> tuple[dict[str, Any], httpx.Response]:
        parsed = urlparse(url)
        allowed_domains = [parsed.netloc] if parsed.netloc else []
        payload = {
            "url": url,
            "options": {
                "spider_options": {
                    "max_depth": max_depth,
                    "page_limit": page_limit,
                    "allowed_domains": allowed_domains,
                    "exclude_paths": [],
                    "include_paths": [],
                    "proxy_server": None,
                },
                "page_options": {
                    "exclude_tags": [],
                    "include_tags": [],
                    "wait_time": 1000,
                    "include_html": include_html,
                    "only_main_content": True,
                    "include_links": True,
                    "timeout": 15000,
                    "accept_cookies_selector": None,
                    "locale": "en-US",
                    "extra_headers": {},
                    "actions": [],
                    "ignore_rendering": False,
                },
            },
        }
        payload_json, response = self.request_json("POST", "/api/v1/core/crawl-requests/", json=payload)
        if not isinstance(payload_json, dict):
            raise SearchAgentError("WaterCrawl crawl create returned an unexpected payload")
        return payload_json, response

    def crawl_results(self, request_uuid: str) -> tuple[list[dict[str, Any]], httpx.Response]:
        payload, response = self.request_json(
            "GET",
            f"/api/v1/core/crawl-requests/{request_uuid}/results/",
            params={"prefetched": "true", "page_size": 100},
        )
        return _items_from_page(payload), response

    def create_search(self, query: str, *, limit: int = 10) -> tuple[dict[str, Any], httpx.Response]:
        payload = {
            "query": query,
            "result_limit": max(1, min(limit, 20)),
            "search_options": {
                "language": None,
                "country": None,
                "time_renge": "any",
                "search_type": "web",
                "depth": os.getenv("WATERCRAWL_SEARCH_DEPTH", "basic"),
            },
        }
        payload_json, response = self.request_json("POST", "/api/v1/core/search/", json=payload)
        if not isinstance(payload_json, dict):
            raise SearchAgentError("WaterCrawl search create returned an unexpected payload")
        return payload_json, response


class WatercrawlSearchClient:
    provider = "watercrawl_search"

    def __init__(
        self,
        *,
        client: WatercrawlApiClient | None = None,
        discovery_client: Any | None = None,
        mode: str | None = None,
        crawl_limit: int | None = None,
    ) -> None:
        self.client = client or WatercrawlApiClient()
        self.discovery_client = discovery_client
        self.mode = (mode or os.getenv("WATERCRAWL_SEARCH_MODE") or "serper_crawl").strip().lower()
        self.crawl_limit = crawl_limit or _int_env("WATERCRAWL_SEARCH_CRAWL_LIMIT", 3, lower=0, upper=10)

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        if self.mode in {"native", "watercrawl", "watercrawl_native"}:
            return self._native_search(query, limit=limit, start=start, query_intent=query_intent)
        if self.mode not in {"serper", "serper_crawl", "serper_then_crawl", "default"}:
            raise SearchAgentError(f"unsupported WATERCRAWL_SEARCH_MODE={self.mode}")
        return self._serper_then_crawl_search(query, limit=limit, start=start, query_intent=query_intent)

    def _native_search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        started = time.monotonic()
        requested_limit = max(1, min(limit, 20))
        create_response: httpx.Response | None = None
        final_response: httpx.Response | None = None
        try:
            created, create_response = self.client.create_search(query, limit=limit)
            request_uuid = str(created.get("uuid") or "")
            if not request_uuid:
                raise SearchAgentError("WaterCrawl search create response did not include uuid")
            finished, final_response = self.client.poll_request("/api/v1/core/search", request_uuid)
            status = str(finished.get("status") or "").lower()
            if status != "finished":
                raise SearchAgentError(f"WaterCrawl search ended with status={status}")
            raw_results = finished.get("result") if isinstance(finished, dict) else []
            results = _items_from_page(raw_results if raw_results is not None else [])
        except Exception as exc:
            response = final_response or create_response
            record_api_usage(
                self.provider,
                "search",
                ok=False,
                status_code=response.status_code if response is not None else None,
                response_headers=response.headers if response is not None else None,
                metadata={"managed_service": "watercrawl", "error_type": type(exc).__name__},
                search_audit={
                    "query": query,
                    "query_intent": query_intent,
                    "requested_limit": requested_limit,
                    "start": start,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            raise

        records: list[EvidenceRecord] = []
        for item in results[:requested_limit]:
            url = str(item.get("url") or item.get("link") or "").strip()
            if not url:
                continue
            records.append(
                EvidenceRecord.build(
                    source=self.provider,
                    source_type="search_engine_result",
                    title=strip_html(_first_text(item.get("title") or item.get("name") or url)),
                    url=url,
                    snippet=strip_html(_first_text(item.get("description") or item.get("snippet") or item.get("markdown") or ""))[:2000],
                    raw_payload_json=item,
                    published_at=item.get("published_at") or item.get("publishedDate"),
                    query=query,
                    query_intent=query_intent,
                    confidence=0.3,
                    confidence_reason="WaterCrawl search metadata; verify against source URL before identity use",
                )
            )
        record_api_usage(
            self.provider,
            "search",
            ok=True,
            status_code=final_response.status_code if final_response is not None else None,
            response_headers=final_response.headers if final_response is not None else None,
            result_count=len(records),
            metadata={"managed_service": "watercrawl"},
            search_audit={
                "query": query,
                "query_intent": query_intent,
                "requested_limit": requested_limit,
                "start": start,
                "result_urls": [record.url for record in records],
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return records

    @staticmethod
    def _result_content(item: dict[str, Any]) -> dict[str, Any]:
        result = item.get("result")
        if isinstance(result, dict):
            return result
        return item

    @staticmethod
    def _metadata_title(metadata: Any, fallback: str) -> str:
        if not isinstance(metadata, dict):
            return fallback
        for key in ("title", "og:title", "twitter:title"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return fallback

    @staticmethod
    def _text_from_content(content: dict[str, Any]) -> str:
        markdown = content.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown
        html_text = content.get("html")
        if isinstance(html_text, str) and html_text.strip():
            return strip_html(html_text)
        extraction = content.get("extraction")
        if extraction not in (None, "", [], {}):
            return compact_text(json.dumps(extraction, ensure_ascii=False))
        return compact_text(json.dumps(content, ensure_ascii=False))

    def _discovery(self) -> Any:
        if self.discovery_client is not None:
            return self.discovery_client
        return SerperSearchClient()

    @staticmethod
    def _dedupe(records: list[EvidenceRecord], *, limit: int) -> list[EvidenceRecord]:
        seen: set[str] = set()
        deduped: list[EvidenceRecord] = []
        for record in records:
            key = record.canonical_url or record.url
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
            if len(deduped) >= limit:
                break
        return deduped

    def _seed_record(
        self,
        seed: EvidenceRecord,
        *,
        query: str,
        query_intent: str | None,
        raw_extra: dict[str, Any] | None = None,
    ) -> EvidenceRecord:
        raw_payload = {
            "mode": "serper_crawl",
            "discovery_provider": seed.source,
            "discovery_record": seed.raw_payload_json,
        }
        if raw_extra:
            raw_payload.update(raw_extra)
        return EvidenceRecord.build(
            source=self.provider,
            source_type="search_then_crawl",
            title=seed.title,
            url=seed.url,
            snippet=seed.snippet,
            raw_payload_json=raw_payload,
            published_at=seed.published_at,
            query=query,
            query_intent=query_intent,
            confidence=0.4,
            confidence_reason="Serper discovered URL; WaterCrawl crawl was skipped or unavailable",
            processing_status="raw",
        )

    def _crawl_seed(
        self,
        seed: EvidenceRecord,
        *,
        query: str,
        query_intent: str | None,
    ) -> EvidenceRecord:
        create_response: httpx.Response | None = None
        final_response: httpx.Response | None = None
        try:
            created, create_response = self.client.create_crawl(
                seed.url,
                max_depth=0,
                page_limit=1,
                include_html=_bool_env("WATERCRAWL_INCLUDE_HTML", True),
            )
            request_uuid = str(created.get("uuid") or "")
            if not request_uuid:
                raise SearchAgentError("WaterCrawl crawl create response did not include uuid")
            finished, final_response = self.client.poll_request("/api/v1/core/crawl-requests", request_uuid)
            status = str(finished.get("status") or "").lower()
            if status != "finished":
                raise SearchAgentError(f"WaterCrawl crawl ended with status={status}")
            items, results_response = self.client.crawl_results(request_uuid)
            final_response = results_response
            if not items:
                raise SearchAgentError("WaterCrawl finished but returned no crawl results")
        except Exception as exc:
            response = final_response or create_response
            record_api_usage(
                "watercrawl",
                "crawl",
                ok=False,
                status_code=response.status_code if response is not None else None,
                response_headers=response.headers if response is not None else None,
                metadata={
                    "managed_service": "watercrawl",
                    "operation_parent": self.provider,
                    "discovery_provider": seed.source,
                    "error_type": type(exc).__name__,
                },
            )
            return self._seed_record(
                seed,
                query=query,
                query_intent=query_intent,
                raw_extra={"watercrawl_error": str(exc), "processing_status": "crawl_error_fallback"},
            )

        record_api_usage(
            "watercrawl",
            "crawl",
            ok=True,
            status_code=final_response.status_code if final_response is not None else None,
            response_headers=final_response.headers if final_response is not None else None,
            result_count=len(items),
            metadata={
                "managed_service": "watercrawl",
                "operation_parent": self.provider,
                "discovery_provider": seed.source,
                "item_count": len(items),
            },
        )
        primary = items[0]
        content = self._result_content(primary)
        final_url = str(primary.get("url") or seed.url)
        metadata = content.get("metadata")
        text = self._text_from_content(content)
        html_text = content.get("html") if isinstance(content.get("html"), str) else None
        markdown = content.get("markdown") if isinstance(content.get("markdown"), str) else None
        title = self._metadata_title(metadata, seed.title or final_url)
        raw_payload = {
            "mode": "serper_crawl",
            "discovery_provider": seed.source,
            "discovery_record": seed.raw_payload_json,
            "crawl": {
                "crawler": "watercrawl",
                "status_code": final_response.status_code if final_response is not None else None,
                "request_uuid": str(primary.get("request") or ""),
                "item_count": len(items),
                "text_length": len(text or ""),
                "has_markdown": bool(markdown),
                "has_html": bool(html_text),
            },
        }
        return EvidenceRecord.build(
            source=self.provider,
            source_type="search_then_crawl",
            title=title,
            url=final_url,
            snippet=(text or seed.snippet)[:2000],
            raw_payload_json=raw_payload,
            published_at=seed.published_at,
            retrieved_at=None,
            query=query,
            query_intent=query_intent,
            confidence=0.65 if text else 0.4,
            confidence_reason="Serper discovered URL and WaterCrawl crawled the page; verify source content before identity use",
            processing_status="crawled" if text else "raw",
        )

    def _serper_then_crawl_search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        started = time.monotonic()
        max_results = max(1, min(limit, 10))
        try:
            discovered = self._discovery().search(
                query,
                limit=max_results,
                start=start,
                query_intent=query_intent,
            )
            seeds = self._dedupe(discovered, limit=max_results)
            records: list[EvidenceRecord] = []
            for index, seed in enumerate(seeds):
                if index < self.crawl_limit:
                    records.append(self._crawl_seed(seed, query=query, query_intent=query_intent))
                else:
                    records.append(
                        self._seed_record(
                            seed,
                            query=query,
                            query_intent=query_intent,
                            raw_extra={"crawl_skipped": "WATERCRAWL_SEARCH_CRAWL_LIMIT reached"},
                        )
                    )
        except Exception as exc:
            record_api_usage(
                self.provider,
                "search",
                ok=False,
                metadata={
                    "mode": "serper_crawl",
                    "discovery_provider": "serper",
                    "crawler": "watercrawl",
                    "error_type": type(exc).__name__,
                },
                search_audit={
                    "query": query,
                    "query_intent": query_intent,
                    "requested_limit": max_results,
                    "start": start,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            raise

        record_api_usage(
            self.provider,
            "search",
            ok=True,
            result_count=len(records),
            metadata={
                "mode": "serper_crawl",
                "discovery_provider": "serper",
                "crawler": "watercrawl",
                "discovered_count": len(seeds),
                "crawled_count": min(len(seeds), self.crawl_limit),
            },
            search_audit={
                "query": query,
                "query_intent": query_intent,
                "requested_limit": max_results,
                "start": start,
                "result_urls": [record.url for record in records],
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return records
