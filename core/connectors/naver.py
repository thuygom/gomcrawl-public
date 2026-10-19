from __future__ import annotations

import time

import httpx

from ..credentials import secret_for_env
from ..schema import EvidenceRecord
from ..usage import record_api_usage
from ..utils import strip_html
from .base import require_secret


class NaverSearchClient:
    endpoint_map = {
        "naver_web": "webkr",
        "naver_news": "news",
    }

    def __init__(self, provider: str) -> None:
        if provider not in self.endpoint_map:
            raise ValueError(f"unsupported Naver provider: {provider}")
        self.provider = provider
        self.client_id = require_secret(secret_for_env("NAVER_CLIENT_ID"), "NAVER_CLIENT_ID")
        self.client_secret = require_secret(secret_for_env("NAVER_CLIENT_SECRET"), "NAVER_CLIENT_SECRET")
        self.timeout = 20.0

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        started = time.monotonic()
        display = max(1, min(limit, 100))
        endpoint = self.endpoint_map[self.provider]
        url = f"https://openapi.naver.com/v1/search/{endpoint}.json"
        headers = {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
        }
        response: httpx.Response | None = None
        try:
            params: dict[str, object] = {"query": query, "display": display, "start": start}
            if self.provider == "naver_news" and query_intent == "noteapp_complete:news":
                params["sort"] = "date"
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                response = client.get(url, params=params, headers=headers)
                response.raise_for_status()
                payload = response.json()
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
                    "requested_limit": display,
                    "start": start,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            raise

        records: list[EvidenceRecord] = []
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            title = strip_html(str(item.get("title") or ""))
            link = str(item.get("originallink") or item.get("link") or "").strip()
            if not link:
                continue
            snippet = strip_html(str(item.get("description") or ""))
            published_at = item.get("pubDate") or item.get("pubdate")
            records.append(
                EvidenceRecord.build(
                    source=self.provider,
                    source_type="search_engine_result",
                    title=title or link,
                    url=link,
                    snippet=snippet,
                    raw_payload_json=item,
                    published_at=str(published_at) if published_at else None,
                    query=query,
                    query_intent=query_intent,
                    confidence=0.35,
                    confidence_reason="Naver search metadata; verify against source URL before final use",
                )
            )
        record_api_usage(
            self.provider,
            "search",
            ok=True,
            status_code=response.status_code,
            response_headers=response.headers,
            result_count=len(records),
            search_audit={
                "query": query,
                "query_intent": query_intent,
                "requested_limit": display,
                "start": start,
                "result_urls": [record.url for record in records],
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return records
