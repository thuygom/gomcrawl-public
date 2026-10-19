from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CrawlerToolSpec:
    name: str
    family: str
    description: str
    input_schema: dict[str, Any]
    capabilities: tuple[str, ...]
    required_environment: tuple[str, ...] = ()
    required_dependencies: tuple[str, ...] = ()
    implementation: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_mcp_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["inputSchema"] = payload.pop("input_schema")
        return payload


COMMON_LOCAL_PARAMS: dict[str, Any] = {
    "url": {
        "type": "string",
        "format": "uri",
        "description": "Public HTTP/HTTPS URL to crawl.",
    },
    "respect_robots": {
        "type": "boolean",
        "default": True,
        "description": "Honor robots.txt allow/disallow rules before crawling.",
    },
    "base_delay_seconds": {
        "type": "number",
        "minimum": 0,
        "default": 2.0,
        "description": "Base polite delay for local-direct requests to the same host.",
    },
    "government_delay_seconds": {
        "type": "number",
        "minimum": 0,
        "default": 10.0,
        "description": "Minimum local-direct delay for .gov, .go.kr, and .gov.kr hosts.",
    },
    "emit_evidence": {
        "type": "boolean",
        "default": False,
        "description": "Return normalized EvidenceRecord instead of raw CrawlResult.",
    },
    "query": {
        "type": "string",
        "description": "Optional source query to attach when emit_evidence is true.",
    },
    "query_intent": {
        "type": "string",
        "description": "Optional evidence query intent such as company_profile, news, or recruiting.",
    },
}


