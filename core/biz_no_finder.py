"""Precision-first Korean business-registration-number resolution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import time
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

from enterpriseDB.enrich_enterprise_db import address_tokens, corporate_email_host, website_host

from .enterprise_db import bounded_int, cap_search_timeout, dedupe, field_values, first_field, parse_bool
from .enterprise_official import OpenDartCompanyClient, canonical_company_name, company_names_equal
from .search import create_search_client
from .utils import compact_text


BIZ_NO_FINDER_ENDPOINT = "/v1/bizNo"
BIZ_NO_FINDER_ENDPOINTS = frozenset((BIZ_NO_FINDER_ENDPOINT, "/api/bizNoFinder"))
BIZ_NO_FINDER_POLICY_VERSION = "biz_no_finder_policy/1.2.0"

SearchClientFactory = Callable[[str], Any]
DartLookup = Callable[[Mapping[str, Any]], dict[str, Any]]

_NUMBER_RE = re.compile(r"(?<!\d)(\d{3})[-.\s]?(\d{2})[-.\s]?(\d{5})(?!\d)")
_LABEL_RE = re.compile(
    r"(?:사업자\s*(?:등록)?\s*(?:번호|no\.?)|"
    r"business\s*(?:(?:registration|license|tax)\s*)?(?:number|no\.?))",
    re.IGNORECASE,
)
_ENTITY_DIRECTORY_DOMAINS = (
    "bizno.net",
    "bizno.co.kr",
    "biznoinfo.com",
    "moneypin.biz",
    "hospitalk.net",
)
_DIRECTORY_DOMAINS = (
    *_ENTITY_DIRECTORY_DOMAINS,
    "jobkorea.co.kr",
    "saramin.co.kr",
    "incruit.com",
    "catch.co.kr",
    "wanted.co.kr",
    "rocketpunch.com",
    "linkedin.com",
)
_SHARED_PLATFORM_DOMAINS = (
    "naver.com",
    "notion.site",
    "tistory.com",
    "blogspot.com",
    "facebook.com",
    "instagram.com",
    "google.com",
)
_EMPTY_ADDRESS_MARKERS = {"o", "x", "true", "false", "유", "무"}
_DOCUMENT_URL_RE = re.compile(
    r"(?:\.(?:pdf|docx?|xlsx?|hwp)(?:$|[?#])|filedownload|attachment|/download(?:/|\?|$))",
    re.IGNORECASE,
)
_RNDCIRCLE_COMPANY_PATH_RE = re.compile(r"^/company/[0-9a-f-]{36}/?$", re.IGNORECASE)
_KOREAN_LETTER_NAMES = (
    "에이", "비", "씨", "디", "이", "에프", "지", "에이치", "아이", "제이", "케이", "엘", "엠",
    "엔", "오", "피", "큐", "알", "에스", "티", "유", "브이", "더블유", "엑스", "와이", "지",
)


def format_business_number(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}" if len(digits) == 10 else ""


def valid_business_number(value: object) -> bool:
    """Validate a Korean 10-digit business number using the NTS checksum."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 10 or len(set(digits)) == 1:
        return False
    weights = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    total = sum(int(digits[index]) * weight for index, weight in enumerate(weights))
    total += int(digits[8]) * 5 // 10
    return (10 - total % 10) % 10 == int(digits[9])


def _directory_host(host: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in _DIRECTORY_DOMAINS)


def _entity_directory_host(host: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in _ENTITY_DIRECTORY_DOMAINS)


def _company_owned_host(host: str) -> bool:
    return bool(
        host
        and not _directory_host(host)
        and not any(host == domain or host.endswith(f".{domain}") for domain in _SHARED_PLATFORM_DOMAINS)
    )


def _same_host(left: str, right: str) -> bool:
    return bool(
        left
        and right
        and (left == right or left.endswith(f".{right}") or right.endswith(f".{left}"))
    )


def _meaningful_address(value: str) -> str:
    value = compact_text(value)
    return "" if value.casefold() in _EMPTY_ADDRESS_MARKERS else value


