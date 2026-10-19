"""Deterministic, high-precision quality gates for company news results.

This module intentionally does not call a model or a provider.  Search result
titles are often the only evidence available before the optional bounded crawl,
so only unambiguous non-business patterns are filtered.  Ambiguous articles
remain visible and can be reviewed with the returned quality metadata.
"""

from __future__ import annotations

import json
import re
from typing import Mapping
from urllib.parse import urlparse

from .utils import compact_text


SCHEMA_VERSION = "news_quality/v1"
POLICY = "deterministic_high_precision/v1"
VALUE_LLM_INTENTS = frozenset({"congratulate", "check_in", "conversation_topic"})
VALUE_LLM_ITEM_LIMIT = 20
VALUE_LLM_TIMEOUT_SECONDS = 4.0

_OBITUARY_RE = re.compile(r"(?:^|[\[【(])\s*부고\s*[\]】)]|부친상|모친상|별세|소천|영면|조의", re.IGNORECASE)
_RECRUITMENT_RE = re.compile(r"(?:^|[\[【(])\s*(?:채용|구인|채용공고)\s*[\]】)]|인재\s*채용|채용\s*공고", re.IGNORECASE)
_ADVERTORIAL_RE = re.compile(
    r"(?:^|[\[【(])\s*(?:광고|협찬|보도자료)\s*[\]】)]|광고성|체험단|\bsponsored\b|\badvertorial\b|\bprnewswire\b",
    re.IGNORECASE,
)
_MARKET_RE = re.compile(
    r"주가|증시|특징주|상한가|하한가|장중|거래량|수급|차익\s*실현|매물\s*출회|주식시장|시황|관련주|테마주|급등|급락|하락\s*마감|상승\s*마감",
    re.IGNORECASE,
)
_ROUNDUP_RE = re.compile(r"오늘의\s*(?:뉴스|증시)|주요\s*뉴스|뉴스\s*종합|이\s*시각|브리핑|모아보기|종합", re.IGNORECASE)
_LOW_VALUE_PROMO_RE = re.compile(r"체험단|모집|할인|쿠폰|경품|이벤트", re.IGNORECASE)
_NEGATIVE_CONTEXT_RE = re.compile(
    r"감소|하락|악화|부진|축소|감축|감소세|하락세|부정적|우려|지연|해지|철회|취소|불매|중단|리콜|소송|논란|사고|화재|해킹|유출|적자|손실|파산|구조조정|철수|조사|제재|과징금|악재|위기",
    re.IGNORECASE,
)
_VALUE_MARKERS = {
    "achievement": re.compile(
        r"수상|선정|계약|수주|투자\s*(?:유치|확대)?|유치|매출|영업이익|실적\s*(?:개선|호조|성장|증가)?|성장|흑자|출시|신규\s*사업|공급|납품|협약|MOU|파트너|진출|(?:사업|시장|생산|공급|수출|매출)\s*확대|증설|특허|인증|수출|상장|자금\s*(?:조달|확보)|인수|합병|성과|우수",
        re.IGNORECASE,
    ),
    "risk": re.compile(
        r"리콜|소송|논란|사고|화재|해킹|유출|감사|적자|손실|악화|부진|감소|감축|지연|해지|철회|취소|불매|우려|파산|구조조정|철수|중단|조사|제재|과징금|악재|위기",
        re.IGNORECASE,
    ),
    "conversation": re.compile(
        r"대표|CEO|취임|선임|승진|창립|기념|사회공헌|기부|봉사|행사|전시|콘퍼런스|컨퍼런스|발표|방문|협력|공동|제품|서비스|솔루션|플랫폼|프로젝트|신제품",
        re.IGNORECASE,
    ),
}
_NOISE_WEIGHTS = {
    "obituary": 1.0,
    "recruitment": 0.95,
    "advertorial": 0.95,
    "market_snapshot": 0.9,
    "roundup": 0.85,
    "low_content": 1.0,
}


