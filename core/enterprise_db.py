from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hmac
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable, Mapping

from enterpriseDB.enrich_enterprise_db import (
    corporate_email_host,
    extract_fields,
    extract_linkedin_company_fields,
    identity_match,
    is_linkedin_company_url,
    ranked_candidates,
)

from .crawlers import create_crawler
from .credentials import secret_for_env
from .enterprise_official import empty_official_result, enrich_official_company
from .search import create_search_client
from .utils import compact_text


ENTERPRISE_DB_ENDPOINT = "/api/enterpriseDB"
ENTERPRISE_DB_KEY_ENV = "ENTERPRISE_DB_API_KEY"
DEFAULT_ENTERPRISE_DB_KEY_PATH = Path("/workspace/key/enterprise_db_api.key")
SEARCH_TIMEOUT_SECONDS = 10.0
CRAWL_MAX_LATENCY_SECONDS = 20.0

SearchClientFactory = Callable[[str], Any]
CrawlerFactory = Callable[..., Any]
OfficialLookup = Callable[[Mapping[str, Any], Mapping[str, str]], dict[str, Any]]

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "company": ("회사명", "기업명", "법인명", "company", "company_name", "corp_name", "회사명(Company)", "식별정보(회사명)"),
    "english_company": ("영문회사명", "회사명_영문", "english_company_name", "company_name_en", "alt_company", "회사명_영문(Alt Company)"),
    "brand": ("브랜드", "brand", "brands", "브랜드(Brand)"),
    "email": ("이메일", "email", "company_email", "대표이메일", "이메일(Email)"),
    "business_number": ("사업자번호", "사업자등록번호", "business_number", "business_registration_number", "biz_no", "식별여부"),
    "corporate_number": ("법인등록번호", "법인번호", "corporate_number", "corporate_registration_number", "corp_no", "crn"),
    "address": ("주소", "address", "addr", "location", "주소(Address)", "식별정보(주소)"),
    "representative": ("대표자", "대표자명", "ceo", "ceo_name", "representative", "식별정보(대표자명)"),
    "products": ("주요상품", "주요제품", "주요서비스", "사업내용", "products", "services", "식별정보(주요상품)"),
    "homepage": ("홈페이지", "웹사이트", "homepage", "website", "url", "웹사이트(Website)", "식별정보(홈페이지)"),
    "phone": ("전화", "대표전화", "phone", "telephone", "전화(Phone)"),
    "fax": ("팩스", "fax", "팩스(Fax)"),
    "scale": ("기업규모", "company_size", "employee_count", "식별정보(기업규모)"),
    "type": ("기업유형", "기업형태", "company_type", "corporate_type", "식별정보(기업유형)"),
    "industry": ("업종", "산업", "industry", "sector", "business_type", "식별정보(업종)"),
    "founded": ("설립일", "설립일자", "founded", "founded_at", "established", "식별정보(설립일)"),
    "revenue": ("매출액", "연매출", "revenue", "annual_revenue", "식별정보(매출액)"),
    "operating_profit": ("영업이익", "operating_profit", "식별정보(영업이익)"),
    "net_income": ("당기순이익", "순이익", "net_income", "식별정보(당기순이익)"),
}

OUTPUT_FIELDS = {
    "company": "회사명",
    "address": "주소",
    "representative": "대표자명",
    "products": "주요상품",
    "homepage": "홈페이지",
    "scale": "기업규모",
    "type": "기업유형",
    "industry": "업종",
    "founded": "설립일",
    "revenue": "매출액",
    "operating_profit": "영업이익",
    "net_income": "당기순이익",
}

ADDITIONAL_OFFICIAL_FIELDS = {
    "english_company": "영문회사명",
    "business_number": "사업자번호",
    "corporate_number": "법인등록번호",
    "stock_code": "종목코드",
    "listing": "상장구분",
    "industry_code": "업종코드",
    "phone": "대표전화",
    "fax": "팩스",
    "settlement_month": "결산월",
    "business_status": "사업자상태",
    "tax_type": "과세유형",
    "business_closed_date": "폐업일",
}

CANONICAL_OUTPUT_FIELDS = {**OUTPUT_FIELDS, **ADDITIONAL_OFFICIAL_FIELDS}


def normalize_key(value: object) -> str:
    return re.sub(r"[\s_\-./()]+", "", str(value or "")).casefold()