def _phone_values(payload: Mapping[str, object]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in field_values(payload, "phone") + field_values(payload, "fax"):
        digits = re.sub(r"\D", "", value)
        if 8 <= len(digits) <= 12 and digits not in seen:
            seen.add(digits)
            values.append({"text": compact_text(value), "digits": digits})
    return values


def _split_aliases(values: list[str]) -> list[str]:
    return dedupe(
        [part.strip() for value in values for part in re.split(r"[;\n]+", value) if part.strip()]
    )


def _website_hosts(value: str, email_host: str) -> list[str]:
    hosts = [
        website_host(part)
        for part in re.split(r"[;,\s]+", value)
        if part.strip()
    ]
    return dedupe([host for host in [*hosts, email_host] if host and re.fullmatch(r"[a-z0-9.-]+", host)])


def biz_no_profile(payload: Mapping[str, object]) -> dict[str, Any]:
    primary_names = _split_aliases(field_values(payload, "company"))
    if not primary_names:
        raise ValueError("company.회사명, company_name, or 회사명(Company) is required")
    english_names = _split_aliases(field_values(payload, "english_company"))
    brands = _split_aliases(field_values(payload, "brand"))
    website = first_field(payload, "homepage")
    email_host = corporate_email_host(first_field(payload, "email"))
    input_hosts = _website_hosts(website, email_host)
    input_host = input_hosts[0] if input_hosts else ""
    return {
        "company": primary_names[0],
        "primary_names": primary_names,
        "english_names": english_names,
        "brands": brands,
        "names": dedupe(primary_names + english_names + brands),
        "dart_names": dedupe(primary_names + english_names),
        "website": website,
        "input_host": input_host,
        "input_hosts": input_hosts,
        "official_input_hosts": [host for host in input_hosts if _company_owned_host(host)],
        "address": _meaningful_address(first_field(payload, "address")),
        "phones": _phone_values(payload),
    }


def biz_no_queries(profile: Mapping[str, Any], *, maximum: int) -> list[str]:
    company = str(profile["company"]).replace('"', " ")[:160]
    queries: list[str] = []
    host = str(profile.get("input_host") or "")
    if host:
        queries.append(f'"{company}" "{host}" "사업자등록번호"')

    address = str(profile.get("address") or "")
    tokens = address_tokens(address)
    if tokens:
        location = " ".join(tokens[-4:])
        queries.append(f'"{company}" "{location}" "사업자등록번호"')
    elif profile.get("phones"):
        phone = str(profile["phones"][0]["text"]).replace('"', " ")
        queries.append(f'"{company}" "{phone}" "사업자등록번호"')

    queries.append(f'"{company}" "사업자등록번호"')
    return dedupe(queries)[:maximum]


def _name_match(text: str, names: list[str]) -> bool:
    blob = canonical_company_name(text)
    for name in names:
        key = canonical_company_name(name)
        minimum = 2 if re.search(r"[가-힣]", key) else 3
        if len(key) >= minimum and key in blob:
            return True
    return False


def _spoken_acronym_key(value: str) -> str:
    key = canonical_company_name(value)
    if not re.search(r"[가-힣]", key):
        return key
    return re.sub(
        r"[a-z]{1,4}",
        lambda match: "".join(
            _KOREAN_LETTER_NAMES[ord(letter) - ord("a")] for letter in match.group()
        ),
        key,
    )


def _discovered_company_aliases(
    records: list[dict[str, Any]], profile: Mapping[str, Any]
) -> list[str]:
    aliases: list[str] = []
    names = list(profile.get("names") or [])
    for record in records:
        url = compact_text(record.get("canonical_url") or record.get("url"))
        parsed = urlsplit(url)
        if parsed.hostname != "app.rndcircle.io" or not _RNDCIRCLE_COMPANY_PATH_RE.fullmatch(parsed.path):
            continue
        title = compact_text(record.get("title"))
        alias = re.split(r"\s+(?:기업\s*정보|보도자료)\b|\s*\|", title, maxsplit=1)[0].strip()
        if alias and any(_spoken_acronym_key(alias) == _spoken_acronym_key(name) for name in names):
            aliases.append(alias)
    return dedupe(aliases)


def _directory_entity_match(title: str, names: list[str]) -> bool:
    for fragment in re.split(r"\s+(?:-|\||·)\s+|[:：]", title):
        cleaned = re.sub(r"\s*(?:사업자\s*(?:등록)?번호|기업정보|조회).*$", "", fragment).strip()
        if any(company_names_equal(cleaned, name) for name in names):
            return True
    return False


def _number_evidence(text: str, url: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for match in _NUMBER_RE.finditer(text):
        before = text[max(0, match.start() - 48) : match.start()]
        after = text[match.end() : match.end() + 24]
        if _LABEL_RE.search(before) or _LABEL_RE.search(after):
            number = "".join(match.groups())
            if valid_business_number(number):
                found.setdefault(number, set()).add("business_number_label")

    decoded_url = unquote(url)
    host = (urlsplit(decoded_url).hostname or "").casefold().removeprefix("www.")
    if _directory_host(host) or re.search(r"biz(?:no|number)|business[-_/]?number", decoded_url, re.I):
        for match in _NUMBER_RE.finditer(decoded_url):
            number = "".join(match.groups())
            if valid_business_number(number):
                found.setdefault(number, set()).add("business_directory_url")
    return found


def _record_dict(record: object) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    serializer = getattr(record, "to_dict", None)
    return dict(serializer()) if callable(serializer) else {}


def _identity_signals(
    *, text: str, url: str, profile: Mapping[str, Any], source_host: str | None = None
) -> dict[str, Any]:
    host = source_host or (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    official_hosts = list(profile.get("official_input_hosts") or [])
    digit_blob = re.sub(r"\D", "", text)
    matched_address_tokens = [
        token for token in address_tokens(str(profile.get("address") or "")) if token.casefold() in text.casefold()
    ]
    strong_address_match = len(matched_address_tokens) >= 4 and any(
        re.search(r"\d|(?:로|길|번길|동)$", token) for token in matched_address_tokens
    )
    return {
        "company_name_match": _name_match(text, list(profile.get("names") or [])),
        "input_domain_match": any(_same_host(host, input_host) for input_host in official_hosts),
        "phone_or_fax_match": any(phone["digits"] in digit_blob for phone in profile.get("phones") or []),
        "address_token_matches": matched_address_tokens,
        "strong_address_match": strong_address_match,
    }


def _search_sources(records: list[dict[str, Any]], profile: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    candidates: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for record in records:
        url = compact_text(record.get("canonical_url") or record.get("url"))
        title = compact_text(record.get("title"))
        snippet = compact_text(record.get("snippet"))
        text = compact_text(f"{title} {snippet}")
        host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
        signals = _identity_signals(text=f"{text} {url}", url=url, profile=profile, source_host=host)
        for number, evidence_kinds in _number_evidence(text, url).items():
            if not (
                signals["company_name_match"]
                or signals["input_domain_match"]
                or signals["phone_or_fax_match"]
                or len(signals["address_token_matches"]) >= 2
            ):
                continue
            marker = (number, url)
            if marker in seen:
                continue
            seen.add(marker)
            if signals["input_domain_match"]:
                category = "official_document" if _DOCUMENT_URL_RE.search(url) else "official_domain"
            else:
                category = "directory" if _directory_host(host) else "web"
            directory_entity_page = bool(
                _entity_directory_host(host)
                and _directory_entity_match(title, list(profile.get("names") or []))
            )
            identity_binding = (
                "company_domain"
                if category == "official_domain"
                else ("directory_entity_page" if directory_entity_page else "context_only")
            )
            candidates.setdefault(number, []).append(
                {
                    "url": url,
                    "title": title,
                    "host": host,
                    "provider": compact_text(record.get("source")) or "serper",
                    "category": category,
                    "identity_binding": identity_binding,
                    "evidence": sorted(evidence_kinds),
                    **signals,
                }
            )
    return {
        number: sources
        for number, sources in candidates.items()
        if any(source["identity_binding"] != "context_only" for source in sources)
    }


def _dart_source(result: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if result.get("status") != "matched":
        return None
    fields = result.get("fields") if isinstance(result.get("fields"), Mapping) else {}
    number = re.sub(r"\D", "", str(fields.get("business_number") or ""))
    if not valid_business_number(number):
        return None
    field_sources = result.get("field_sources") if isinstance(result.get("field_sources"), Mapping) else {}
    url = str(field_sources.get("business_number") or "")
    text = compact_text(
        " ".join(
            str(fields.get(field) or "")
            for field in ("company", "english_company", "homepage", "phone", "fax", "address")
        )
    )
    homepage_host = website_host(str(fields.get("homepage") or ""))
    signals = _identity_signals(text=text, url=url, profile=profile, source_host=homepage_host)
    registry_sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    identity_binding = next(
        (
            compact_text(source.get("identity_reason"))
            for source in registry_sources
            if isinstance(source, Mapping) and compact_text(source.get("identity_reason"))
        ),
        "official_registry_exact_name",
    )
    return number, {
        "url": url,
        "title": "OpenDART 기업개황",
        "host": "opendart.fss.or.kr",
        "provider": "opendart",
        "category": "official_registry",
        "identity_binding": identity_binding,
        "evidence": [identity_binding],
        "matched_company": compact_text(fields.get("company")),
        **signals,
    }


def _summarize_candidate(number: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    hosts = {str(source.get("host") or "") for source in sources if source.get("host")}
    registry = any(source["category"] == "official_registry" for source in sources)
    official_domain = sum(source["category"] == "official_domain" for source in sources)
    name_matches = sum(bool(source["company_name_match"]) for source in sources)
    phone_matches = sum(bool(source["phone_or_fax_match"]) for source in sources)
    address_matches = max((len(source["address_token_matches"]) for source in sources), default=0)
    strong_address_matches = sum(bool(source["strong_address_match"]) for source in sources)
    domain_matches = sum(bool(source["input_domain_match"]) for source in sources)
    non_directory_hosts = {
        source["host"]
        for source in sources
        if source.get("host") and source["category"] not in {"directory", "official_registry"}
    }
    return {
        "business_registration_number": format_business_number(number),
        "evidence_level": "low",
        "signals": {
            "official_registry": registry,
            "official_domain_sources": official_domain,
            "independent_source_hosts": len(hosts),
            "non_directory_web_hosts": len(non_directory_hosts),
            "company_name_matches": name_matches,
            "phone_or_fax_matches": phone_matches,
            "max_address_token_matches": address_matches,
            "strong_address_matches": strong_address_matches,
            "input_domain_matches": domain_matches,
        },
        "sources": sources,
    }


def _is_auto_acceptable(candidate: Mapping[str, Any]) -> tuple[bool, str]:
    signals = candidate["signals"]
    if (
        signals["official_registry"]
        and signals["official_domain_sources"]
        and signals["company_name_matches"]
    ):
        return True, "OpenDART and the supplied company domain independently state the same number"
    return False, "candidate requires OpenDART and supplied-company-domain agreement for automatic acceptance"


def _candidate_rank(candidate: Mapping[str, Any]) -> tuple[int, ...]:
    signals = candidate["signals"]
    return (
        int(bool(signals["official_domain_sources"])),
        signals["phone_or_fax_matches"],
        signals["strong_address_matches"],
        signals["max_address_token_matches"],
        int(signals["official_registry"]),
        signals["non_directory_web_hosts"],
        signals["company_name_matches"],
        signals["independent_source_hosts"],
    )


def _default_dart_lookup(profile: Mapping[str, Any]) -> dict[str, Any]:
    return OpenDartCompanyClient(include_reports=False).lookup(
        {
            "names": list(profile.get("dart_names") or []),
            "business_no": "",
            "corporate_no": "",
        }
    )


def find_business_number(
    payload: Mapping[str, object],
    *,
    search_client_factory: SearchClientFactory = create_search_client,
    dart_lookup: DartLookup | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    profile = biz_no_profile(payload)
    respect_robots = parse_bool(payload.get("respect_robots"), default=True, name="respect_robots")
    search_limit = bounded_int(payload.get("search_limit") or payload.get("limit"), default=7, maximum=10)
    max_queries = bounded_int(payload.get("max_queries"), default=3, maximum=4)
    queries = biz_no_queries(profile, maximum=max_queries)

    records: list[dict[str, Any]] = []
    search_errors: list[str] = []
    dart_errors: list[str] = []
    dart_result: dict[str, Any] = {"status": "not_run", "provider": "opendart"}

    def search(query: str) -> list[dict[str, Any]]:
        client = cap_search_timeout(search_client_factory("serper"))
        return [
            _record_dict(record)
            for record in client.search(query, limit=search_limit, query_intent="biz_no_finder")
        ]

    search_started = time.monotonic()
    lookup = dart_lookup or _default_dart_lookup
    with ThreadPoolExecutor(max_workers=len(queries) + 1) as executor:
        dart_future = executor.submit(lookup, profile)
        search_futures = {executor.submit(search, query): query for query in queries}
        for future in as_completed([dart_future, *search_futures]):
            if future is dart_future:
                try:
                    dart_result = future.result()
                except Exception as exc:
                    dart_errors.append(f"opendart: {type(exc).__name__}: {exc}")
                continue
            query = search_futures[future]
            try:
                records.extend(future.result())
            except Exception as exc:
                search_errors.append(f"{query}: {type(exc).__name__}: {exc}")
    if dart_result.get("error"):
        dart_errors.append(f"opendart: {dart_result['error']}")

    grouped = _search_sources(records, profile)
    dart_candidate = _dart_source(dart_result, profile)
    if dart_candidate:
        number, source = dart_candidate
        grouped.setdefault(number, []).append(source)

    fallback_aliases: list[str] = []
    fallback_query = ""
    if not grouped:
        fallback_aliases = _discovered_company_aliases(records, profile)
        fallback_name = (fallback_aliases or [str(profile["company"])])[0].replace('"', " ")[:160]
        fallback_query = f'site:bizno.net/article "{fallback_name}"'
        queries.append(fallback_query)
        try:
            fallback_records = search(fallback_query)
            records.extend(fallback_records)
            fallback_profile = {
                **profile,
                "names": dedupe([*list(profile.get("names") or []), *fallback_aliases]),
            }
            for number, sources in _search_sources(fallback_records, fallback_profile).items():
                grouped.setdefault(number, []).extend(sources)
        except Exception as exc:
            search_errors.append(f"{fallback_query}: {type(exc).__name__}: {exc}")

    search_elapsed = time.monotonic() - search_started

    candidates = [_summarize_candidate(number, sources) for number, sources in grouped.items()]
    candidates.sort(key=_candidate_rank, reverse=True)
    accepted = False
    decision_reason = "no checksum-valid candidate with matching company identity evidence was found"
    if len(candidates) == 1:
        accepted, decision_reason = _is_auto_acceptable(candidates[0])
        candidates[0]["evidence_level"] = "high" if accepted else "medium"
    elif candidates:
        decision_reason = "conflicting checksum-valid business-number candidates require review"
        for candidate in candidates:
            signals = candidate["signals"]
            candidate["evidence_level"] = "medium" if any(
                (
                    signals["official_registry"],
                    signals["official_domain_sources"],
                    signals["phone_or_fax_matches"],
                    signals["strong_address_matches"],
                )
            ) else "low"

    if accepted:
        status, decision = "found", "auto_accept"
    elif len(candidates) == 1:
        status, decision = "review_required", "review"
    elif candidates:
        status, decision = "ambiguous", "review"
    else:
        status, decision = "not_found", "abstain"

    accepted_number = candidates[0]["business_registration_number"] if accepted else None
    recommended = candidates[0]["business_registration_number"] if len(candidates) == 1 else None
    return {
        "schema_version": "biz_no_finder/v1",
        "policy_version": BIZ_NO_FINDER_POLICY_VERSION,
        "endpoint": BIZ_NO_FINDER_ENDPOINT,
        "response_mode": "synchronous",
        "completion_status": "completed",
        "status": status,
        "decision": decision,
        "decision_reason": decision_reason,
        "business_registration_number": accepted_number,
        "recommended_candidate": recommended,
        "matched_company": profile["company"],
        "candidates": candidates,
        "request": {
            "company": profile["company"],
            "search_queries": queries,
            "fallback_used": bool(fallback_query),
            "discovered_aliases": fallback_aliases,
            "search_limit": search_limit,
            "respect_robots": respect_robots,
            "crawl_performed": False,
            "input_signals": {
                "company_alias_count": len(profile["names"]),
                "domain": profile["input_host"],
                "domain_count": len(profile["input_hosts"]),
                "has_address": bool(profile["address"]),
                "phone_or_fax_count": len(profile["phones"]),
            },
        },
        "subtasks": [
            {
                "name": "opendart_company_lookup",
                "provider": "opendart",
                "status": dart_result.get("status"),
                "count": int(bool(dart_candidate)),
                "elapsed_seconds": dart_result.get("elapsed_seconds", 0.0),
            },
            {
                "name": "business_number_search",
                "provider": "serper",
                "status": "completed" if not search_errors else ("partial" if records else "failed"),
                "count": len(records),
                "elapsed_seconds": round(search_elapsed, 3),
            },
        ],
        "errors": [*dart_errors, *search_errors],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