def news_value_llm_disabled() -> bool:
    import os

    return os.getenv("WEB_SEARCH_NEWS_VALUE_LLM_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _text(value: object, limit: int = 2_000) -> str:
    return compact_text(value)[:limit]


def _is_url_title(title: str) -> bool:
    parsed = urlparse(title)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _value_signals(title: str, snippet: str) -> tuple[list[str], list[str], list[str]]:
    reasons: list[str] = []
    intents: list[str] = []
    categories: list[str] = []
    for source, text in (("title", title), ("snippet", snippet)):
        for category, pattern in _VALUE_MARKERS.items():
            match = pattern.search(text)
            if not match:
                continue
            if category == "achievement":
                context = text[max(0, match.start() - 10) : match.end() + 14]
                if _NEGATIVE_CONTEXT_RE.search(context):
                    continue
            categories.append(category)
            intents.append(
                {
                    "achievement": "congratulate",
                    "risk": "check_in",
                    "conversation": "conversation_topic",
                }[category]
            )
            reasons.append(f"{source}:{category}:{match.group(0)}")
    return _dedupe(reasons), _dedupe(intents), _dedupe(categories)


def assess_news_quality(record: Mapping[str, object]) -> dict[str, object]:
    """Return a stable quality decision without network or model work.

    A record is filtered only when a high-precision marker is present.  The
    market/roundup rules require the absence of a material business-event term,
    preventing ordinary earnings, investment, contract, and product coverage
    from being hidden merely because a title mentions a share price.
    """

    title = _text(record.get("title"))
    snippet = _text(record.get("snippet"))
    value_reasons, value_intents, value_categories = _value_signals(title, snippet)
    reasons: list[str] = []
    if not title:
        reasons.append("low_content")
    elif _is_url_title(title) and not snippet:
        reasons.append("low_content")

    if _OBITUARY_RE.search(title) and not value_reasons:
        reasons.append("obituary")
    if _RECRUITMENT_RE.search(title) and not value_reasons:
        reasons.append("recruitment")
    if _ADVERTORIAL_RE.search(title) and (not value_reasons or _LOW_VALUE_PROMO_RE.search(title)):
        reasons.append("advertorial")
    if _MARKET_RE.search(title) and not value_reasons:
        reasons.append("market_snapshot")
    if _ROUNDUP_RE.search(title) and not value_reasons:
        reasons.append("roundup")

    reasons = _dedupe(reasons)
    score = max((_NOISE_WEIGHTS[reason] for reason in reasons), default=0.0)
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": POLICY,
        "status": "noise" if reasons else "keep",
        "is_noise": bool(reasons),
        "noise_score": round(score, 2),
        "noise_reasons": reasons,
        # Value intents are semantic decisions; keywords stay as raw evidence
        # only and never masquerade as the decision itself.
        "value_status": "unavailable",
        "value_score": 0.0,
        "value_source": "unavailable",
        "value_intents": [],
        "value_categories": _dedupe(value_categories),
        "value_signals": value_reasons,
    }


_VALUE_INTENT_STATUS = {
    "congratulate": "valuable",
    "check_in": "valuable",
    "conversation_topic": "contextual",
}


def apply_llm_value_intents(quality: dict[str, object], classification: Mapping[str, object]) -> dict[str, object]:
    """Overlay an LLM value-intent verdict onto a quality record."""
    intent = classification.get("intent")
    if intent not in VALUE_LLM_INTENTS:
        return quality
    confidence = classification.get("confidence")
    score = round(float(confidence), 2) if isinstance(confidence, (int, float)) else 0.7
    quality["value_intents"] = [intent]
    quality["value_source"] = "llm"
    quality["value_status"] = _VALUE_INTENT_STATUS.get(intent, "weak")
    quality["value_score"] = score
    reason = compact_text(classification.get("reason"))[:300]
    if reason:
        quality["value_llm_reason"] = reason
    return quality


