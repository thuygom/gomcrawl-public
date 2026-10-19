from __future__ import annotations

import time

import httpx

from ..credentials import secret_for_env
from ..schema import EvidenceRecord
from ..usage import record_api_usage
from ..utils import strip_html
from .base import require_secret


class YouTubeSearchClient:
    provider = "youtube"

    def __init__(self) -> None:
        self.api_key = require_secret(secret_for_env("YOUTUBE_API_KEY"), "YOUTUBE_API_KEY")

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        started = time.monotonic()
        params = {
            "key": self.api_key,
            "part": "snippet",
            "q": query,
            "maxResults": max(1, min(limit, 50)),
            "type": "video,channel",
        }
        response: httpx.Response | None = None
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                response = client.get("https://www.googleapis.com/youtube/v3/search", params=params)
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
                    "requested_limit": params["maxResults"],
                    "start": start,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            raise

        records = []
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            ident = item.get("id") or {}
            video_id = ident.get("videoId")
            channel_id = ident.get("channelId") or snippet.get("channelId")
            if video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
            elif channel_id:
                url = f"https://www.youtube.com/channel/{channel_id}"
            else:
                continue
            records.append(
                EvidenceRecord.build(
                    source=self.provider,
                    source_type="official_api",
                    title=strip_html(str(snippet.get("title") or url)),
                    url=url,
                    snippet=strip_html(str(snippet.get("description") or "")),
                    raw_payload_json=item,
                    published_at=snippet.get("publishedAt"),
                    query=query,
                    query_intent=query_intent,
                    confidence=0.35,
                    confidence_reason="YouTube search result; enrich with videos.list before factual use",
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
                "requested_limit": params["maxResults"],
                "start": start,
                "result_urls": [record.url for record in records],
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return records
