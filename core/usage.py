from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_USAGE_DIR = PROJECT_ROOT / ".run" / "usage"
DEFAULT_SEARCH_AUDIT_DIR = PROJECT_ROOT / ".run" / "search_audit"
_WRITE_LOCK = threading.Lock()
_SITE_PATTERN = re.compile(r"(?:^|\s)site:([^\s]+)", re.IGNORECASE)

LIMIT_HEADER_NAMES = (
    "x-rate-limit",
    "x-ratelimit-limit",
    "ratelimit-limit",
    "x-quota-limit",
    "x-quota-limit-limit",
)
REMAINING_HEADER_NAMES = (
    "x-rate-limit-remaining",
    "x-ratelimit-remaining",
    "ratelimit-remaining",
    "x-quota-remaining",
)
RESET_HEADER_NAMES = (
    "x-rate-limit-reset",
    "x-ratelimit-reset",
    "ratelimit-reset",
)
QUOTA_SCOPE_BY_PROVIDER = {
    "naver_web": "naver_search_openapi",
    "naver_news": "naver_search_openapi",
    "naver_book": "naver_search_openapi",
    "firecrawl": "firecrawl_account",
    "firecrawl_search": "firecrawl_account",
    "watercrawl": "watercrawl_account",
    "watercrawl_search": "watercrawl_account",
    "apify_linkedin": "apify_account",
    "apify_linkedin_profile_search": "apify_account",
    "serper": "serper_account",
    "openai_confidence": "openai_account",
    "workflow_linkedin_profile_finder": "gateway_workflows",
    "workflow_person_evidence_finder": "gateway_workflows",
    "workflow_noteapp_complete_news": "gateway_workflows",
    "workflow_noteapp_complete_publication": "gateway_workflows",
    "workflow_noteapp_complete_award": "gateway_workflows",
    "workflow_noteapp_complete_social": "gateway_workflows",
    "workflow_noteapp_complete_profile": "gateway_workflows",
    "workflow_noteapp_complete_company-homepage": "gateway_workflows",
    "crawler_auto": "gateway_crawl_tools",
    "crawler_static": "gateway_crawl_tools",
    "crawler_playwright": "gateway_crawl_tools",
    "crawler_selenium": "gateway_crawl_tools",
    "crawler_firecrawl": "gateway_crawl_tools",
    "crawler_watercrawl": "gateway_crawl_tools",
    "crawler_stealth_browser": "gateway_crawl_tools",
    "crawler_playwright_firecrawl": "gateway_crawl_tools",
    "crawler_apify_linkedin": "gateway_crawl_tools",
    "bot_detection": "gateway_crawl_tools",
}
DEFAULT_DAILY_LIMIT_BY_SCOPE = {
    "naver_search_openapi": 25_000,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def usage_dir() -> Path:
    return Path(os.getenv("WEB_SEARCH_USAGE_DIR", str(DEFAULT_USAGE_DIR))).expanduser()


def usage_enabled() -> bool:
    return os.getenv("WEB_SEARCH_USAGE_DISABLED", "").strip().lower() not in {"1", "true", "yes", "on"}


def search_audit_dir() -> Path:
    return Path(os.getenv("WEB_SEARCH_AUDIT_DIR", str(DEFAULT_SEARCH_AUDIT_DIR))).expanduser()


def search_audit_enabled() -> bool:
    return os.getenv("WEB_SEARCH_AUDIT_DISABLED", "").strip().lower() not in {"1", "true", "yes", "on"}


def normalize_provider_env(provider: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in provider.upper())


def quota_scope_for_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    return QUOTA_SCOPE_BY_PROVIDER.get(normalized, normalized)


def configured_daily_limit(provider: str) -> int | None:
    names = (
        f"WEB_SEARCH_DAILY_LIMIT_{normalize_provider_env(provider)}",
        f"WEB_SEARCH_LIMIT_{normalize_provider_env(provider)}",
    )
    for name in names:
        value = os.getenv(name, "").strip()
        if not value:
            continue
        try:
            parsed = int(value)
        except ValueError:
            continue
        return parsed if parsed >= 0 else None
    return DEFAULT_DAILY_LIMIT_BY_SCOPE.get(provider.strip().lower())


def parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_epoch_reset(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    try:
        numeric = float(raw)
    except ValueError:
        return None
    if numeric > 1_000_000_000_000:
        seconds = numeric / 1000.0
    elif numeric > 1_000_000_000:
        seconds = numeric
    else:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize_headers(headers: Mapping[str, object] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in dict(headers).items()}


def first_header(headers: Mapping[str, str], names: Iterable[str]) -> str | None:
    for name in names:
        value = headers.get(name)
        if value is not None and value != "":
            return value
    return None


def quota_snapshot_from_headers(headers: Mapping[str, object] | None) -> dict[str, object]:
    normalized = normalize_headers(headers)
    limit_raw = first_header(normalized, LIMIT_HEADER_NAMES)
    remaining_raw = first_header(normalized, REMAINING_HEADER_NAMES)
    reset_raw = first_header(normalized, RESET_HEADER_NAMES)
    retry_after_raw = normalized.get("retry-after")

    snapshot: dict[str, object] = {}
    if limit_raw is not None:
        snapshot["limit"] = parse_int(limit_raw) if parse_int(limit_raw) is not None else limit_raw
    if remaining_raw is not None:
        snapshot["remaining"] = parse_int(remaining_raw) if parse_int(remaining_raw) is not None else remaining_raw
    if reset_raw is not None:
        snapshot["reset_raw"] = reset_raw
        reset_at = parse_epoch_reset(reset_raw)
        if reset_at:
            snapshot["reset_at"] = reset_at
    if retry_after_raw is not None:
        snapshot["retry_after"] = parse_int(retry_after_raw) if parse_int(retry_after_raw) is not None else retry_after_raw
    return snapshot


def sanitize_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    if not metadata:
        return {}
    allowed = {
        "error_type",
        "returncode",
        "transport",
        "managed_service",
        "model",
        "item_count",
        "endpoint",
        "category_count",
        "best_match_found",
        "best_match_score",
        "mode",
        "discovery_provider",
        "crawler",
        "discovered_count",
        "crawled_count",
        "operation_parent",
        "reason",
        "shutdown_error",
        "remaining_chrome_processes",
        "completed_since_recycle",
        "pid_threads",
        "chrome_processes",
        "max_pid_threads",
        "max_chrome_processes",
    }
    clean: dict[str, object] = {}
    for key, value in metadata.items():
        if key not in allowed:
            continue
        if value is None:
            continue
        clean[key] = value
    return clean


def record_api_usage(
    provider: str,
    operation: str,
    *,
    ok: bool,
    status_code: int | None = None,
    response_headers: Mapping[str, object] | None = None,
    result_count: int | None = None,
    units: int = 1,
    metadata: Mapping[str, object] | None = None,
    search_audit: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    if not usage_enabled():
        return None
    timestamp = utc_now()
    if search_audit:
        record_search_audit(
            provider,
            operation,
            query=str(search_audit.get("query") or ""),
            ok=ok,
            timestamp=timestamp,
            status_code=status_code,
            result_count=result_count,
            units=units,
            result_urls=search_audit.get("result_urls"),
            query_intent=str(search_audit.get("query_intent") or ""),
            requested_limit=parse_int(search_audit.get("requested_limit")),
            start=parse_int(search_audit.get("start")),
            duration_ms=parse_int(search_audit.get("duration_ms")),
            error_type=str((metadata or {}).get("error_type") or ""),
            source=str(search_audit.get("source") or "runtime"),
        )
    event: dict[str, object] = {
        "schema_version": "web_search_usage_event/v1",
        "timestamp": timestamp.isoformat(),
        "date": timestamp.date().isoformat(),
        "provider": provider,
        "quota_scope": quota_scope_for_provider(provider),
        "operation": operation,
        "ok": bool(ok),
        "units": max(1, int(units)),
    }
    if status_code is not None:
        event["status_code"] = int(status_code)
    if result_count is not None:
        event["result_count"] = max(0, int(result_count))
    quota = quota_snapshot_from_headers(response_headers)
    if quota:
        event["quota"] = quota
    clean_metadata = sanitize_metadata(metadata)
    if clean_metadata:
        event["metadata"] = clean_metadata

    directory = usage_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{timestamp.date().isoformat()}.jsonl"
    _append_private_jsonl(path, event)
    return event


def _append_private_jsonl(path: Path, event: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: one process-wide lock is enough until write throughput requires per-file locks.
    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        path.chmod(0o600)


def _clean_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").lower()
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path or "/", "", ""))


def _host(value: object) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").lower()
    except ValueError:
        return ""


def query_fingerprint(query: str) -> str:
    normalized = " ".join(query.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def record_search_audit(
    provider: str,
    operation: str,
    *,
    query: str,
    ok: bool | None,
    timestamp: datetime | None = None,
    status_code: int | None = None,
    result_count: int | None = None,
    units: int = 1,
    result_urls: object = None,
    query_intent: str = "",
    requested_limit: int | None = None,
    start: int | None = None,
    duration_ms: int | None = None,
    error_type: str = "",
    source: str = "runtime",
    source_event_id: str = "",
) -> dict[str, object] | None:
    if not search_audit_enabled():
        return None
    when = timestamp or utc_now()
    cleaned_urls = []
    if isinstance(result_urls, (list, tuple, set)):
        cleaned_urls = [url for value in result_urls if (url := _clean_url(value))]
    target_domains = sorted(
        {
            domain.lower().strip(".")
            for domain in _SITE_PATTERN.findall(query)
            if domain and "/" not in domain
        }
    )
    event: dict[str, object] = {
        "schema_version": "web_search_audit_event/v1",
        "timestamp": when.isoformat(),
        "date": when.date().isoformat(),
        "provider": provider,
        "quota_scope": quota_scope_for_provider(provider),
        "operation": operation,
        "ok": ok,
        "units": max(1, int(units)),
        "query": query[:1000],
        "query_fingerprint": query_fingerprint(query),
        "query_intent": query_intent[:160],
        "target_domains": target_domains,
        "result_urls": cleaned_urls[:100],
        "result_domains": sorted({host for value in cleaned_urls if (host := _host(value))}),
        "source": source[:80],
    }
    optional = {
        "status_code": status_code,
        "result_count": result_count,
        "requested_limit": requested_limit,
        "start": start,
        "duration_ms": duration_ms,
        "error_type": error_type[:120] if error_type else None,
        "source_event_id": source_event_id or None,
    }
    for key, value in optional.items():
        if value is not None and value != "":
            event[key] = max(0, int(value)) if key in {"result_count", "requested_limit", "start", "duration_ms"} else value
    path = search_audit_dir() / f"{when.date().isoformat()}.jsonl"
    _append_private_jsonl(path, event)
    return event


def usage_files(days: int) -> list[Path]:
    today = utc_now().date()
    safe_days = max(1, min(int(days), 366))
    return [usage_dir() / f"{(today - timedelta(days=offset)).isoformat()}.jsonl" for offset in range(safe_days)]


def iter_usage_events(*, days: int = 1, provider: str | None = None) -> Iterable[dict[str, object]]:
    normalized_provider = provider.strip().lower() if provider else None
    for path in usage_files(days):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_provider = str(event.get("provider") or "").lower()
                if normalized_provider and event_provider != normalized_provider:
                    continue
                yield event


def search_audit_files(days: int) -> list[Path]:
    today = utc_now().date()
    safe_days = max(1, min(int(days), 366))
    return [search_audit_dir() / f"{(today - timedelta(days=offset)).isoformat()}.jsonl" for offset in range(safe_days)]


def iter_search_audit_events(*, days: int = 1, provider: str | None = None) -> Iterable[dict[str, object]]:
    normalized_provider = provider.strip().lower() if provider else None
    for path in search_audit_files(days):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if normalized_provider and str(event.get("provider") or "").lower() != normalized_provider:
                    continue
                yield event


def summarize_search_audit(*, days: int = 1, provider: str | None = None, limit: int = 100) -> dict[str, object]:
    # ponytail: scan JSONL directly; move to SQLite only when retention makes scrape latency measurable.
    safe_days = max(1, min(int(days), 366))
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "provider": "",
            "event_count": 0,
            "success_count": 0,
            "error_count": 0,
            "unknown_count": 0,
            "result_count": 0,
            "duration_ms_sum": 0,
            "duration_ms_count": 0,
        }
    )
    target_domains: dict[str, int] = defaultdict(int)
    result_domains: dict[str, int] = defaultdict(int)
    recent = []
    for event in iter_search_audit_events(days=safe_days, provider=provider):
        event_provider = str(event.get("provider") or "unknown")
        row = grouped[event_provider]
        row["provider"] = event_provider
        row["event_count"] += 1
        if event.get("ok") is True:
            row["success_count"] += 1
        elif event.get("ok") is False:
            row["error_count"] += 1
        else:
            row["unknown_count"] += 1
        row["result_count"] += int(event.get("result_count") or 0)
        duration_ms = parse_int(event.get("duration_ms"))
        if duration_ms is not None:
            row["duration_ms_sum"] += duration_ms
            row["duration_ms_count"] += 1
        for domain in event.get("target_domains") or []:
            target_domains[str(domain)] += 1
        for domain in event.get("result_domains") or []:
            result_domains[str(domain)] += 1
        recent.append(
            {
                key: event.get(key)
                for key in (
                    "timestamp",
                    "provider",
                    "query_fingerprint",
                    "query_intent",
                    "target_domains",
                    "result_domains",
                    "requested_limit",
                    "result_count",
                    "duration_ms",
                    "status_code",
                    "ok",
                    "source",
                )
                if event.get(key) is not None
            }
        )
    providers = []
    for _, row in sorted(grouped.items()):
        duration_count = int(row["duration_ms_count"])
        providers.append(
            {
                **row,
                "average_duration_ms": round(int(row["duration_ms_sum"]) / duration_count, 2) if duration_count else None,
            }
        )
    recent.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    safe_limit = max(1, min(int(limit), 500))
    return {
        "schema_version": "web_search_audit_summary/v1",
        "generated_at": utc_now().isoformat(),
        "days": safe_days,
        "private_detail_logged": search_audit_enabled(),
        "sensitive_values_exposed": False,
        "providers": providers,
        "target_domains": [{"domain": key, "count": value} for key, value in sorted(target_domains.items(), key=lambda item: (-item[1], item[0]))],
        "result_domains": [{"domain": key, "count": value} for key, value in sorted(result_domains.items(), key=lambda item: (-item[1], item[0]))],
        "recent": recent[:safe_limit],
    }


def summarize_usage(*, days: int = 1, provider: str | None = None) -> dict[str, object]:
    safe_days = max(1, min(int(days), 366))
    today = utc_now().date()
    today_key = today.isoformat()
    date_keys = [(today - timedelta(days=offset)).isoformat() for offset in range(safe_days - 1, -1, -1)]
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "provider": "",
            "call_count": 0,
            "success_count": 0,
            "error_count": 0,
            "units": 0,
            "result_count": 0,
            "today_units": 0,
            "operations": defaultdict(int),
            "last_seen_at": None,
            "last_status_code": None,
            "last_quota": None,
        }
    )
    quota_grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "quota_scope": "",
            "providers": set(),
            "call_count": 0,
            "success_count": 0,
            "error_count": 0,
            "units": 0,
            "today_units": 0,
            "last_seen_at": None,
            "last_quota": None,
        }
    )
    daily: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(
            lambda: {
                "calls": 0,
                "success": 0,
                "errors": 0,
                "units": 0,
                "last_seen_at": None,
                "last_quota": None,
            }
        )
    )
    quota_daily: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(
            lambda: {
                "calls": 0,
                "success": 0,
                "errors": 0,
                "units": 0,
                "providers": set(),
                "last_seen_at": None,
                "last_quota": None,
            }
        )
    )

    for event in iter_usage_events(days=safe_days, provider=provider):
        event_provider = str(event.get("provider") or "unknown")
        quota_scope = str(event.get("quota_scope") or quota_scope_for_provider(event_provider))
        row = grouped[event_provider]
        row["provider"] = event_provider
        row["quota_scope"] = quota_scope
        row["call_count"] += 1
        units = int(event.get("units") or 1)
        row["units"] += units
        row["result_count"] += int(event.get("result_count") or 0)
        if event.get("date") == today_key:
            row["today_units"] += units
        if event.get("ok"):
            row["success_count"] += 1
        else:
            row["error_count"] += 1
        row["operations"][str(event.get("operation") or "unknown")] += 1
        timestamp = str(event.get("timestamp") or "")
        event_date = str(event.get("date") or "") or timestamp[:10]
        day = daily[event_provider][event_date]
        day["calls"] += 1
        day["units"] += units
        if event.get("ok"):
            day["success"] += 1
        else:
            day["errors"] += 1
        if timestamp and (day["last_seen_at"] is None or timestamp > str(day["last_seen_at"])):
            day["last_seen_at"] = timestamp
            day["last_quota"] = event.get("quota")
        if timestamp and (row["last_seen_at"] is None or timestamp > str(row["last_seen_at"])):
            row["last_seen_at"] = timestamp
            row["last_status_code"] = event.get("status_code")
            row["last_quota"] = event.get("quota")

        quota_row = quota_grouped[quota_scope]
        quota_row["quota_scope"] = quota_scope
        quota_row["providers"].add(event_provider)
        quota_row["call_count"] += 1
        quota_row["units"] += units
        if event.get("date") == today_key:
            quota_row["today_units"] += units
        if event.get("ok"):
            quota_row["success_count"] += 1
        else:
            quota_row["error_count"] += 1
        if timestamp and (quota_row["last_seen_at"] is None or timestamp > str(quota_row["last_seen_at"])):
            quota_row["last_seen_at"] = timestamp
            quota_row["last_quota"] = event.get("quota")

        quota_day = quota_daily[quota_scope][event_date]
        quota_day["providers"].add(event_provider)
        quota_day["calls"] += 1
        quota_day["units"] += units
        if event.get("ok"):
            quota_day["success"] += 1
        else:
            quota_day["errors"] += 1
        if timestamp and (quota_day["last_seen_at"] is None or timestamp > str(quota_day["last_seen_at"])):
            quota_day["last_seen_at"] = timestamp
            quota_day["last_quota"] = event.get("quota")

    providers: list[dict[str, object]] = []
    series: dict[str, list[dict[str, object]]] = {}
    for event_provider, row in sorted(grouped.items()):
        limit = configured_daily_limit(event_provider)
        payload: dict[str, object] = {
            "provider": event_provider,
            "quota_scope": row.get("quota_scope") or quota_scope_for_provider(event_provider),
            "call_count": row["call_count"],
            "success_count": row["success_count"],
            "error_count": row["error_count"],
            "units": row["units"],
            "result_count": row["result_count"],
            "today_units": row["today_units"],
            "operations": dict(row["operations"]),
            "last_seen_at": row["last_seen_at"],
            "last_status_code": row["last_status_code"],
            "last_quota": row["last_quota"],
            "configured_daily_limit": limit,
            "configured_daily_remaining": max(0, limit - int(row["today_units"])) if limit is not None else None,
        }
        providers.append(payload)
        provider_points: list[dict[str, object]] = []
        for date_key in date_keys:
            day = daily[event_provider][date_key]
            quota = day["last_quota"] if isinstance(day["last_quota"], dict) else {}
            quota_limit = quota.get("limit")
            quota_remaining = quota.get("remaining")
            point: dict[str, object] = {
                "date": date_key,
                "calls": int(day["calls"]),
                "success": int(day["success"]),
                "errors": int(day["errors"]),
                "units": int(day["units"]),
                "quota_limit": quota_limit,
                "quota_remaining": quota_remaining,
                "configured_daily_limit": limit,
                "configured_daily_remaining": max(0, limit - int(day["units"])) if limit is not None else None,
                "last_seen_at": day["last_seen_at"],
            }
            provider_points.append(point)
        series[event_provider] = provider_points

    quota_scopes: list[dict[str, object]] = []
    quota_series: dict[str, list[dict[str, object]]] = {}
    for quota_scope, row in sorted(quota_grouped.items()):
        limit = configured_daily_limit(quota_scope)
        scope_payload: dict[str, object] = {
            "quota_scope": quota_scope,
            "providers": sorted(row["providers"]),
            "call_count": row["call_count"],
            "success_count": row["success_count"],
            "error_count": row["error_count"],
            "units": row["units"],
            "today_units": row["today_units"],
            "last_seen_at": row["last_seen_at"],
            "last_quota": row["last_quota"],
            "configured_daily_limit": limit,
            "configured_daily_remaining": max(0, limit - int(row["today_units"])) if limit is not None else None,
        }
        quota_scopes.append(scope_payload)
        scope_points: list[dict[str, object]] = []
        for date_key in date_keys:
            day = quota_daily[quota_scope][date_key]
            quota = day["last_quota"] if isinstance(day["last_quota"], dict) else {}
            scope_points.append(
                {
                    "date": date_key,
                    "calls": int(day["calls"]),
                    "success": int(day["success"]),
                    "errors": int(day["errors"]),
                    "units": int(day["units"]),
                    "providers": sorted(day["providers"]),
                    "quota_limit": quota.get("limit"),
                    "quota_remaining": quota.get("remaining"),
                    "configured_daily_limit": limit,
                    "configured_daily_remaining": max(0, limit - int(day["units"])) if limit is not None else None,
                    "last_seen_at": day["last_seen_at"],
                }
            )
        quota_series[quota_scope] = scope_points

    overall = []
    for date_key in date_keys:
        calls = sum(int(daily[event_provider][date_key]["calls"]) for event_provider in grouped)
        units = sum(int(daily[event_provider][date_key]["units"]) for event_provider in grouped)
        errors = sum(int(daily[event_provider][date_key]["errors"]) for event_provider in grouped)
        overall.append({"date": date_key, "calls": calls, "units": units, "errors": errors})

    return {
        "schema_version": "web_search_usage_summary/v1",
        "generated_at": utc_now().isoformat(),
        "days": safe_days,
        "usage_dir": str(usage_dir()),
        "query_or_url_values_logged": False,
        "providers": providers,
        "quota_scopes": quota_scopes,
        "series": series,
        "quota_series": quota_series,
        "overall_series": overall,
    }


