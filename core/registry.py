from __future__ import annotations

from dataclasses import asdict, dataclass

from .credentials import key_file_candidate_strings


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider: str
    priority: str
    source_type: str
    allowed_targets: tuple[str, ...]
    required_env: tuple[str, ...] = ()
    quota_note: str = ""
    policy_note: str = ""
    bulk_discovery: bool = False

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["env"] = ", ".join(self.required_env)
        payload["credential_file_candidates"] = {
            env_name: key_file_candidate_strings(env_name)
            for env_name in self.required_env
        }
        return payload


@dataclass(frozen=True, slots=True)
class CrawlerSpec:
    crawler: str
    use: str
    engine_type: str = "local_direct"
    obeys_robots: bool = True
    local_target_politeness: bool = True
    same_domain_concurrency: int = 1
    default_delay_seconds: float = 2.0
    required_env: tuple[str, ...] = ()
    policy_note: str = ""

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["credential_file_candidates"] = {
            env_name: key_file_candidate_strings(env_name)
            for env_name in self.required_env
        }
        return payload


SEARCH_PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        provider="naver_web",
        priority="P1",
        source_type="search_engine_result",
        allowed_targets=("person_profile", "company_profile", "business_card_context"),
        required_env=("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"),
        quota_note="display<=100, start<=1000; daily API quota must be managed outside this adapter.",
        policy_note="Snippet is candidate evidence only; verify against source URL.",
        bulk_discovery=True,
    ),
    ProviderSpec(
        provider="naver_news",
        priority="P1",
        source_type="search_engine_result",
        allowed_targets=("person_profile", "company_profile", "news"),
        required_env=("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"),
        quota_note="display<=100, start<=1000; daily API quota must be managed outside this adapter.",
        policy_note="News metadata is not final evidence without source verification.",
        bulk_discovery=True,
    ),
    ProviderSpec(
        provider="youtube",
        priority="P2",
        source_type="official_api",
        allowed_targets=("person_profile", "company_profile", "social", "talks"),
        required_env=("YOUTUBE_API_KEY",),
        quota_note="Search calls are tightly budgeted; enrich candidates with videos.list.",
        policy_note="Auxiliary evidence only.",
        bulk_discovery=False,
    ),
    ProviderSpec(
        provider="firecrawl_search",
        priority="P1/P2 crawl-like",
        source_type="search_engine_result",
        allowed_targets=("company_profile", "public_web_discovery"),
        required_env=("FIRECRAWL_API_KEY",),
        quota_note="Credit-based; search generally costs more than scrape/map.",
        policy_note="Public, non-sensitive pages only; check retention policy before bulk use.",
        bulk_discovery=False,
    ),
    ProviderSpec(
        provider="watercrawl_search",
        priority="P1/P2 crawl-like",
        source_type="search_then_crawl",
        allowed_targets=("company_profile", "public_web_discovery", "news"),
        required_env=("SERPER_API_KEY", "WATERCRAWL_API_URL", "WATERCRAWL_API_KEY"),
        quota_note="Uses Serper credits for URL discovery and WaterCrawl service quota for top-result crawls.",
        policy_note="Serper discovers public URLs; WaterCrawl crawls selected pages. Verify source content before final evidence use.",
        bulk_discovery=False,
    ),
    ProviderSpec(
        provider="serper",
        priority="P1/P2",
        source_type="search_engine_result",
        allowed_targets=("company_profile", "person_profile", "public_web_discovery", "news"),
        required_env=("SERPER_API_KEY",),
        quota_note="Credit-based Google SERP API; successful responses include a credits field captured as usage units.",
        policy_note="Google SERP metadata through Serper; verify against source URL before final use.",
        bulk_discovery=True,
    ),
    ProviderSpec(
        provider="apify_linkedin_profile_search",
        priority="P2 controlled",
        source_type="social_profile_result",
        allowed_targets=("person_profile", "public_linkedin_profile"),
        required_env=("APIFY_API_TOKEN",),
        quota_note="Apify Actor run cost applies; default flow uses harvestapi/linkedin-profile-search-by-name Short mode, then one selected URL is enriched by harvestapi/linkedin-profile-scraper.",
        policy_note="Managed Apify LinkedIn profile search by name. Email is not sent to the Actor; use downstream identity scoring for email/company checks.",
        bulk_discovery=False,
    ),
)