CRAWLER_TOOL_SPECS: tuple[CrawlerToolSpec, ...] = (
    CrawlerToolSpec(
        name="web_search_static_crawl",
        family="static",
        description="Fetch a public static HTML/RSS/sitemap-like page with httpx and extract main text with BeautifulSoup4.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **COMMON_LOCAL_PARAMS,
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 20,
                    "description": "HTTP timeout for the direct fetch.",
                },
            },
            "required": ["url"],
        },
        capabilities=(
            "direct HTTP GET",
            "HTML title extraction",
            "main text extraction",
            "robots.txt allow/disallow check",
            "robots Crawl-delay support",
            "per-host jittered delay",
            "Retry-After and cooldown handling",
            "CrawlResult or EvidenceRecord output",
        ),
        required_dependencies=("httpx", "beautifulsoup4"),
        implementation={
            "gateway": {
                "method": "POST",
                "url": "http://127.0.0.1:9101/api/crawl",
                "crawler": "static",
                "observability": "centralized through /api/usage and /observability",
            },
            "python": "core.crawlers.StaticSoupCrawler",
            "cli": "python -m core.cli crawl <url> --crawler static",
            "local_debug_cli": "python -m core.cli crawl <url> --crawler static --local",
        },
        output={
            "default": "CrawlResult",
            "optional": "EvidenceRecord when emit_evidence=true",
        },
        policy={
            "crawler_engine": "local_direct",
            "public_pages_only": True,
            "login_or_captcha_bypass": False,
            "local_target_politeness": True,
        },
    ),
    CrawlerToolSpec(
        name="web_search_browser_crawl",
        family="browser",
        description="Render a public page with Playwright, Playwright Stealth, or Selenium when static HTML is not enough.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **COMMON_LOCAL_PARAMS,
                "engine": {
                    "type": "string",
                    "enum": ["playwright", "playwright_stealth", "selenium"],
                    "default": "playwright",
                    "description": "Browser automation backend.",
                },
                "browser": {
                    "type": "string",
                    "enum": ["chromium", "firefox", "webkit"],
                    "default": "chromium",
                    "description": "Playwright browser type. Ignored by Selenium.",
                },
                "screenshot_path": {
                    "type": "string",
                    "description": "Optional path for a Playwright full-page screenshot.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1000,
                    "default": 30000,
                    "description": "Browser navigation timeout in milliseconds.",
                },
            },
            "required": ["url"],
        },
        capabilities=(
            "JavaScript-rendered page extraction",
            "Playwright Chromium/Firefox/WebKit support",
            "optional playwright-stealth context patches",
            "Selenium Chrome fallback",
            "optional full-page screenshot with Playwright",
            "robots.txt allow/disallow check",
            "robots Crawl-delay support",
            "per-host jittered delay",
            "Retry-After and cooldown handling when response metadata is available",
            "CrawlResult or EvidenceRecord output",
        ),
        required_dependencies=("playwright, optional playwright-stealth, or selenium", "beautifulsoup4"),
        implementation={
            "gateway": {
                "method": "POST",
                "url": "http://127.0.0.1:9101/api/crawl",
                "crawler": "playwright, playwright_stealth, or selenium",
                "observability": "centralized through /api/usage and /observability",
            },
            "python": [
                "core.crawlers.PlaywrightCrawler",
                "core.crawlers.PlaywrightStealthCrawler",
                "core.crawlers.SeleniumCrawler",
            ],
            "cli": [
                "python -m core.cli crawl <url> --crawler playwright",
                "python -m core.cli crawl <url> --crawler playwright_stealth",
                "python -m core.cli crawl <url> --crawler selenium",
            ],
            "local_debug_cli": [
                "python -m core.cli crawl <url> --crawler playwright --local",
                "python -m core.cli crawl <url> --crawler playwright_stealth --local",
                "python -m core.cli crawl <url> --crawler selenium --local",
            ],
        },
        output={
            "default": "CrawlResult",
            "optional": "EvidenceRecord when emit_evidence=true",
        },
        policy={
            "crawler_engine": "local_direct_browser",
            "public_pages_only": True,
            "login_or_captcha_bypass": False,
            "session_automation": False,
            "local_target_politeness": True,
        },
        notes=(
            "Use browser crawl only when static crawl is insufficient.",
            "Selenium is a compatibility fallback, not the default path.",
        ),
    ),
    CrawlerToolSpec(
        name="web_search_managed_firecrawl_crawl",
        family="managed_remote",
        description="Use Firecrawl API/CLI to scrape a public page into markdown through a managed remote crawler.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "url": {
                    "type": "string",
                    "format": "uri",
                    "description": "Public HTTP/HTTPS URL to scrape with Firecrawl.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 180,
                    "description": "Local timeout for the Firecrawl CLI command.",
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown"],
                    "default": "markdown",
                    "description": "Current CLI wrapper output format.",
                },
                "only_main_content": {
                    "type": "boolean",
                    "default": True,
                    "description": "Request main-content extraction from Firecrawl CLI.",
                },
                "emit_evidence": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return normalized EvidenceRecord instead of raw CrawlResult.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional source query to attach when emit_evidence is true.",
                },
                "query_intent": {
                    "type": "string",
                    "description": "Optional evidence query intent.",
                },
            },
            "required": ["url"],
        },
        capabilities=(
            "managed remote scrape",
            "markdown extraction",
            "main-content extraction",
            "Firecrawl API-key authenticated access",
            "CrawlResult or EvidenceRecord output",
            "target robots/rate behavior delegated to Firecrawl",
        ),
        required_environment=("FIRECRAWL_API_KEY",),
        required_dependencies=("firecrawl CLI",),
        implementation={
            "gateway": {
                "method": "POST",
                "url": "http://127.0.0.1:9101/api/crawl",
                "crawler": "firecrawl",
                "observability": "tool call is centralized; Firecrawl account credits are tracked separately when visible",
            },
            "python": "core.crawlers.FirecrawlCrawler",
            "cli": "python -m core.cli crawl <url> --crawler firecrawl",
            "local_debug_cli": "python -m core.cli crawl <url> --crawler firecrawl --local",
        },
        output={
            "default": "CrawlResult",
            "optional": "EvidenceRecord when emit_evidence=true",
        },
        policy={
            "crawler_engine": "managed_remote",
            "public_pages_only": True,
            "login_or_captcha_bypass": False,
            "local_target_politeness": False,
            "target_politeness": "delegated_to_firecrawl",
            "must_track": ["api_429_retry_after", "credits", "robotsBlocked", "retention_policy"],
        },
        notes=(
            "Local base-delay/government-delay settings do not throttle the target site for Firecrawl.",
            "Future REST/SDK implementation should expose Firecrawl-native delay, maxConcurrency, and retention controls.",
        ),
    ),
    CrawlerToolSpec(
        name="web_search_managed_watercrawl_crawl",
        family="managed_remote",
        description="Use a configured WaterCrawl API service to run an async crawl job and return normalized crawl content.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "url": {
                    "type": "string",
                    "format": "uri",
                    "description": "Public HTTP/HTTPS URL to crawl with WaterCrawl.",
                },
                "emit_evidence": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return normalized EvidenceRecord instead of raw CrawlResult.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional source query to attach when emit_evidence is true.",
                },
                "query_intent": {
                    "type": "string",
                    "description": "Optional evidence query intent.",
                },
            },
            "required": ["url"],
        },
        capabilities=(
            "managed async crawl job",
            "WaterCrawl REST API integration",
            "markdown/html result normalization",
            "CrawlResult or EvidenceRecord output",
            "target robots/rate behavior delegated to WaterCrawl",
        ),
        required_environment=("WATERCRAWL_API_URL", "WATERCRAWL_API_KEY"),
        required_dependencies=("WaterCrawl API service",),
        implementation={
            "gateway": {
                "method": "POST",
                "url": "http://127.0.0.1:9101/api/crawl",
                "crawler": "watercrawl",
                "observability": "gateway call is centralized; WaterCrawl API usage is tracked when visible",
            },
            "python": "core.crawlers.WatercrawlCrawler",
            "cli": "python -m core.cli crawl <url> --crawler watercrawl",
            "local_debug_cli": "python -m core.cli crawl <url> --crawler watercrawl --local",
        },
        output={
            "default": "CrawlResult",
            "optional": "EvidenceRecord when emit_evidence=true",
        },
        policy={
            "crawler_engine": "managed_remote",
            "public_pages_only": True,
            "login_or_captcha_bypass": False,
            "local_target_politeness": False,
            "target_politeness": "delegated_to_watercrawl",
            "must_track": ["api_url", "api_key", "service_robots_policy", "credits_or_quota", "retention_policy"],
        },
        notes=(
            "Set WATERCRAWL_API_URL to the WaterCrawl base URL, for example a self-hosted deployment.",
            "The wrapper uses X-API-Key and polls /api/v1/core crawl request status.",
        ),
    ),
    CrawlerToolSpec(
        name="web_search_stealth_browser_crawl",
        family="browser",
        description="Use the stealth-browser multi-engine router for difficult public JS/TLS-fingerprint pages.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **COMMON_LOCAL_PARAMS,
                "vendor_hint": {
                    "type": "string",
                    "enum": ["cloudflare", "cloudflare-turnstile", "datadome", "perimeterx", "akamai", "imperva", "kasada", "fingerprint-test"],
                    "description": "Optional STEALTH_BROWSER_VENDOR_HINT equivalent.",
                },
                "screenshot_path": {
                    "type": "string",
                    "description": "Optional output path for a base64 screenshot returned by the selected engine.",
                },
            },
            "required": ["url"],
        },
        capabilities=(
            "multi-engine router",
            "nodriver Chromium path",
            "curl_cffi TLS impersonation path when installed",
            "camoufox Firefox path when installed",
            "robots.txt allow/disallow check",
            "per-host jittered delay",
            "CrawlResult or EvidenceRecord output",
        ),
        required_dependencies=("stealth-browser package or external/stealth-browser clone", "nodriver/curl_cffi/camoufox optional engines"),
        implementation={
            "gateway": {
                "method": "POST",
                "url": "http://127.0.0.1:9101/api/crawl",
                "crawler": "stealth_browser",
                "observability": "centralized through /api/usage and /observability",
            },
            "python": "core.crawlers.StealthBrowserCrawler",
            "cli": "python -m core.cli crawl <url> --crawler stealth_browser",
            "local_debug_cli": "python -m core.cli crawl <url> --crawler stealth_browser --local",
        },
        output={
            "default": "CrawlResult",
            "optional": "EvidenceRecord when emit_evidence=true",
        },
        policy={
            "crawler_engine": "local_direct_stealth_browser",
            "public_pages_only": True,
            "login_or_captcha_bypass": False,
            "session_automation": False,
            "local_target_politeness": True,
        },
        notes=(
            "Use this only when static or normal browser crawl is insufficient.",
            "Runtime selection can be tuned with STEALTH_BROWSER_VENDOR_HINT and related STEALTH_BROWSER_* env vars.",
        ),
    ),
    CrawlerToolSpec(
        name="web_search_managed_apify_linkedin_crawl",
        family="managed_remote",
        description="Run a configured Apify Actor against one explicit public LinkedIn profile/company URL.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "url": {
                    "type": "string",
                    "format": "uri",
                    "description": "Public LinkedIn /in/, /company/, /school/, or /showcase/ URL.",
                },
                "emit_evidence": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return normalized EvidenceRecord instead of raw CrawlResult.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional source query to attach when emit_evidence is true.",
                },
                "query_intent": {
                    "type": "string",
                    "description": "Optional evidence query intent.",
                },
            },
            "required": ["url"],
        },
        capabilities=(
            "managed remote Actor run",
            "public LinkedIn URL scrape",
            "profile/company/school/showcase URL allowlist",
            "contact-like field redaction",
            "CrawlResult or EvidenceRecord output",
            "target robots/rate behavior delegated to configured Apify Actor",
        ),
        required_environment=("APIFY_API_TOKEN",),
        required_dependencies=(
            "Default harvestapi/linkedin-profile-scraper and harvestapi/linkedin-company Actors, or Actor ID overrides",
        ),
        implementation={
            "gateway": {
                "method": "POST",
                "url": "http://127.0.0.1:9101/api/crawl",
                "crawler": "apify_linkedin",
                "observability": "gateway call is centralized; Apify account usage is tracked separately when the Actor call executes",
            },
            "python": "core.crawlers.ApifyLinkedInCrawler",
            "cli": "python -m core.cli crawl <linkedin-url> --crawler apify_linkedin",
            "local_debug_cli": "python -m core.cli crawl <linkedin-url> --crawler apify_linkedin --local",
        },
        output={
            "default": "CrawlResult",
            "optional": "EvidenceRecord when emit_evidence=true",
        },
        policy={
            "crawler_engine": "managed_remote",
            "public_pages_only": True,
            "explicit_url_required": True,
            "broad_people_search": False,
            "email_or_contact_lookup": False,
            "login_or_captcha_bypass": False,
            "local_target_politeness": False,
            "target_politeness": "delegated_to_configured_apify_actor",
            "must_track": ["actor_id", "actor_terms", "credits_or_monthly_usage", "retention_policy"],
        },
        notes=(
            "/in/ uses harvestapi/linkedin-profile-scraper; /company/ uses harvestapi/linkedin-company.",
            "Override them with APIFY_LINKEDIN_ACTOR_ID and APIFY_LINKEDIN_COMPANY_ACTOR_ID when needed.",
        ),
    ),
)


def available_crawler_tools(family: str | None = None) -> list[dict[str, Any]]:
    normalized = family.strip().lower() if family else None
    tools = CRAWLER_TOOL_SPECS
    if normalized:
        tools = tuple(tool for tool in tools if tool.family == normalized)
    return [tool.to_mcp_dict() for tool in tools]
