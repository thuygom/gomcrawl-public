from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping

import httpx

from .credentials import secret_for_env
from .usage import record_api_usage

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-nano"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_OUTPUT_TOKENS = 1400
MAX_ITEM_TEXT_CHARS = 2200
MAX_REFERENCE_CHARS = 1200

RELATIONS = {"exact", "likely", "partial", "weak", "mismatch", "uncertain"}


CONFIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "confidence": {"type": "number"},
                    "confidence_reason": {"type": "string"},
                    "relation": {
                        "type": "string",
                        "enum": ["exact", "likely", "partial", "weak", "mismatch", "uncertain"],
                    },
                    "matched_signals": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "missing_or_uncertain": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "item_id",
                    "confidence",
                    "confidence_reason",
                    "relation",
                    "matched_signals",
                    "missing_or_uncertain",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

INSTRUCTIONS = """Score whether each observed search or crawl item refers to the same real-world person/company context as the reference.
The reference may include nullable Person fields (name, email, english_name, title, department, phone) and Company fields (company_name, ceo_name, business_registration_number, corporate_registration_number, founded_at, listed, industry, patents_certifications, address, main_products_services, homepage).
Use only the provided title, URL, snippet, text preview, provider metadata, raw payload excerpt, and reference fields.
Do not invent facts. Search snippets are candidate evidence, not verified facts.
Give high confidence only for strong identifiers such as exact email, phone, company registration numbers, official homepage/domain, or name plus company/job context.
Return conservative confidence from 0 to 1:
0.90-1.00 exact same person/company identity or official source strongly confirms it.
0.70-0.89 likely same identity with useful supporting evidence.
0.45-0.69 relevant but ambiguous, indirect, or metadata-only.
0.20-0.44 weak topical overlap or high namesake risk.
0.00-0.19 mismatch, irrelevant, or insufficient evidence.
Keep reasons short and specific."""

