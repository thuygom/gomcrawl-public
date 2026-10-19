from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from .schema import CrawlResult
from .usage import record_api_usage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENT_DIR = PROJECT_ROOT / ".run" / "bot_detection"
_WRITE_LOCK = threading.Lock()


def _detect_block(*, title: str, html: str, url: str) -> object:
    from ._vendor.stealth_detect import detect_block
    return detect_block(title=title, html=html, url=url)


def crawl_result_has_block_signal(result: CrawlResult) -> bool:
    body = result.html or result.markdown or result.text or result.error or ""
    detection = _detect_block(title=result.title or result.error or "", html=body, url=result.url)
    return bool(getattr(detection, "blocked", False)) or result.status_code in {403, 429, 503}


def event_dir() -> Path:
    return Path(os.getenv("WEB_SEARCH_BOT_DETECTION_DIR", str(DEFAULT_EVENT_DIR))).expanduser()


def _safe_token(value: object, default: str = "unknown") -> str:
    clean = re.sub(r"[^a-z0-9_-]+", "_", str(value or "").strip().lower()).strip("_")
    return clean[:60] or default


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "unknown").lower()
    except ValueError:
        return "unknown"


def _write_event(event: dict[str, object]) -> None:
    directory = event_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{str(event['timestamp'])[:10]}.jsonl"
    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        path.chmod(0o600)


def observe_crawl_result(result: CrawlResult, *, requested_crawler: str) -> dict[str, object]:
    body = result.html or result.markdown or result.text or result.error or ""
    detection = _detect_block(title=result.title or result.error or "", html=body, url=result.url)
    detected = bool(getattr(detection, "blocked", False))
    http_block = result.status_code in {403, 429}
    has_content = bool(result.title or result.html or result.markdown or result.text)
    if detected:
        status = "blocked"
    elif http_block:
        status = "http_block"
    elif has_content:
        status = "clear"
    else:
        status = "not_inspected"
    vendor = _safe_token(getattr(detection, "vendor", None), "unknown")
    severity = _safe_token(getattr(detection, "severity", None), "none")
    event: dict[str, object] = {
        "schema_version": "web_search_bot_detection/v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "detected": detected,
        "vendor": vendor,
        "severity": severity,
        "behavioral": bool(getattr(detection, "is_behavioral", False)),
        "crawler": _safe_token(requested_crawler),
        "actual_crawler": _safe_token(result.crawler),
        "host": _hostname(result.url),
        "status_code": result.status_code,
        "error_type": "crawl_error" if result.error else None,
    }
    problem = detected or http_block
    _write_event(event)
    operation = f"blocked_{vendor}_{severity}" if detected else status
    record_api_usage(
        "bot_detection",
        operation,
        ok=not problem,
        status_code=result.status_code,
        result_count=1 if detected else 0,
        metadata={
            "crawler": event["crawler"],
            "mode": status,
            "error_type": event["error_type"],
        },
    )
    public = {
        key: event[key]
        for key in (
            "status",
            "detected",
            "vendor",
            "severity",
            "behavioral",
            "crawler",
            "actual_crawler",
            "host",
            "status_code",
        )
    }
    result.raw_payload["bot_detection"] = public
    if detected and not result.error:
        result.error = f"bot challenge detected: {vendor}"
    return public


def observe_crawl_exception(url: str, *, requested_crawler: str, error: Exception) -> None:
    event = {
        "schema_version": "web_search_bot_detection/v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "not_inspected",
        "detected": False,
        "vendor": "unknown",
        "severity": "none",
        "behavioral": False,
        "crawler": _safe_token(requested_crawler),
        "actual_crawler": "unknown",
        "host": _hostname(url),
        "status_code": None,
        "error_type": type(error).__name__,
    }
    _write_event(event)
    record_api_usage(
        "bot_detection",
        "not_inspected",
        ok=True,
        result_count=0,
        metadata={"crawler": event["crawler"], "mode": "not_inspected", "error_type": type(error).__name__},
    )


def _event_files(days: int) -> Iterable[Path]:
    today = datetime.now(timezone.utc).date()
    for offset in range(max(1, min(days, 366))):
        yield event_dir() / f"{(today - timedelta(days=offset)).isoformat()}.jsonl"


def summarize_bot_detections(*, days: int = 1, limit: int = 100) -> dict[str, object]:
    events: list[dict[str, object]] = []
    for path in _event_files(days):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    events.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return {
        "schema_version": "web_search_bot_detection_summary/v1",
        "days": max(1, min(days, 366)),
        "check_count": len(events),
        "detected_count": sum(bool(item.get("detected")) for item in events),
        "problem_count": sum(item.get("status") in {"blocked", "http_block"} for item in events),
        "recent": events[: max(1, min(limit, 500))],
    }