def _metric_labels(**labels: object) -> str:
    escaped = []
    for key, value in labels.items():
        text = str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        escaped.append(f'{key}="{text}"')
    return "{" + ",".join(escaped) + "}" if escaped else ""


def _timestamp_seconds(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def render_prometheus_metrics(
    usage: Mapping[str, object],
    audit: Mapping[str, object],
    provider_rows: Iterable[Mapping[str, object]] = (),
) -> str:
    lines = [
        "# HELP web_search_agent_provider_calls_total Provider API calls in the retained usage ledger.",
        "# TYPE web_search_agent_provider_calls_total counter",
        "# HELP web_search_agent_provider_errors_total Provider API errors in the retained usage ledger.",
        "# TYPE web_search_agent_provider_errors_total counter",
        "# HELP web_search_agent_provider_units_total Provider quota units in the retained usage ledger.",
        "# TYPE web_search_agent_provider_units_total counter",
        "# HELP web_search_agent_provider_operation_calls_total Provider API calls grouped by operation.",
        "# TYPE web_search_agent_provider_operation_calls_total counter",
        "# HELP web_search_agent_provider_results_total Search results returned by provider.",
        "# TYPE web_search_agent_provider_results_total counter",
        "# HELP web_search_agent_provider_ready Whether required provider credentials are configured.",
        "# TYPE web_search_agent_provider_ready gauge",
        "# HELP web_search_agent_provider_last_seen_timestamp_seconds Last provider call timestamp.",
        "# TYPE web_search_agent_provider_last_seen_timestamp_seconds gauge",
        "# HELP web_search_agent_provider_daily_limit Configured local daily quota limit.",
        "# TYPE web_search_agent_provider_daily_limit gauge",
        "# HELP web_search_agent_provider_daily_remaining Configured local daily quota remaining.",
        "# TYPE web_search_agent_provider_daily_remaining gauge",
        "# HELP web_search_agent_search_duration_milliseconds Search duration from the private audit ledger.",
        "# TYPE web_search_agent_search_duration_milliseconds summary",
    ]
    for raw in usage.get("providers", []) if isinstance(usage.get("providers"), list) else []:
        if not isinstance(raw, Mapping):
            continue
        provider = str(raw.get("provider") or "unknown")
        scope = str(raw.get("quota_scope") or quota_scope_for_provider(provider))
        labels = _metric_labels(provider=provider, quota_scope=scope)
        lines.extend(
            [
                f"web_search_agent_provider_calls_total{labels} {int(raw.get('call_count') or 0)}",
                f"web_search_agent_provider_errors_total{labels} {int(raw.get('error_count') or 0)}",
                f"web_search_agent_provider_units_total{labels} {int(raw.get('units') or 0)}",
                f"web_search_agent_provider_results_total{labels} {int(raw.get('result_count') or 0)}",
                f"web_search_agent_provider_last_seen_timestamp_seconds{labels} {_timestamp_seconds(raw.get('last_seen_at'))}",
            ]
        )
        for operation, count in dict(raw.get("operations") or {}).items():
            lines.append(
                f"web_search_agent_provider_operation_calls_total{_metric_labels(provider=provider, operation=operation)} {int(count)}"
            )
        if raw.get("configured_daily_limit") is not None:
            lines.append(f"web_search_agent_provider_daily_limit{labels} {int(raw['configured_daily_limit'])}")
        if raw.get("configured_daily_remaining") is not None:
            lines.append(f"web_search_agent_provider_daily_remaining{labels} {int(raw['configured_daily_remaining'])}")
    for raw in audit.get("providers", []) if isinstance(audit.get("providers"), list) else []:
        if not isinstance(raw, Mapping):
            continue
        labels = _metric_labels(provider=raw.get("provider") or "unknown")
        lines.append(f"web_search_agent_search_duration_milliseconds_sum{labels} {int(raw.get('duration_ms_sum') or 0)}")
        lines.append(f"web_search_agent_search_duration_milliseconds_count{labels} {int(raw.get('duration_ms_count') or 0)}")
    for raw in provider_rows:
        provider = str(raw.get("provider") or "")
        if provider:
            lines.append(f"web_search_agent_provider_ready{_metric_labels(provider=provider)} {1 if raw.get('ready') else 0}")
    return "\n".join(lines) + "\n"
