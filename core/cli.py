from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .code_inspect import inspect_paths
from .crawlers import available_crawlers, create_crawler
from .doctor import build_doctor_report
from .gateway import crawl_result_from_payload, gateway_crawl, gateway_search, should_use_gateway
from .pipeline import search_and_crawl
from .search import SearchAgentError, available_search_providers, create_search_client
from .tools import available_crawler_tools
from .usage import summarize_usage


def write_payload(payload: Any, output: str | None = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text + "\n", encoding="utf-8")
        return
    print(text)


def cmd_providers(args: argparse.Namespace) -> int:
    write_payload({"search": available_search_providers(), "crawlers": available_crawlers()}, args.output)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    write_payload(build_doctor_report(), args.output)
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    write_payload(
        {
            "schema_version": "mcp-tool-spec-list/v1",
            "agent_id": "web_search_agent",
            "tools": available_crawler_tools(args.family),
        },
        args.output,
    )
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    write_payload(summarize_usage(days=args.days, provider=args.provider), args.output)
    return 0


def cmd_code_inspect(args: argparse.Namespace) -> int:
    write_payload(inspect_paths(args.paths, level=args.level), args.output)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if should_use_gateway(args.local):
        payload = gateway_search(
            args.query,
            provider=args.provider,
            limit=args.limit,
            start=args.start,
            intent=args.intent,
        )
        write_payload(payload if args.gateway_raw else payload.get("records", []), args.output)
        return 0
    client = create_search_client(args.provider)
    records = client.search(args.query, limit=args.limit, start=args.start, query_intent=args.intent)
    write_payload([record.to_dict() for record in records], args.output)
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    if should_use_gateway(args.local):
        if args.screenshot:
            raise SearchAgentError("gateway crawl does not accept screenshot paths; use --local for screenshot capture")
        payload = gateway_crawl(
            args.url,
            crawler=args.crawler,
            respect_robots=not args.ignore_robots,
            browser=args.browser,
            allow_selenium_fallback=args.allow_selenium_fallback,
            base_delay_seconds=args.base_delay,
            government_delay_seconds=args.government_delay,
        )
        result_payload = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if args.evidence:
            result = crawl_result_from_payload(result_payload)
            write_payload(result.to_evidence(query=args.query, query_intent=args.intent).to_dict(), args.output)
        else:
            write_payload(result_payload, args.output)
        return 0 if not result_payload.get("error") else 2

    crawler = create_crawler(
        args.crawler,
        respect_robots=not args.ignore_robots,
        browser=args.browser,
        allow_selenium_fallback=args.allow_selenium_fallback,
        screenshot_path=args.screenshot,
        base_delay_seconds=args.base_delay,
        government_delay_seconds=args.government_delay,
    )
    result = crawler.crawl(args.url)
    if args.evidence:
        write_payload(result.to_evidence(query=args.query, query_intent=args.intent).to_dict(), args.output)
    else:
        write_payload(result.to_dict(), args.output)
    return 0 if not result.error else 2


