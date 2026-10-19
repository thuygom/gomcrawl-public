from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ..credentials import resolve_binary, secret_for_env
from ..schema import EvidenceRecord
from ..usage import record_api_usage
from ..utils import strip_html
from .base import SearchAgentError, require_secret


ROOT = Path(__file__).resolve().parents[2]


def find_firecrawl_web_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        candidates = [
            (payload.get("data") or {}).get("web") if isinstance(payload.get("data"), dict) else None,
            payload.get("web"),
            payload.get("results"),
            payload.get("data"),
        ]
        for candidate in candidates:
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


class FirecrawlSearchClient:
    provider = "firecrawl_search"

    def __init__(self, *, timeout: float = 120) -> None:
        self.api_key = require_secret(secret_for_env("FIRECRAWL_API_KEY"), "FIRECRAWL_API_KEY")
        self.binary = resolve_binary("firecrawl")
        self.timeout = timeout
        if not self.binary:
            raise SearchAgentError("firecrawl CLI is missing")

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        started = time.monotonic()
        requested_limit = max(1, min(limit, 50))
        env = os.environ.copy()
        env["FIRECRAWL_API_KEY"] = self.api_key
        with tempfile.TemporaryDirectory(prefix="web-search-agent-firecrawl-") as tmp:
            output_path = Path(tmp) / "search.json"
            cmd = [
                self.binary or "firecrawl",
                "search",
                query,
                "--limit",
                str(requested_limit),
                "--sources",
                "web",
                "--json",
                "-o",
                str(output_path),
            ]
            try:
                result = subprocess.run(
                    cmd,
                    cwd=str(ROOT),
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=self.timeout,
                )
            except Exception as exc:
                record_api_usage(
                    self.provider,
                    "search",
                    ok=False,
                    metadata={"error_type": type(exc).__name__, "managed_service": "firecrawl"},
                    search_audit={
                        "query": query,
                        "query_intent": query_intent,
                        "requested_limit": requested_limit,
                        "start": start,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
                raise
            if result.returncode != 0:
                record_api_usage(
                    self.provider,
                    "search",
                    ok=False,
                    metadata={"returncode": result.returncode, "managed_service": "firecrawl"},
                    search_audit={
                        "query": query,
                        "query_intent": query_intent,
                        "requested_limit": requested_limit,
                        "start": start,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
                raise SearchAgentError(f"firecrawl search failed: {result.stderr.strip() or result.stdout.strip()}")
            if output_path.exists():
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            elif result.stdout.strip() and result.stdout.strip().startswith(("{", "[")):
                payload = json.loads(result.stdout)
            else:
                payload = {"success": True, "data": {"web": []}, "message": result.stdout.strip()}

        web_results = find_firecrawl_web_results(payload)
        records = []
        for item in web_results:
            url = str(item.get("url") or item.get("link") or "").strip()
            if not url:
                continue
            records.append(
                EvidenceRecord.build(
                    source=self.provider,
                    source_type="search_engine_result",
                    title=strip_html(str(item.get("title") or item.get("name") or url)),
                    url=url,
                    snippet=strip_html(str(item.get("description") or item.get("snippet") or item.get("markdown") or ""))[:2000],
                    raw_payload_json=item,
                    published_at=item.get("publishedDate") or item.get("published_at"),
                    query=query,
                    query_intent=query_intent,
                    confidence=0.3,
                    confidence_reason="Firecrawl search metadata; check credits/retention before bulk use",
                )
            )
        record_api_usage(
            self.provider,
            "search",
            ok=True,
            result_count=len(records),
            metadata={"managed_service": "firecrawl"},
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