_VALUE_LLM_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "intent": {
                        "type": "string",
                        "enum": ["congratulate", "check_in", "conversation_topic", "none"],
                    },
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["item_id", "intent", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_VALUE_LLM_INSTRUCTIONS = (
    "You classify Korean business-news search results for a CRM contact-reminder feature. "
    "For each item decide the single best relationship intent from the title and snippet only: "
    "'congratulate' when the company achieved something positive worth congratulating (contract, award, "
    "earnings growth, launch, expansion, investment raised); 'check_in' when the article reports a risk, "
    "loss, lawsuit, recall, or other negative event worth a caring check-in; 'conversation_topic' when the "
    "article is a neutral but useful talking point (product news, executive moves, events, partnerships); "
    "'none' when none applies. Judge the whole headline: a lawsuit about a contract is 'check_in', not "
    "'congratulate'; a share-price move without a business event is 'none'. Return JSON only."
)


def classify_value_intents_llm(
    records: list[Mapping[str, object]],
    *,
    timeout_seconds: float = VALUE_LLM_TIMEOUT_SECONDS,
    api_key: str | None = None,
    model: str | None = None,
    url: str | None = None,
) -> dict[int, dict[str, object]] | None:
    """One bounded batch call; ``None`` on any failure so callers can degrade."""
    if not records:
        return None
    import httpx
    from .credentials import secret_for_env
    from .llm_confidence import (
        extract_response_text,
        llm_model,
        openai_responses_url,
        parse_json_object,
    )
    from .usage import record_api_usage

    api_key = api_key if api_key is not None else secret_for_env("OPENAI_API_KEY")
    if not api_key:
        return None
    model = model or llm_model()
    items = [
        {"item_id": str(index), "title": _text(record.get("title"), 200), "snippet": _text(record.get("snippet"), 500)}
        for index, record in enumerate(records[:VALUE_LLM_ITEM_LIMIT])
    ]
    payload = {
        "model": model,
        "instructions": _VALUE_LLM_INSTRUCTIONS,
        "input": [{"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)}],
        "text": {"format": {"type": "json_schema", "name": "news_value_intents", "strict": True, "schema": _VALUE_LLM_SCHEMA}},
        "store": False,
        "max_output_tokens": 1200,
    }
    try:
        import time

        started = time.monotonic()
        with httpx.Client(timeout=max(1.0, float(timeout_seconds))) as client:
            response = client.post(
                url or openai_responses_url(),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        parsed = parse_json_object(extract_response_text(response.json()))
        record_api_usage(
            "openai_news_value", "news_value_intents", ok=True,
            status_code=response.status_code, response_headers=response.headers,
            result_count=len(items), metadata={"model": model, "elapsed": round(time.monotonic() - started, 3)},
        )
    except Exception:
        return None
    result: dict[int, dict[str, object]] = {}
    for entry in parsed.get("items") if isinstance(parsed.get("items"), list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(str(entry.get("item_id")))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(records):
            continue
        intent = entry.get("intent")
        if intent == "none":
            result[index] = {"intent": None}
            continue
        if intent not in VALUE_LLM_INTENTS:
            continue
        confidence = entry.get("confidence")
        result[index] = {
            "intent": intent,
            "confidence": confidence if isinstance(confidence, (int, float)) and 0 <= confidence <= 1 else None,
            "reason": entry.get("reason"),
        }
    return result


def empty_news_quality_stats() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": POLICY,
        "filter_mode": "high_precision",
        "candidates_seen": 0,
        "noise_filtered": 0,
        "noise_reasons": {},
        "valuable_returned": 0,
        "contextual_returned": 0,
        "weak_returned": 0,
        "unavailable_returned": 0,
        "value_intents": {},
        "url_duplicates_removed": 0,
        "title_duplicates_removed": 0,
        "returned_count": 0,
    }


def record_noise_stat(stats: dict[str, object], quality: Mapping[str, object]) -> None:
    if not quality.get("is_noise"):
        return
    stats["noise_filtered"] = int(stats.get("noise_filtered") or 0) + 1
    reasons = stats.get("noise_reasons")
    if not isinstance(reasons, dict):
        reasons = {}
        stats["noise_reasons"] = reasons
    for reason in quality.get("noise_reasons") or []:
        reasons[str(reason)] = int(reasons.get(str(reason)) or 0) + 1


def record_value_stat(stats: dict[str, object], quality: Mapping[str, object]) -> None:
    status = str(quality.get("value_status") or "weak")
    key = f"{status}_returned"
    if key in stats:
        stats[key] = int(stats.get(key) or 0) + 1
    intents = stats.get("value_intents")
    if not isinstance(intents, dict):
        intents = {}
        stats["value_intents"] = intents
    for intent in quality.get("value_intents") or []:
        intents[str(intent)] = int(intents.get(str(intent)) or 0) + 1