def cmd_enrich(args: argparse.Namespace) -> int:
    if should_use_gateway(args.local):
        search_payload = gateway_search(
            args.query,
            provider=args.provider,
            limit=args.limit,
            intent=args.intent,
        )
        search_results = list(search_payload.get("records", [])) if isinstance(search_payload.get("records"), list) else []
        crawl_results: list[dict[str, Any]] = []
        crawl_evidence: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in search_results:
            if not isinstance(record, dict):
                continue
            url = str(record.get("url") or "")
            canonical = str(record.get("canonical_url") or url)
            if not url or canonical in seen:
                continue
            seen.add(canonical)
            crawl_payload = gateway_crawl(
                url,
                crawler=args.crawler,
                respect_robots=not args.ignore_robots,
                allow_selenium_fallback=args.allow_selenium_fallback,
                base_delay_seconds=args.base_delay,
                government_delay_seconds=args.government_delay,
            )
            result_payload = crawl_payload.get("result") if isinstance(crawl_payload.get("result"), dict) else {}
            crawl_results.append(result_payload)
            crawl_evidence.append(
                crawl_result_from_payload(result_payload).to_evidence(query=args.query, query_intent=args.intent).to_dict()
            )
        write_payload(
            {
                "query": args.query,
                "provider": args.provider,
                "crawler": args.crawler,
                "gateway": True,
                "search_results": search_results,
                "crawl_results": crawl_results,
                "crawl_evidence": crawl_evidence,
            },
            args.output,
        )
        return 0

    result = search_and_crawl(
        args.query,
        provider=args.provider,
        limit=args.limit,
        crawler_name=args.crawler,
        query_intent=args.intent,
        respect_robots=not args.ignore_robots,
        allow_selenium_fallback=args.allow_selenium_fallback,
        base_delay_seconds=args.base_delay,
        government_delay_seconds=args.government_delay,
    )
    write_payload(result.to_dict(), args.output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="web-search-agent")
    parser.add_argument("--output", "-o", help="Write JSON output to this path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    providers = subparsers.add_parser("providers", help="List configured search and crawler adapters")
    providers.set_defaults(func=cmd_providers)

    doctor = subparsers.add_parser("doctor", help="Check credentials, optional dependencies, and crawler policy")
    doctor.set_defaults(func=cmd_doctor)

    tools = subparsers.add_parser("tools", help="List MCP-style crawler tool specs")
    tools.add_argument("--family", choices=["static", "browser", "managed_remote"], help="Filter crawler tool family")
    tools.set_defaults(func=cmd_tools)

    usage = subparsers.add_parser("usage", help="Summarize local provider usage and captured rate-limit headers")
    usage.add_argument("--days", type=int, default=1, help="Number of UTC date files to include")
    usage.add_argument("--provider", help="Filter by provider")
    usage.set_defaults(func=cmd_usage)

    code_inspect = subparsers.add_parser("code-inspect", help="Run static code inspection on local files or directories")
    code_inspect.add_argument("paths", nargs="+", help="Files or directories to inspect")
    code_inspect.add_argument("--level", default="medium", choices=["low", "medium", "high", "하", "중", "상"], help="Inspection depth")
    code_inspect.set_defaults(func=cmd_code_inspect)

    search = subparsers.add_parser("search", help="Run one search API adapter")
    search.add_argument("query")
    search.add_argument("--provider", default="naver_web")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--start", type=int, default=1)
    search.add_argument("--intent", help="query_intent value for evidence records")
    search.add_argument("--local", action="store_true", help="Bypass the central gateway and call the connector directly")
    search.add_argument("--gateway-raw", action="store_true", help="When using the gateway, emit the full gateway response wrapper")
    search.set_defaults(func=cmd_search)

    crawl = subparsers.add_parser("crawl", help="Crawl one public URL")
    crawl.add_argument("url")
    crawl.add_argument("--crawler", default="auto", choices=["auto", "static", "playwright", "playwright_stealth", "selenium", "firecrawl", "watercrawl", "stealth_browser", "apify_linkedin"])
    crawl.add_argument("--browser", default="chromium", choices=["chromium", "firefox", "webkit"])
    crawl.add_argument("--ignore-robots", action="store_true", help="Disable robots.txt check for internal tests only")
    crawl.add_argument("--allow-selenium-fallback", action="store_true")
    crawl.add_argument("--base-delay", type=float, default=2.0, help="Base polite delay for local-direct crawler host requests")
    crawl.add_argument("--government-delay", type=float, default=10.0, help="Minimum local-direct polite delay for .gov/.go.kr/.gov.kr hosts")
    crawl.add_argument("--screenshot", help="Path for Playwright full-page screenshot")
    crawl.add_argument("--evidence", action="store_true", help="Emit normalized EvidenceRecord instead of CrawlResult")
    crawl.add_argument("--query", help="Optional source query to attach when --evidence is used")
    crawl.add_argument("--intent", help="Optional query_intent to attach when --evidence is used")
    crawl.add_argument("--local", action="store_true", help="Bypass the central gateway and call the crawler directly")
    crawl.set_defaults(func=cmd_crawl)

    enrich = subparsers.add_parser("enrich", help="Search then crawl result URLs")
    enrich.add_argument("query")
    enrich.add_argument("--provider", default="naver_web")
    enrich.add_argument("--limit", type=int, default=5)
    enrich.add_argument("--crawler", default="auto", choices=["auto", "static", "playwright", "playwright_stealth", "selenium", "firecrawl", "watercrawl", "stealth_browser", "apify_linkedin"])
    enrich.add_argument("--intent", help="query_intent value for evidence records")
    enrich.add_argument("--ignore-robots", action="store_true", help="Disable robots.txt check for internal tests only")
    enrich.add_argument("--allow-selenium-fallback", action="store_true")
    enrich.add_argument("--base-delay", type=float, default=2.0, help="Base polite delay for local-direct crawler host requests")
    enrich.add_argument("--government-delay", type=float, default=10.0, help="Minimum local-direct polite delay for .gov/.go.kr/.gov.kr hosts")
    enrich.add_argument("--local", action="store_true", help="Bypass the central gateway and run search/crawl in this process")
    enrich.set_defaults(func=cmd_enrich)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SearchAgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
