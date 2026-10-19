from __future__ import annotations

from dataclasses import dataclass, field

from .crawlers import create_crawler
from .schema import CrawlResult, EvidenceRecord
from .search import create_search_client


@dataclass(slots=True)
class EnrichmentResult:
    query: str
    provider: str
    crawler: str
    search_results: list[EvidenceRecord] = field(default_factory=list)
    crawl_results: list[CrawlResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "provider": self.provider,
            "crawler": self.crawler,
            "search_results": [record.to_dict() for record in self.search_results],
            "crawl_results": [result.to_dict() for result in self.crawl_results],
            "crawl_evidence": [result.to_evidence(query=self.query).to_dict() for result in self.crawl_results],
        }


def search_and_crawl(
    query: str,
    *,
    provider: str,
    limit: int = 5,
    crawler_name: str = "auto",
    query_intent: str | None = None,
    respect_robots: bool = True,
    allow_selenium_fallback: bool = False,
    base_delay_seconds: float = 2.0,
    government_delay_seconds: float = 10.0,
) -> EnrichmentResult:
    search_client = create_search_client(provider)
    crawler = create_crawler(
        crawler_name,
        respect_robots=respect_robots,
        allow_selenium_fallback=allow_selenium_fallback,
        base_delay_seconds=base_delay_seconds,
        government_delay_seconds=government_delay_seconds,
    )
    search_results = search_client.search(query, limit=limit, query_intent=query_intent)
    crawl_results: list[CrawlResult] = []
    seen: set[str] = set()
    for record in search_results:
        if record.canonical_url in seen:
            continue
        seen.add(record.canonical_url)
        crawl_results.append(crawler.crawl(record.url))
    return EnrichmentResult(
        query=query,
        provider=provider,
        crawler=crawler_name,
        search_results=search_results,
        crawl_results=crawl_results,
    )