CRAWLER_SPECS: tuple[CrawlerSpec, ...] = (
    CrawlerSpec(
        crawler="auto",
        use="static HTML first, then stealth-browser, then managed Firecrawl only after a local failure",
        engine_type="hybrid_composite",
        default_delay_seconds=2.0,
        policy_note="Stops at the first usable result and never uses managed fallback for a detected block page.",
    ),
    CrawlerSpec(
        crawler="static",
        use="BeautifulSoup4/httpx for static HTML, RSS, sitemap-like pages",
        default_delay_seconds=2.0,
        policy_note="Best default for public pages; obey robots and per-host delay.",
    ),
    CrawlerSpec(
        crawler="playwright",
        use="SPA/public JS-rendered pages via Chromium/Firefox/WebKit",
        default_delay_seconds=5.0,
        policy_note="Public pages only; no login, CAPTCHA, or session automation.",
    ),
    CrawlerSpec(
        crawler="playwright_stealth",
        use="Playwright plus playwright-stealth context patches for explicit public URLs",
        engine_type="local_direct_playwright_stealth",
        default_delay_seconds=5.0,
        policy_note="Public pages only; obeys robots and fails closed on login/CAPTCHA-shaped URLs.",
    ),
    CrawlerSpec(
        crawler="selenium",
        use="legacy browser compatibility fallback",
        default_delay_seconds=5.0,
        policy_note="Compatibility fallback only, not the default crawler.",
    ),
    CrawlerSpec(
        crawler="firecrawl",
        use="PoC markdown extraction through Firecrawl CLI/API credits",
        engine_type="managed_remote",
        local_target_politeness=False,
        default_delay_seconds=2.0,
        policy_note="Target crawling is delegated to Firecrawl; manage API key, credits, rate limits, robotsBlocked, and retention policy.",
    ),
    CrawlerSpec(
        crawler="watercrawl",
        use="WaterCrawl REST API crawl job for public pages through a configured self-hosted/cloud service",
        engine_type="managed_remote",
        local_target_politeness=False,
        default_delay_seconds=2.0,
        required_env=("WATERCRAWL_API_URL", "WATERCRAWL_API_KEY"),
        policy_note="Target crawling is delegated to WaterCrawl; configure service-side concurrency, robots, retention, and API quotas.",
    ),
    CrawlerSpec(
        crawler="stealth_browser",
        use="local multi-engine stealth-browser router for difficult public JS/TLS-fingerprint pages",
        engine_type="local_direct_stealth_browser",
        default_delay_seconds=5.0,
        policy_note="Public pages only; still uses local robots/polite gate and refuses login/CAPTCHA-shaped URLs.",
    ),
    CrawlerSpec(
        crawler="apify_linkedin",
        use="URL-based public LinkedIn profile scrape through the default HarvestAPI Actor; custom Actors remain supported",
        engine_type="managed_remote",
        obeys_robots=False,
        local_target_politeness=False,
        default_delay_seconds=2.0,
        required_env=("APIFY_API_TOKEN",),
        policy_note="The default Actor handles public profile crawling; custom company/school Actors can be configured separately. Contact fields are redacted.",
    ),
)


def get_provider_spec(provider: str) -> ProviderSpec | None:
    normalized = provider.strip().lower()
    for spec in SEARCH_PROVIDER_SPECS:
        if spec.provider == normalized:
            return spec
    return None


def available_search_providers() -> list[dict[str, object]]:
    return [spec.to_dict() for spec in SEARCH_PROVIDER_SPECS]


def available_crawlers() -> list[dict[str, object]]:
    return [spec.to_dict() for spec in CRAWLER_SPECS]