def text_values(value: object) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, Mapping):
        result: list[str] = []
        for item in value.values():
            result.extend(text_values(item))
        return result
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(text_values(item))
        return result
    text = compact_text(value)
    return [text] if text else []


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = compact_text(value)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def request_sources(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    company = payload.get("company")
    nested = company if isinstance(company, Mapping) else {}
    return [nested, payload]


def field_values(payload: Mapping[str, object], field: str) -> list[str]:
    aliases = {normalize_key(alias) for alias in FIELD_ALIASES[field]}
    values: list[str] = []
    for source in request_sources(payload):
        for key, value in source.items():
            if normalize_key(key) in aliases:
                if isinstance(value, Mapping):
                    continue
                values.extend(text_values(value))
    return dedupe(values)


def first_field(payload: Mapping[str, object], field: str) -> str:
    values = field_values(payload, field)
    return values[0] if values else ""


def parse_bool(value: object, *, default: bool, name: str = "option") -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def bounded_int(value: object, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError("search_limit, crawl_limit, and max_queries must be integers") from exc
    return max(1, min(parsed, maximum))


def enterprise_profile(payload: Mapping[str, object]) -> tuple[dict[str, Any], dict[str, str]]:
    names = dedupe(
        field_values(payload, "company")
        + field_values(payload, "english_company")
        + field_values(payload, "brand")
    )
    if not names:
        raise ValueError("company.회사명 or company_name is required")
    known = {field: first_field(payload, field) for field in OUTPUT_FIELDS}
    business_number = re.sub(r"\D", "", first_field(payload, "business_number"))
    corporate_number = re.sub(r"\D", "", first_field(payload, "corporate_number"))
    website = known["homepage"] or corporate_email_host(first_field(payload, "email"))
    profile = {
        "names": names,
        "company": names[0],
        "business_no": business_number if len(business_number) == 10 else "",
        "corporate_no": corporate_number if len(corporate_number) == 13 else "",
        "address": known["address"],
        "website": website,
    }
    return profile, known


def enterprise_search_queries(
    payload: Mapping[str, object], profile: Mapping[str, Any], *, maximum: int
) -> list[str]:
    company = str(profile["company"])
    clean_company = company.replace('"', "")
    business_number = str(profile.get("business_no") or "")
    queries = text_values(payload.get("query"))
    if business_number:
        queries.append(f'"{clean_company}" "{business_number}" 기업정보 대표자 설립일 매출액')
    else:
        queries.append(f'"{clean_company}" 기업정보 대표자 설립일 매출액')
        if parse_bool(payload.get("linkedin_identity"), default=True):
            queries.append(f'site:linkedin.com/company "{clean_company}"')

    for alias in list(profile.get("names") or [])[1:]:
        clean_alias = str(alias).replace('"', "")
        queries.append(f'"{clean_alias}" "{clean_company}" company profile 기업정보')

    location = " ".join(str(profile.get("address") or "").split()[:2])
    context = " ".join(value for value in (location, first_field(payload, "industry")) if value)
    queries.append(compact_text(f'"{clean_company}" 기업정보 대표자 주요사업 {context}'))
    return dedupe(queries)[:maximum]


def cap_search_timeout(client: object) -> object:
    if hasattr(client, "timeout"):
        try:
            client.timeout = min(float(client.timeout), SEARCH_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            client.timeout = SEARCH_TIMEOUT_SECONDS
    return client


def bearer_authorized(authorization: object, expected_key: str | None = None) -> bool:
    expected = expected_key if expected_key is not None else secret_for_env(ENTERPRISE_DB_KEY_ENV)
    scheme, separator, token = str(authorization or "").partition(" ")
    return bool(
        expected
        and separator
        and scheme.casefold() == "bearer"
        and token
        and hmac.compare_digest(token.strip(), expected.strip())
    )


def provision_api_key(path: Path = DEFAULT_ENTERPRISE_DB_KEY_PATH) -> Path:
    path = path.expanduser().resolve()
    if path.exists():
        if not path.read_text(encoding="utf-8").strip().startswith("tie-"):
            raise ValueError(f"existing key is invalid: {path}")
        path.chmod(0o600)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"tie-{secrets.token_urlsafe(32)}\n".encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, token)
    finally:
        os.close(descriptor)
    return path


def complete_enterprise_db(
    payload: Mapping[str, object],
    *,
    search_client_factory: SearchClientFactory = create_search_client,
    crawler_factory: CrawlerFactory = create_crawler,
    official_lookup: OfficialLookup | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    profile, known = enterprise_profile(payload)
    respect_robots = parse_bool(payload.get("respect_robots"), default=True, name="respect_robots")
    linkedin_identity = parse_bool(payload.get("linkedin_identity"), default=True, name="linkedin_identity")
    use_official_sources = parse_bool(payload.get("official_sources"), default=True, name="official_sources")
    search_limit = bounded_int(payload.get("search_limit") or payload.get("limit"), default=5, maximum=10)
    crawl_limit = bounded_int(payload.get("crawl_limit"), default=2, maximum=3)
    max_queries = bounded_int(payload.get("max_queries"), default=3, maximum=3)

    official_started = time.monotonic()
    official = empty_official_result()
    if use_official_sources:
        try:
            official = (official_lookup or enrich_official_company)(profile, known)
        except Exception as exc:
            official["errors"].append(f"official_lookup: {type(exc).__name__}: {exc}")
    else:
        official["subtasks"].append(
            {
                "name": "official_company_sources",
                "provider": "official_company_sources",
                "operation": "company_lookup",
                "status": "skipped",
                "count": 0,
                "elapsed_seconds": 0.0,
                "reason": "official_sources=false",
            }
        )
    official_elapsed = time.monotonic() - official_started
    official_fields = official.get("fields") if isinstance(official.get("fields"), Mapping) else {}
    if official.get("identity_confirmed"):
        for name_field in ("company", "english_company"):
            value = compact_text(official_fields.get(name_field))
            if value and value not in profile["names"]:
                profile["names"].append(value)
        discovered_business_no = re.sub(r"\D", "", str(official_fields.get("business_number") or ""))
        discovered_corporate_no = re.sub(r"\D", "", str(official_fields.get("corporate_number") or ""))
        if not profile["business_no"] and len(discovered_business_no) == 10:
            profile["business_no"] = discovered_business_no
        if not profile["corporate_no"] and len(discovered_corporate_no) == 13:
            profile["corporate_no"] = discovered_corporate_no
        if not profile["address"] and official_fields.get("address"):
            profile["address"] = compact_text(official_fields["address"])
        if not profile["website"] and official_fields.get("homepage"):
            profile["website"] = compact_text(official_fields["homepage"])

    queries = enterprise_search_queries(payload, profile, maximum=max_queries)

    records: list[dict[str, Any]] = []
    search_errors: list[str] = []

    def search(query: str) -> list[dict[str, Any]]:
        client = cap_search_timeout(search_client_factory("serper"))
        return [
            record.to_dict()
            for record in client.search(
                query,
                limit=search_limit,
                query_intent="enterprise_db",
            )
        ]

    search_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        futures = {executor.submit(search, query): query for query in queries}
        for future in as_completed(futures):
            try:
                records.extend(future.result())
            except Exception as exc:
                search_errors.append(f"{futures[future]}: {type(exc).__name__}: {exc}")
    search_elapsed = time.monotonic() - search_started

    candidates = ranked_candidates({"records": records}, profile)
    linkedin_candidates = [candidate for candidate in candidates if is_linkedin_company_url(str(candidate["url"]))]
    ordinary_candidates = [candidate for candidate in candidates if not is_linkedin_company_url(str(candidate["url"]))]
    use_linkedin = bool(linkedin_identity and not profile["business_no"] and linkedin_candidates)
    if use_linkedin:
        selected = ordinary_candidates[: max(0, crawl_limit - 1)] + linkedin_candidates[:1]
    else:
        selected = ordinary_candidates[:crawl_limit]
    extracted: dict[str, str] = {}
    field_sources: dict[str, str] = {}
    web_sources: list[dict[str, object]] = []
    crawl_errors: list[str] = []
    crawl_started = time.monotonic()
    if selected:
        for candidate in selected:
            url = str(candidate["url"])
            try:
                crawler_name = "apify_linkedin" if is_linkedin_company_url(url) else "auto"
                crawler = crawler_factory(
                    crawler_name,
                    respect_robots=respect_robots,
                    max_latency_seconds=CRAWL_MAX_LATENCY_SECONDS,
                )
                result = crawler.crawl(url)
                if result.error:
                    raise RuntimeError(result.error)
                found = extract_fields(result.markdown or "", result.text or "")
                found.update(extract_linkedin_company_fields(result.to_dict()))
                matched, reason = identity_match(profile, candidate, result.to_dict(), found)
                if not matched:
                    crawl_errors.append(f"{url}: {reason}")
                    continue
                for field, value in found.items():
                    if field not in extracted:
                        extracted[field] = value
                        field_sources[field] = url
                web_sources.append(
                    {
                        "url": url,
                        "title": result.title or candidate.get("title") or "",
                        "crawler": result.crawler,
                        "status_code": result.status_code,
                        "identity_reason": reason,
                        "fields": [OUTPUT_FIELDS[field] for field in found if field in OUTPUT_FIELDS],
                    }
                )
            except Exception as exc:
                crawl_errors.append(f"{url}: {type(exc).__name__}: {exc}")
    crawl_elapsed = time.monotonic() - crawl_started

    enterprise: dict[str, object] = {
        "회사명": known["company"] or profile["company"],
    }
    english_names = field_values(payload, "english_company")
    brands = field_values(payload, "brand")
    raw_business_number = first_field(payload, "business_number")
    raw_corporate_number = first_field(payload, "corporate_number")
    business_number = raw_business_number if len(re.sub(r"\D", "", raw_business_number)) == 10 else ""
    corporate_number = raw_corporate_number if len(re.sub(r"\D", "", raw_corporate_number)) == 13 else ""
    if english_names:
        enterprise["영문회사명"] = english_names[0]
    if brands:
        enterprise["브랜드"] = brands
    if business_number:
        enterprise["사업자번호"] = business_number
    if corporate_number:
        enterprise["법인등록번호"] = corporate_number

    enriched_fields: list[str] = []
    response_sources: dict[str, str] = {}
    official_field_sources = official.get("field_sources") if isinstance(official.get("field_sources"), Mapping) else {}
    for field, output_name in OUTPUT_FIELDS.items():
        value = known[field] or official_fields.get(field, "") or extracted.get(field, "")
        if value:
            enterprise[output_name] = value
        if not known[field] and official_fields.get(field):
            enriched_fields.append(output_name)
            response_sources[output_name] = str(official_field_sources.get(field) or "")
        elif not known[field] and extracted.get(field):
            enriched_fields.append(output_name)
            response_sources[output_name] = field_sources[field]

    request_additional = {
        "english_company": english_names[0] if english_names else "",
        "business_number": business_number,
        "corporate_number": corporate_number,
    }
    for field, output_name in ADDITIONAL_OFFICIAL_FIELDS.items():
        request_value = request_additional.get(field, "")
        value = request_value or official_fields.get(field, "")
        if value and output_name not in enterprise:
            enterprise[output_name] = value
        if not request_value and official_fields.get(field) and output_name not in enriched_fields:
            enriched_fields.append(output_name)
            response_sources[output_name] = str(official_field_sources.get(field) or "")

    official_sources: list[dict[str, object]] = []
    for source in official.get("sources") or []:
        if not isinstance(source, Mapping):
            continue
        normalized_source = dict(source)
        normalized_source["fields"] = [
            CANONICAL_OUTPUT_FIELDS.get(str(field), str(field))
            for field in source.get("fields") or []
        ]
        official_sources.append(normalized_source)
    sources = official_sources + web_sources

    if enriched_fields:
        status = "enriched"
    elif sources:
        status = "matched_no_new_fields"
    elif records:
        status = "no_match"
    elif search_errors:
        status = "search_error"
    else:
        status = "no_match"

    search_task: dict[str, object] = {
        "name": "enterprise_search",
        "provider": "serper",
        "operation": "search",
        "status": "completed" if not search_errors else ("partial" if records else "failed"),
        "count": len(records),
        "elapsed_seconds": round(search_elapsed, 3),
    }
    crawl_task: dict[str, object] = {
        "name": "enterprise_crawl",
        "provider": "auto",
        "operation": "crawl",
        "status": "completed" if not crawl_errors else ("partial" if web_sources else "failed"),
        "count": len(web_sources),
        "elapsed_seconds": round(crawl_elapsed, 3),
    }
    item = {
        "status": status,
        "enterprise": enterprise,
        "enriched_fields": enriched_fields,
        "field_sources": response_sources,
        "sources": sources,
        "identity_label": "A" if official.get("identity_confirmed") or web_sources else None,
    }
    return {
        "schema_version": "enterprise_db/v1",
        "endpoint": ENTERPRISE_DB_ENDPOINT,
        "response_mode": "synchronous",
        "completion_status": "completed",
        "query": queries[0],
        "request": {
            "company": {"회사명": profile["company"]},
            "search_queries": queries,
            "search_limit": search_limit,
            "crawl_limit": crawl_limit,
            "linkedin_identity": linkedin_identity,
            "official_sources": use_official_sources,
            "respect_robots": respect_robots,
        },
        "status": status,
        "count": 1,
        "items": [item],
        "subtasks": [*(official.get("subtasks") or []), search_task, crawl_task],
        "raw": {
            "official_errors": official.get("errors") or [],
            "search_errors": search_errors,
            "crawl_errors": crawl_errors,
            "candidate_urls": [str(candidate["url"]) for candidate in selected],
        },
        "official_elapsed_seconds": round(official_elapsed, 3),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


if __name__ == "__main__":
    ready = provision_api_key(Path(os.getenv("ENTERPRISE_DB_API_KEY_FILE", DEFAULT_ENTERPRISE_DB_KEY_PATH)))
    print(f"enterpriseDB API key ready: {ready}")
