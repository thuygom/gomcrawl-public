from __future__ import annotations

from .apify import ApifyLinkedInProfileSearchClient
from .base import SearchAgentError, SearchClient
from .firecrawl import FirecrawlSearchClient
from .naver import NaverSearchClient
from .serper import SerperSearchClient
from .watercrawl import WatercrawlSearchClient
from .youtube import YouTubeSearchClient


def create_search_client(provider: str) -> SearchClient:
    normalized = provider.strip().lower()
    if normalized in NaverSearchClient.endpoint_map:
        return NaverSearchClient(normalized)
    if normalized == YouTubeSearchClient.provider:
        return YouTubeSearchClient()
    if normalized == FirecrawlSearchClient.provider:
        return FirecrawlSearchClient()
    if normalized == WatercrawlSearchClient.provider:
        return WatercrawlSearchClient()
    if normalized == SerperSearchClient.provider:
        return SerperSearchClient()
    if normalized == ApifyLinkedInProfileSearchClient.provider:
        return ApifyLinkedInProfileSearchClient()
    raise SearchAgentError(f"unknown search provider: {provider}")