def llm_confidence_disabled() -> bool:
    return os.getenv("WEB_SEARCH_LLM_CONFIDENCE_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def openai_responses_url() -> str:
    return os.getenv("WEB_SEARCH_OPENAI_RESPONSES_URL", "").strip() or OPENAI_RESPONSES_URL


def llm_model() -> str:
    return (
        os.getenv("WEB_SEARCH_LLM_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or DEFAULT_MODEL
    )


def llm_timeout_seconds() -> float:
    raw = os.getenv("WEB_SEARCH_LLM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return max(1.0, min(float(raw), 60.0))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def llm_reasoning_effort() -> str:
    value = os.getenv("WEB_SEARCH_LLM_REASONING_EFFORT", "low").strip().lower()
    return value if value in {"minimal", "low", "medium", "high", "xhigh"} else "low"


def llm_verbosity() -> str:
    value = os.getenv("WEB_SEARCH_LLM_VERBOSITY", "low").strip().lower()
    return value if value in {"low", "medium", "high"} else "low"


def clean_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def compact_string(value: object, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def json_excerpt(value: object, max_chars: int = 1600) -> str:
    if value in (None, "", {}, []):
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return compact_string(text, max_chars)


def safe_string_list(value: object, *, max_items: int = 4) -> list[str]:
    if not isinstance(value, list):
        return []
    clean: list[str] = []
    for item in value[:max_items]:
        text = compact_string(item, 180)
        if text:
            clean.append(text)
    return clean


def safe_confidence(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(parsed, 1.0))


def safe_relation(value: object) -> str:
    relation = str(value or "").strip().lower()
    return relation if relation in RELATIONS else "uncertain"


def normalize_reference(reference: Mapping[str, object]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in reference.items():
        if value in (None, ""):
            continue
        if isinstance(value, (dict, list, tuple)):
            normalized[str(key)] = json_excerpt(value, MAX_REFERENCE_CHARS)
        else:
            normalized[str(key)] = compact_string(value, MAX_REFERENCE_CHARS)
    return normalized


def normalize_items(items: list[Mapping[str, object]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        item_id = compact_string(item.get("item_id") or item.get("result_id") or f"item-{index}", 160)
        normalized.append(
            {
                "item_id": item_id,
                "source": compact_string(item.get("source") or item.get("crawler"), 120),
                "source_type": compact_string(item.get("source_type") or item.get("kind"), 120),
                "title": compact_string(item.get("title"), 300),
                "url": compact_string(item.get("url") or item.get("canonical_url"), 700),
                "snippet": compact_string(item.get("snippet"), MAX_ITEM_TEXT_CHARS),
                "text_preview": compact_string(item.get("text_preview") or item.get("text"), MAX_ITEM_TEXT_CHARS),
                "published_at": compact_string(item.get("published_at"), 80),
                "raw_payload_excerpt": json_excerpt(item.get("raw_payload_json") or item.get("raw_payload")),
            }
        )
    return normalized


def model_supports_reasoning_config(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith("gpt-5") or normalized.startswith("o")


def build_responses_payload(model: str, reference: Mapping[str, object], items: list[Mapping[str, object]]) -> dict[str, Any]:
    text_config: dict[str, Any] = {
        "format": {
            "type": "json_schema",
            "name": "web_search_confidence_batch",
            "strict": True,
            "schema": CONFIDENCE_SCHEMA,
        }
    }
    if model.strip().lower().startswith("gpt-5"):
        text_config["verbosity"] = llm_verbosity()

    payload: dict[str, Any] = {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "reference": normalize_reference(reference),
                        "items": normalize_items(items),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        ],
        "text": text_config,
        "store": False,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    }
    if model_supports_reasoning_config(model):
        payload["reasoning"] = {"effort": llm_reasoning_effort()}
    return payload


def context_value(context: Mapping[str, object], names: tuple[str, ...]) -> object:
    sources = [context]
    for key in ("metadata", "meta", "usage_context"):
        child = context.get(key)
        if isinstance(child, Mapping):
            sources.append(child)
    normalized = {str(key).lower(): value for source in sources for key, value in source.items()}
    for name in names:
        value = normalized.get(name.lower())
        if value not in (None, "", [], {}):
            return value
    return None


def safe_header_value(value: object, limit: int = 128) -> str:
    text = "".join(ch for ch in str(value or "").strip() if ch.isprintable())
    return text[:limit]


def llm_usage_headers(context: Mapping[str, object] | None) -> dict[str, str]:
    if not context:
        return {}
    fields = {
        "X-Tenant-ID": ("tenant_id", "tenantId", "tenant", "company_id", "companyId"),
        "X-Tenant-Label": ("tenant_label", "tenantLabel", "tenant_name", "tenantName"),
        "X-User-ID": ("user_id", "userId", "user", "username", "email"),
        "X-User-Label": ("user_label", "userLabel", "user_name", "userName", "display_name", "displayName"),
        "X-Account-ID": ("account_id", "accountId", "account"),
        "X-LLM-Workflow": ("workflow", "mode", "client_mode"),
        "X-LLM-Purpose": ("llm_purpose",),
        "X-LLM-Source": ("llm_source",),
    }
    headers: dict[str, str] = {}
    for header, names in fields.items():
        value = safe_header_value(context_value(context, names))
        if value:
            headers[header] = value
    headers.setdefault("X-LLM-Source", "web_search_agent")
    headers.setdefault("X-LLM-Purpose", "confidence")
    return headers


def extract_response_text(payload: Mapping[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    parts: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for chunk in content:
                if not isinstance(chunk, dict):
                    continue
                text = chunk.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "\n".join(parts).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.startswith("json"):
            clean = clean[4:]
    parsed = json.loads(clean)
    if not isinstance(parsed, dict):
        raise ValueError("LLM confidence response was not a JSON object")
    return parsed


def safe_usage(payload: Mapping[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    clean: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            clean[key] = value
    return clean


def skipped_summary(status: str, reason: str, *, item_count: int, model: str | None = None) -> dict[str, object]:
    return {
        "enabled": False,
        "status": status,
        "model": model,
        "item_count": item_count,
        "items_scored": 0,
        "elapsed_seconds": 0.0,
        "reason": reason,
    }


def skipped_annotations(items: list[Mapping[str, object]], status: str, reason: str) -> dict[str, dict[str, object]]:
    annotations: dict[str, dict[str, object]] = {}
    for item in normalize_items(items):
        annotations[item["item_id"]] = {
            "status": status,
            "confidence": None,
            "confidence_reason": reason,
            "relation": "uncertain",
            "matched_signals": [],
            "missing_or_uncertain": [reason],
        }
    return annotations


def evaluate_confidence_batch(
    reference: Mapping[str, object],
    items: list[Mapping[str, object]],
    *,
    enabled: bool = True,
    usage_context: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    model = llm_model()
    if not enabled:
        reason = "disabled by request"
        return skipped_summary("skipped", reason, item_count=len(items), model=model), skipped_annotations(items, "skipped", reason)
    if llm_confidence_disabled():
        reason = "disabled by WEB_SEARCH_LLM_CONFIDENCE_DISABLED"
        return skipped_summary("skipped", reason, item_count=len(items), model=model), skipped_annotations(items, "skipped", reason)
    if not items:
        return skipped_summary("skipped", "no items to score", item_count=0, model=model), {}

    api_key = secret_for_env("OPENAI_API_KEY")
    if not api_key:
        reason = "OPENAI_API_KEY missing"
        return skipped_summary("skipped", reason, item_count=len(items), model=model), skipped_annotations(items, "skipped", reason)

    started = time.monotonic()
    request_payload = build_responses_payload(model, reference, items)
    status_code: int | None = None
    try:
        with httpx.Client(timeout=llm_timeout_seconds()) as client:
            response = client.post(
                openai_responses_url(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    **llm_usage_headers(usage_context),
                },
                json=request_payload,
            )
        status_code = response.status_code
        response.raise_for_status()
        raw = response.json()
        output_text = extract_response_text(raw)
        parsed = parse_json_object(output_text)
        llm_items = parsed.get("items")
        if not isinstance(llm_items, list):
            raise ValueError("LLM confidence response missing items")

        annotations: dict[str, dict[str, object]] = {}
        for item in llm_items:
            if not isinstance(item, dict):
                continue
            item_id = compact_string(item.get("item_id"), 160)
            if not item_id:
                continue
            confidence = safe_confidence(item.get("confidence"))
            annotations[item_id] = {
                "status": "ok",
                "model": model,
                "confidence": confidence,
                "confidence_reason": compact_string(item.get("confidence_reason"), 500),
                "relation": safe_relation(item.get("relation")),
                "matched_signals": safe_string_list(item.get("matched_signals")),
                "missing_or_uncertain": safe_string_list(item.get("missing_or_uncertain")),
            }

        missing_reason = "LLM did not return a score for this item"
        for item in normalize_items(items):
            annotations.setdefault(
                item["item_id"],
                {
                    "status": "missing",
                    "model": model,
                    "confidence": None,
                    "confidence_reason": missing_reason,
                    "relation": "uncertain",
                    "matched_signals": [],
                    "missing_or_uncertain": [missing_reason],
                },
            )

        usage = safe_usage(raw)
        record_api_usage(
            "openai_confidence",
            "confidence",
            ok=True,
            status_code=status_code,
            response_headers=response.headers,
            result_count=len(annotations),
            metadata={"transport": "openai_responses", "model": model, "item_count": len(items)},
        )
        summary = {
            "enabled": True,
            "status": "ok",
            "model": model,
            "item_count": len(items),
            "items_scored": sum(1 for item in annotations.values() if item.get("status") == "ok"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "usage": usage,
        }
        return summary, annotations
    except Exception as exc:
        record_api_usage(
            "openai_confidence",
            "confidence",
            ok=False,
            status_code=status_code,
            result_count=0,
            metadata={"transport": "openai_responses", "model": model, "item_count": len(items), "error_type": type(exc).__name__},
        )
        reason = f"OpenAI confidence scoring failed: {type(exc).__name__}"
        summary = {
            "enabled": True,
            "status": "error",
            "model": model,
            "item_count": len(items),
            "items_scored": 0,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "reason": reason,
        }
        return summary, skipped_annotations(items, "error", reason)
