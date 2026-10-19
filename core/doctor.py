from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .credentials import binary_candidates, resolve_binary, resolve_credential
from .registry import CRAWLER_SPECS, SEARCH_PROVIDER_SPECS


def dependency_status(import_name: str, label: str | None = None) -> dict[str, object]:
    return {
        "name": label or import_name,
        "import": import_name,
        "present": importlib.util.find_spec(import_name) is not None,
    }


def repo_status(relative_path: str) -> dict[str, object]:
    path = Path(__file__).resolve().parents[1] / relative_path
    return {
        "path": relative_path,
        "present": path.exists(),
        "absolute_path": str(path),
    }


def binary_status(name: str) -> dict[str, object]:
    path = resolve_binary(name)
    return {
        "name": name,
        "present": bool(path),
        "path": path,
        "candidate_paths": binary_candidates(name),
    }


def credential_status(env_name: str) -> dict[str, object]:
    return resolve_credential(env_name).to_dict()


def provider_credentials_report() -> list[dict[str, object]]:
    report = []
    for spec in SEARCH_PROVIDER_SPECS:
        credentials = [credential_status(env_name) for env_name in spec.required_env]
        report.append(
            {
                "provider": spec.provider,
                "priority": spec.priority,
                "credentials": credentials,
                "ready": all(item["present"] for item in credentials) if credentials else True,
                "policy_note": spec.policy_note,
                "quota_note": spec.quota_note,
            }
        )
    return report


def crawler_credentials_report() -> list[dict[str, object]]:
    report = []
    for spec in CRAWLER_SPECS:
        credentials = [credential_status(env_name) for env_name in spec.required_env]
        report.append(
            {
                "crawler": spec.crawler,
                "credentials": credentials,
                "ready": all(item["present"] for item in credentials) if credentials else True,
                "policy_note": spec.policy_note,
            }
        )
    return report


def llm_confidence_report() -> dict[str, object]:
    credentials = [credential_status("OPENAI_API_KEY")]
    return {
        "service": "openai_confidence",
        "credentials": credentials,
        "ready": all(item["present"] for item in credentials),
        "credential_values_printed": False,
    }


def playwright_browser_status(browser: str = "chromium") -> dict[str, object]:
    if importlib.util.find_spec("playwright") is None:
        return {"browser": browser, "present": False, "status": "playwright package missing"}
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser_type = getattr(playwright, browser)
            executable = Path(browser_type.executable_path)
            return {
                "browser": browser,
                "present": executable.exists(),
                "path": str(executable),
                "status": "ok" if executable.exists() else "browser executable missing",
            }
    except Exception as exc:
        return {"browser": browser, "present": False, "status": str(exc)}


def build_doctor_report() -> dict[str, Any]:
    return {
        "providers": provider_credentials_report(),
        "crawler_credentials": crawler_credentials_report(),
        "dependencies": [
            dependency_status("bs4", "beautifulsoup4"),
            dependency_status("httpx"),
            dependency_status("playwright"),
            dependency_status("playwright_stealth", "playwright-stealth"),
            dependency_status("selenium"),
            dependency_status("stealth_browser", "stealth-browser"),
        ],
        "external_repositories": [
            repo_status("external/watercrawl"),
            repo_status("external/stealth-browser"),
        ],
        "binaries": [
            binary_status("firecrawl"),
        ],
        "playwright_browsers": [
            playwright_browser_status("chromium"),
        ],
        "crawlers": [spec.to_dict() for spec in CRAWLER_SPECS],
        "llm_confidence": llm_confidence_report(),
        "privacy": {
            "credential_values_printed": False,
            "contact_identity_hardcoded": False,
        },
    }
