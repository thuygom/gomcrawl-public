from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote_plus, urlparse

from .crawlers import create_crawler
from .entity_match import identity_label_for_result
from .linkedin_finder import find_linkedin_profiles
from .llm_confidence import clean_bool
from .person_evidence_finder import find_person_evidence
from .news_quality import (
    apply_llm_value_intents,
    assess_news_quality,
    classify_value_intents_llm,
    empty_news_quality_stats,
    news_value_llm_disabled,
    record_noise_stat,
    record_value_stat,
)
from .schema import CrawlResult, EvidenceRecord
from .search import SearchAgentError, create_search_client
from .utils import canonicalize_url, compact_text


SearchClientFactory = Callable[[str], Any]
CrawlerFactory = Callable[..., Any]
LinkedInFinder = Callable[[Mapping[str, object]], dict[str, object]]
PersonEvidenceFinder = Callable[[Mapping[str, object]], dict[str, object]]

NOTEAPP_COMPLETE_PURPOSES = (
    "news",
    "publication",
    "award",
    "social",
    "profile",
    "company-homepage",
)
MAX_QUERY_LENGTH = 500
DEFAULT_LIMIT = 5
NEWS_RESPONSE_BUDGET_SECONDS = 8.5
NEWS_SEARCH_BUDGET_SECONDS = 5.0
NEWS_SEARCH_PROVIDER_TIMEOUT_SECONDS = 4.5
NEWS_THUMBNAIL_BUDGET_SECONDS = 2.0
NEWS_THUMBNAIL_MAX_LATENCY_SECONDS = 1.5
NEWS_THUMBNAIL_MAX_WORKERS = 2
NEWS_VALUE_LLM_COLLECT_SECONDS = 2.5
NEWS_RECENCY_DAYS = 183
NEWS_BUSINESS_CRAWL_LIMIT = 2
NEWS_BUSINESS_CRAWL_BUDGET_SECONDS = 3.0
NEWS_BUSINESS_CRAWL_MAX_LATENCY_SECONDS = 1.5
NEWS_BUSINESS_CRAWL_MIN_TEXT_CHARS = 300
NEWS_FIRECRAWL_FALLBACK_TIMEOUT_SECONDS = 3.0
NEWS_RESULT_ANALYSIS_VERSION = "v2"
COMPANY_HOMEPAGE_AUTO_MAX_LATENCY_SECONDS = 5.0
COMPANY_HOMEPAGE_FIRECRAWL_TIMEOUT_SECONDS = 12.0
COMPANY_HOMEPAGE_MIN_TEXT_CHARS = 200
NEWS_EXCLUDED_HOST_SUFFIXES = (
    "linkedin.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
)
KOREAN_BUSINESS_NEWS_SOURCES = {
    "etnews.com": "전자신문",
    "zdnet.co.kr": "지디넷코리아",
    "ddaily.co.kr": "디지털데일리",
    "bloter.net": "블로터",
    "platum.kr": "플래텀",
    "industrynews.co.kr": "인더스트리뉴스",
    "businesspost.co.kr": "비즈니스포스트",
    "byline.network": "바이라인네트워크",
    "itworld.co.kr": "ITWorld Korea",
    "dealsite.co.kr": "딜사이트",
    "thebell.co.kr": "더벨",
    "digitaltoday.co.kr": "디지털투데이",
    "techm.kr": "테크M",
    "aitimes.com": "AI타임스",
    "hellot.net": "헬로티",
    "kidd.co.kr": "산업일보",
    "irobotnews.com": "로봇신문",
    "startupn.kr": "스타트업엔",
    "biz.chosun.com": "조선비즈",
    "news1.kr": "뉴스1",
    "sedaily.com": "서울경제",
    "hankyung.com": "한국경제",
    "mk.co.kr": "매일경제",
    "mt.co.kr": "머니투데이",
    "fnnews.com": "파이낸셜뉴스",
    "ajunews.com": "아주경제",
    "ebn.co.kr": "EBN 산업경제",
    "newstomato.com": "뉴스토마토",
    "kpinews.kr": "KPI뉴스",
    "viva100.com": "브릿지경제",
}
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}")
URL_RE = re.compile(r"https?://\S+", re.I)
DIRECT_URL_CRAWLERS = {"stealth_browser", "playwright_stealth"}
CHALLENGE_TITLE_MARKERS = ("access denied", "attention required", "captcha", "just a moment")
CHALLENGE_TEXT_MARKERS = (
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies to continue",
    "cloudflare ray id",
)

PERSON_FIELD_SPECS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("이름", ("이름", "성명", "name", "person_name", "korean_name"), False),
    ("영문명", ("영문명", "영문 이름", "english_name", "englishName", "name_en", "eng_name"), False),
    ("직책", ("직책", "직위", "title", "position", "role", "job_title"), False),
    ("부서", ("부서", "소속", "department", "dept", "team", "division"), False),
    ("이메일", ("이메일", "메일", "email", "mail", "email_address"), False),
    ("전화번호", ("전화번호", "연락처", "휴대폰", "phone", "telephone", "mobile", "tel", "phone_number"), False),
)

COMPANY_FIELD_SPECS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("회사명", ("회사명", "회사", "법인명", "기업명", "company_name", "company", "corp_name"), False),
    ("학교", ("학교", "학력", "school", "schools", "university", "college", "대학교", "대학"), False),
    ("사업자번호", ("사업자번호", "사업자등록번호", "business_registration_number", "business_number", "biz_no", "brn"), False),
    ("법인번호", ("법인번호", "법인등록번호", "corporate_registration_number", "corporate_number", "corp_no", "crn"), False),
    ("대표자", ("대표자", "대표자명", "대표", "ceo_name", "ceo", "representative"), False),
    ("설립일", ("설립일", "founded_at", "founded_date", "established_at", "established"), False),
    ("상장여부", ("상장여부", "상장", "listed", "is_listed", "listing_status"), False),
    ("업종", ("업종", "산업", "industry", "sector", "business_type"), False),
    ("특허/인증", ("특허/인증", "특허", "인증", "patents_certifications", "patents", "certifications", "ip"), True),
    ("주소", ("주소", "소재지", "address", "addr", "location"), False),
    ("주요제품/서비스", ("주요제품/서비스", "주요제품", "주요서비스", "products_services", "main_products_services", "products", "services"), True),
    ("홈페이지", ("홈페이지", "웹사이트", "homepage", "home_page", "website", "url", "site"), False),
)

TOPLEVEL_SKIP_KEYS = {
    "query",
    "target",
    "subject",
    "reference",
    "person",
    "Person",
    "person_data",
    "company",
    "Company",
    "company_data",
    "limit",
    "start",
    "llm_confidence",
    "include_llm_confidence",
    "article_sentiment",
    "providers",
    "provider",
    "vertical",
    "type",
    "categories",
    "category",
    "serp_limit",
    "book_limit",
    "max_queries_per_source",
    "crawler",
    "fallback_crawler",
    "crawl_top",
    "crawl_limit",
    "crawl_business_news",
    "url",
    "intent",
}


def normalize_key(value: object) -> str:
    return re.sub(r"[\s_\-./]+", "", str(value or "").strip().lower())


def mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def as_text_values(value: object) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, Mapping):
        values: list[str] = []
        for item in value.values():
            values.extend(as_text_values(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(as_text_values(item))
        return values
    text = compact_text(value)
    return [text] if text else []


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = compact_text(value)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def pick_field_value(sources: list[Mapping[str, object]], aliases: tuple[str, ...], *, as_list: bool) -> object:
    normalized = {normalize_key(alias) for alias in aliases}
    collected: list[str] = []
    for source in sources:
        for key, value in source.items():
            if normalize_key(key) not in normalized:
                continue
            collected.extend(as_text_values(value))
    values = dedupe(collected)
    if as_list:
        return values
    return values[0] if values else ""


def build_structured_entities(payload: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    reference_payload = mapping_or_empty(payload.get("reference"))
    top_level = {
        key: value
        for key, value in payload.items()
        if key not in TOPLEVEL_SKIP_KEYS
    }
    person_sources = [
        top_level,
        mapping_or_empty(reference_payload.get("person") or reference_payload.get("Person")),
        mapping_or_empty(payload.get("person") or payload.get("Person") or payload.get("person_data")),
    ]
    company_sources = [
        top_level,
        mapping_or_empty(reference_payload.get("company") or reference_payload.get("Company")),
        mapping_or_empty(payload.get("company") or payload.get("Company") or payload.get("company_data")),
    ]

    person: dict[str, object] = {}
    for canonical, aliases, as_list in PERSON_FIELD_SPECS:
        value = pick_field_value(person_sources, aliases, as_list=as_list)
        if value not in ("", [], None):
            person[canonical] = value

    company: dict[str, object] = {}
    for canonical, aliases, as_list in COMPANY_FIELD_SPECS:
        value = pick_field_value(company_sources, aliases, as_list=as_list)
        if value not in ("", [], None):
            company[canonical] = value
    return person, company


def query_terms_for_purpose(person: Mapping[str, object], company: Mapping[str, object], purpose: str) -> list[str]:
    person_name = dedupe(as_text_values(person.get("이름")) + as_text_values(person.get("영문명")))
    company_name = dedupe(as_text_values(company.get("회사명")))
    role_terms = dedupe(as_text_values(person.get("직책")) + as_text_values(person.get("부서")) + as_text_values(company.get("업종")))

    if purpose == "profile":
        return dedupe(company_name + person_name + role_terms[:1])
    if purpose == "news":
        return dedupe(company_name + person_name + role_terms[:2])
    if purpose == "social":
        return dedupe(person_name + company_name + role_terms[:2])
    return dedupe(person_name + company_name + role_terms[:2])


def build_query(payload: Mapping[str, object], person: Mapping[str, object], company: Mapping[str, object], purpose: str) -> str:
    for key in ("query", "target", "subject"):
        text = compact_text(payload.get(key))
        if text:
            return text[:MAX_QUERY_LENGTH]
    query = " ".join(query_terms_for_purpose(person, company, purpose))
    return compact_text(query)[:MAX_QUERY_LENGTH]


def prepare_payload(payload: Mapping[str, object], purpose: str) -> dict[str, object]:
    person, company = build_structured_entities(payload)
    query = build_query(payload, person, company, purpose)
    if not query and purpose != "profile" and not payload.get("url"):
        raise ValueError("query or identifying person/company fields are required")
    prepared = dict(payload)
    if person:
        prepared["person"] = person
    if company:
        prepared["company"] = company
    if query:
        prepared["query"] = query
    return prepared


def normalize_complete_purpose(purpose: str) -> str:
    return compact_text(purpose).lower().replace("_", "-").replace(" ", "-")


def endpoint_for_purpose(purpose: str) -> str:
    purpose = normalize_complete_purpose(purpose)
    if purpose not in NOTEAPP_COMPLETE_PURPOSES:
        raise ValueError("unsupported noteapp complete purpose")
    return f"/v1/complete/{purpose}"


def clamp_limit(value: object, default: int = DEFAULT_LIMIT) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 10))


def host_label(url: object) -> str:
    parsed = urlparse(str(url or ""))
    host = parsed.netloc.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches_suffix(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def news_candidate_allowed(record: Mapping[str, object]) -> bool:
    raw = raw_map(record)
    urls = (
        record.get("url"),
        raw.get("originallink"),
        raw.get("link"),
        raw.get("displayLink"),
    )
    return not any(host_matches_suffix(host_label(url), NEWS_EXCLUDED_HOST_SUFFIXES) for url in urls)


def korean_business_news_source(value: object) -> str:
    host = host_label(value) or compact_text(value).lower().removeprefix("www.")
    for suffix, source_name in KOREAN_BUSINESS_NEWS_SOURCES.items():
        if host_matches_suffix(host, (suffix,)):
            return source_name
    return ""


def news_company_name(value: object) -> str:
    text = compact_text(value)
    for marker in ("주식회사", "(주)", "㈜"):
        text = text.replace(marker, "")
    return compact_text(text)


def news_search_queries(prepared: Mapping[str, object]) -> list[str]:
    company = mapping_or_empty(prepared.get("company"))
    person = mapping_or_empty(prepared.get("person"))
    company_names = [news_company_name(value) for value in as_text_values(company.get("회사명"))]
    person_names = as_text_values(person.get("이름")) + as_text_values(person.get("영문명"))
    company_target = next((value for value in company_names if value), "")
    target = company_target or next((value for value in person_names if value), "")
    if target:
        quoted = f'"{target.replace(chr(34), "").strip()}"'
        if company_target:
            return [f"{quoted} 뉴스", f"{quoted} 기사"]
        return [quoted, f"{quoted} 뉴스"]
    query = compact_text(prepared.get("query"))
    return dedupe([query, f"{query} 뉴스"])


def news_identity_terms(prepared: Mapping[str, object]) -> list[str]:
    company = mapping_or_empty(prepared.get("company"))
    company_names = as_text_values(company.get("회사명"))
    if company_names:
        return dedupe(company_names + [news_company_name(value) for value in company_names])
    person = mapping_or_empty(prepared.get("person"))
    return dedupe(as_text_values(person.get("이름")) + as_text_values(person.get("영문명")))


def parse_news_datetime(value: object, *, now: datetime | None = None) -> datetime | None:
    text = compact_text(value)
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None:
        for date_format in ("%b %d, %Y", "%B %d, %Y", "%Y. %m. %d.", "%Y.%m.%d."):
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
    if parsed is None:
        relative = re.fullmatch(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", text, re.I)
        korean_relative = re.fullmatch(r"(\d+)\s*(분|시간|일|주|개월|년)\s*전", text)
        match = relative or korean_relative
        if match:
            unit_days = {
                "minute": 1 / 1440,
                "hour": 1 / 24,
                "day": 1,
                "week": 7,
                "month": 30,
                "year": 365,
                "분": 1 / 1440,
                "시간": 1 / 24,
                "일": 1,
                "주": 7,
                "개월": 30,
                "년": 365,
            }
            current = now or datetime.now(timezone.utc)
            parsed = current - timedelta(days=int(match.group(1)) * unit_days[match.group(2).lower()])
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def news_record_datetime(record: Mapping[str, object], *, now: datetime | None = None) -> datetime | None:
    raw = raw_map(record)
    return parse_news_datetime(
        record.get("published_at") or raw.get("pubDate") or raw.get("pubdate") or raw.get("date"),
        now=now,
    )


def news_record_matches_identity(
    record: Mapping[str, object], terms: list[str], *, title_only: bool = False
) -> bool:
    if not terms:
        return True
    text = record_title(record) if title_only else f"{record_title(record)} {record_snippet(record)}"
    text = text.casefold()
    for term in terms:
        needle = compact_text(term).casefold()
        start = text.find(needle)
        while start >= 0:
            if start == 0 or not text[start - 1].isalnum():
                return True
            start = text.find(needle, start + 1)
    return False


def news_record_is_eligible(
    record: Mapping[str, object],
    *,
    identity_terms: list[str],
    cutoff: datetime,
    now: datetime,
    business_only: bool = False,
    identity_title_only: bool = False,
) -> bool:
    if not _news_record_matches_base_policy(
        record,
        identity_terms=identity_terms,
        cutoff=cutoff,
        now=now,
        business_only=business_only,
        identity_title_only=identity_title_only,
    ):
        return False
    return not bool(assess_news_quality(record).get("is_noise"))


def _news_record_matches_base_policy(
    record: Mapping[str, object],
    *,
    identity_terms: list[str],
    cutoff: datetime,
    now: datetime,
    business_only: bool = False,
    identity_title_only: bool = False,
) -> bool:
    raw = raw_map(record)
    if not news_candidate_allowed(record) or not news_record_matches_identity(
        record, identity_terms, title_only=identity_title_only
    ):
        return False
    if business_only and not any(
        korean_business_news_source(value)
        for value in (record.get("url"), raw.get("originallink"), raw.get("link"), raw.get("displayLink"))
    ):
        return False
    published = news_record_datetime(record, now=now)
    return published is not None and published >= cutoff


def finalize_news_records(
    records: list[dict[str, object]], *, now: datetime, limit: int
) -> list[dict[str, object]]:
    def sort_key(record: Mapping[str, object]) -> tuple[datetime, bool]:
        raw = raw_map(record)
        is_business_news = any(
            korean_business_news_source(value)
            for value in (record.get("url"), raw.get("originallink"), raw.get("link"), raw.get("displayLink"))
        )
        return news_record_datetime(record, now=now) or datetime.min.replace(tzinfo=timezone.utc), is_business_news

    unique: list[dict[str, object]] = []
    seen_titles: set[str] = set()
    for record in sorted(records, key=sort_key, reverse=True):
        title_key = normalize_key(record_title(record))
        if title_key and title_key in seen_titles:
            continue
        if title_key:
            seen_titles.add(title_key)
        unique.append(record)
    return unique[:limit]


def bounded_remaining_seconds(deadline: float, cap: float) -> float:
    return max(0.0, min(cap, deadline - time.monotonic()))


def cap_client_timeout(client: object, timeout_seconds: float) -> object:
    if not hasattr(client, "timeout"):
        return client
    try:
        current = float(getattr(client, "timeout"))
    except (TypeError, ValueError):
        current = timeout_seconds
    setattr(client, "timeout", min(current, timeout_seconds))
    return client


def first_year(*values: object) -> str:
    for value in values:
        for text in as_text_values(value):
            normalized = compact_text(text).replace("\\n", "\n")
            if len(normalized) >= 4 and normalized[:4].isdigit() and normalized[:4].startswith(("19", "20")):
                return normalized[:4]
            match = YEAR_RE.search(normalized)
            if match:
                return match.group(0)
    return ""


def record_title(record: Mapping[str, object]) -> str:
    return compact_text(record.get("title"))


def record_snippet(record: Mapping[str, object]) -> str:
    return compact_text(record.get("snippet"))


def raw_map(record: Mapping[str, object]) -> Mapping[str, object]:
    return mapping_or_empty(record.get("raw_payload_json"))


def keep_present(values: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}


def thumbnail_urls_from_item(item: Mapping[str, object]) -> list[str]:
    urls: list[str] = []

    def visit(value: object, image_context: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = normalize_key(key)
                visit(child, image_context or any(token in normalized for token in ("image", "thumbnail", "thumb", "picture")))
            return
        if isinstance(value, (list, tuple, set)):
            for child in value:
                visit(child, image_context)
            return
        if not image_context:
            return
        text = str(value or "").strip()
        parsed = urlparse(text)
        if parsed.scheme in {"http", "https"} and parsed.netloc and text not in urls:
            urls.append(text)

    visit(item)
    return urls


def _contains_identity_value(values: object, text: object) -> bool:
    haystack = normalize_key(text)
    if not haystack:
        return False
    return any(normalize_key(value) and normalize_key(value) in haystack for value in as_text_values(values))


def _collect_named_values(value: object, names: tuple[str, ...]) -> list[str]:
    wanted = {normalize_key(name) for name in names}
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if normalize_key(key) in wanted:
                found.extend(as_text_values(child))
            else:
                found.extend(_collect_named_values(child, names))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            found.extend(_collect_named_values(child, names))
    return dedupe(found)


def _item_raw_payload(item: Mapping[str, object]) -> Mapping[str, object]:
    raw = item.get("raw_payload_json") or item.get("raw_payload")
    return raw if isinstance(raw, Mapping) else {}


def _reference_identity_fields(reference: Mapping[str, object]) -> tuple[list[str], list[str], list[str], list[str]]:
    person, company = build_structured_entities(reference)
    names = dedupe(as_text_values(person.get("이름")) + as_text_values(person.get("영문명")))
    contexts = dedupe(
        as_text_values(company.get("회사명"))
        + as_text_values(company.get("학교"))
        + as_text_values(person.get("직책"))
        + as_text_values(person.get("부서"))
    )
    companies = dedupe(as_text_values(company.get("회사명")))
    schools = dedupe(as_text_values(company.get("학교")))
    return names, contexts, companies, schools


def _labelled_values(text: object, labels: tuple[str, ...]) -> list[str]:
    wanted = {normalize_key(label) for label in labels}
    values: list[str] = []
    for line in str(text or "").replace("|", "\n").splitlines():
        left, separator, right = line.partition(":")
        if separator and normalize_key(left) in wanted:
            values.append(right.strip())
    return dedupe(values)


def _cooccurs(values_left: object, values_right: object, text: object) -> bool:
    segments = str(text or "")
    for separator in ("\n", "|", ".", "。", "!", "！", "?", "？"):
        segments = segments.replace(separator, "\n")
    return any(
        _contains_identity_value(values_left, segment) and _contains_identity_value(values_right, segment)
        for segment in segments.splitlines()
        if segment.strip()
    )


def _publication_identity_label(item: Mapping[str, object], reference: Mapping[str, object]) -> str | None:
    names, _contexts, companies, schools = _reference_identity_fields(reference)
    raw = _item_raw_payload(item)
    author_values = dedupe(
        as_text_values(item.get("author_role"))
        + _collect_named_values(raw, ("author", "authors", "writer", "creator", "author_name", "authorName"))
    )
    author_text = " ".join(author_values)
    if not _contains_identity_value(names, author_text):
        return None
    publication_contexts = dedupe(companies + schools)
    if not publication_contexts:
        return "A"
    context_values = dedupe(
        author_values
        + as_text_values(item.get("summary"))
        + as_text_values(item.get("description"))
        + _collect_named_values(
            raw,
            ("affiliation", "affiliations", "institution", "organization", "company", "companyName", "school", "university"),
        )
    )
    return "A" if _contains_identity_value(publication_contexts, " ".join(context_values)) else "D"


def _award_identity_label(item: Mapping[str, object], reference: Mapping[str, object]) -> str | None:
    names, contexts, _companies, _schools = _reference_identity_fields(reference)
    raw = _item_raw_payload(item)
    description = " ".join(as_text_values(item.get("description")))
    recipient_values = dedupe(
        _labelled_values(description, ("성명", "수상자", "수상인", "recipient", "awardee", "winner"))
        + _collect_named_values(raw, ("recipient", "recipient_name", "awardee", "winner", "person_name", "person"))
    )
    if not recipient_values or not _contains_identity_value(names, " ".join(recipient_values)):
        return None
    if not contexts:
        return "A"
    context_text = " ".join(
        [
            description,
            " ".join(_labelled_values(description, ("소속", "기관", "affiliation", "organization", "company"))),
            " ".join(_collect_named_values(raw, ("affiliation", "organization", "company", "companyName", "institution"))),
        ]
    )
    return "A" if _contains_identity_value(contexts, context_text) else "D"


def _social_identity_label(item: Mapping[str, object], reference: Mapping[str, object]) -> str | None:
    names, _contexts, companies, schools = _reference_identity_fields(reference)
    contexts = dedupe(companies + schools)
    raw = _item_raw_payload(item)
    platform = compact_text(item.get("platform")).lower()
    if platform == "linkedin":
        owner_values = _collect_named_values(raw, ("name", "fullName", "profileName", "firstName", "lastName"))
        owner_text = " ".join(owner_values + as_text_values(item.get("title")))
    else:
        owner_values = _collect_named_values(raw, ("channelTitle", "channelName", "owner", "creator"))
        if not owner_values:
            return "D" if _contains_identity_value(names, item.get("title")) else None
        owner_text = " ".join(owner_values)
    if not _contains_identity_value(names, owner_text):
        return None
    if not contexts:
        return "A" if platform == "linkedin" else "D"
    context_text = " ".join(
        as_text_values(item.get("title"))
        + as_text_values(item.get("snippet"))
        + _collect_named_values(
            raw,
            (
                "headline",
                "occupation",
                "currentPosition",
                "company",
                "companyName",
                "currentCompany",
                "currentCompanies",
                "school",
                "schools",
                "university",
                "description",
                "channelDescription",
            ),
        )
    )
    return "A" if _contains_identity_value(contexts, context_text) else "D"


def _profile_identity_label(item: Mapping[str, object], reference: Mapping[str, object]) -> str | None:
    names, _contexts, companies, schools = _reference_identity_fields(reference)
    contexts = dedupe(companies + schools)
    raw = _item_raw_payload(item)
    crawl = mapping_or_empty(raw.get("profile_crawl") or raw.get("crawl"))
    profile_text = " ".join(
        as_text_values(item.get("title"))
        + as_text_values(item.get("education"))
        + as_text_values(item.get("career_history"))
        + _collect_named_values(crawl, ("title", "text_preview", "text"))
    )
    if not _contains_identity_value(names, profile_text):
        return None
    if not contexts:
        return "A"
    context_text = " ".join(
        as_text_values(item.get("education"))
        + as_text_values(item.get("career_history"))
        + _collect_named_values(crawl, ("text_preview", "text"))
    )
    return "A" if _contains_identity_value(contexts, context_text) else "D"


def _company_homepage_identity_label(item: Mapping[str, object], reference: Mapping[str, object]) -> str | None:
    company = mapping_or_empty(reference.get("company"))
    companies = as_text_values(company.get("회사명"))
    evidence = " ".join(
        as_text_values(item.get("title"))
        + as_text_values(item.get("text_preview"))
        + as_text_values(item.get("homepage_url"))
    )
    if _contains_identity_value(companies, evidence):
        return "C"
    requested_hosts = {host_label(value) for value in as_text_values(company.get("홈페이지")) if host_label(value)}
    return "C" if host_label(item.get("homepage_url")) in requested_hosts else None


def _news_identity_label(item: Mapping[str, object], reference: Mapping[str, object]) -> str | None:
    names, contexts, companies, _schools = _reference_identity_fields(reference)
    crawl = mapping_or_empty(mapping_or_empty(item.get("raw_payload_json")).get("business_news_crawl"))
    evidence_parts = (
        as_text_values(item.get("title"))
        + as_text_values(item.get("snippet"))
        + as_text_values(item.get("_identity_text"))
        + as_text_values(crawl.get("text_preview"))
    )
    evidence_text = " ".join(evidence_parts)
    name_match = _contains_identity_value(names, evidence_text)
    company_match = _contains_identity_value(companies, evidence_text)
    company_keys = {normalize_key(company_value) for company_value in companies}
    context_values = [value for value in contexts if normalize_key(value) not in company_keys]
    if any(_cooccurs(names, companies, part) for part in evidence_parts):
        return "A"
    if name_match and any(_cooccurs(names, context_values, part) for part in evidence_parts):
        return "B"
    if name_match and contexts:
        return "D"
    return "D" if name_match else ("C" if company_match else None)


def purpose_identity_label(purpose: str, item: Mapping[str, object], reference: Mapping[str, object]) -> str | None:
    if purpose == "publication":
        return _publication_identity_label(item, reference)
    if purpose == "award":
        return _award_identity_label(item, reference)
    if purpose == "social":
        return _social_identity_label(item, reference)
    if purpose == "profile":
        return _profile_identity_label(item, reference)
    if purpose == "news":
        return _news_identity_label(item, reference)
    if purpose == "company-homepage":
        return _company_homepage_identity_label(item, reference)
    return identity_label_for_result(reference, item)


def finalize_complete_items(result: dict[str, object]) -> None:
    request = mapping_or_empty(result.get("request"))
    reference = {
        "person": mapping_or_empty(request.get("person")),
        "company": mapping_or_empty(request.get("company")),
    }
    items = result.get("items")
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        scores = mapping_or_empty(item.get("scores"))
        identity_match = mapping_or_empty(scores.get("identity_match"))
        label = purpose_identity_label(str(result.get("purpose") or ""), item, reference)
        item["identity_label"] = label
        if isinstance(scores.get("identity_match"), dict):
            scores["identity_match"]["identity_label"] = label
        if result.get("purpose") == "news":
            selected = thumbnail_urls_from_item({"thumb_urls": item.get("thumb_urls")})
            thumb_urls = dedupe(selected + thumbnail_urls_from_item(item))
            item["thumb_urls"] = thumb_urls
            item["primary_thumb_url"] = thumb_urls[0] if thumb_urls else None


def score_summary_from_record(record: Mapping[str, object]) -> dict[str, object]:
    scores = keep_present(
        {
            "confidence": record.get("confidence"),
            "confidence_reason": record.get("confidence_reason"),
            "base_confidence": record.get("base_confidence"),
            "base_confidence_reason": record.get("base_confidence_reason"),
            "llm_confidence": record.get("llm_confidence"),
            "identity_match_score": record.get("identity_match_score"),
            "identity_match": record.get("identity_match"),
            "policy_risk": record.get("policy_risk"),
            "processing_status": record.get("processing_status"),
        }
    )
    identity = mapping_or_empty(record.get("identity_match"))
    if identity and "identity_match_score" not in scores and identity.get("score") is not None:
        scores["identity_match_score"] = identity.get("score")
    return scores


def score_summary_from_candidate(candidate: Mapping[str, object]) -> dict[str, object]:
    identity = mapping_or_empty(candidate.get("identity_match"))
    primary_record = mapping_or_empty(candidate.get("primary_record"))
    scores = keep_present(
        {
            "workflow_score": candidate.get("workflow_score"),
            "workflow_score_reasons": candidate.get("workflow_score_reasons"),
            "identity_match_score": identity.get("score") if identity else None,
            "identity_match": identity,
            "llm_confidence": candidate.get("llm_confidence"),
            "rank": candidate.get("rank"),
            "evidence_count": candidate.get("evidence_count"),
        }
    )
    source_record_scores = score_summary_from_record(primary_record)
    if source_record_scores:
        scores["source_record"] = source_record_scores
    return scores


def raw_payload_from_record(record: Mapping[str, object], scores: Mapping[str, object]) -> dict[str, object]:
    return {
        "record": {
            "result_id": record.get("result_id"),
            "source": record.get("source"),
            "source_type": record.get("source_type"),
            "url": record.get("url"),
            "canonical_url": record.get("canonical_url"),
            "published_at": record.get("published_at"),
            "retrieved_at": record.get("retrieved_at"),
            "query": record.get("query"),
            "query_intent": record.get("query_intent"),
            "raw_payload_json": dict(raw_map(record)),
        },
        "scores": dict(scores),
    }


def raw_payload_from_candidate(candidate: Mapping[str, object], scores: Mapping[str, object]) -> dict[str, object]:
    primary_record = mapping_or_empty(candidate.get("primary_record"))
    return {
        "candidate": keep_present(
            {
                "candidate_id": candidate.get("candidate_id"),
                "category": candidate.get("category"),
                "source": candidate.get("source"),
                "source_label": candidate.get("source_label"),
                "source_url": candidate.get("source_url"),
                "providers": candidate.get("providers"),
                "queries": candidate.get("queries"),
                "rank": candidate.get("rank"),
                "evidence_count": candidate.get("evidence_count"),
            }
        ),
        "primary_record": dict(primary_record),
        "scores": dict(scores),
    }


def subtask(
    name: str,
    *,
    provider: str,
    operation: str,
    count: int | None = None,
    elapsed_seconds: float | None = None,
    categories: list[str] | None = None,
) -> dict[str, object]:
    return keep_present(
        {
            "name": name,
            "provider": provider,
            "operation": operation,
            "status": "completed",
            "count": count,
            "elapsed_seconds": round(elapsed_seconds, 3) if elapsed_seconds is not None else None,
            "categories": categories,
        }
    )


def timed_subtask(
    name: str,
    *,
    provider: str,
    operation: str,
    callback: Callable[[], Any],
    categories: list[str] | None = None,
) -> tuple[Any, dict[str, object]]:
    started = time.monotonic()
    result = callback()
    count = None
    if isinstance(result, list):
        count = len(result)
    elif isinstance(result, Mapping):
        count = len(result.get("candidates") or result.get("items") or [])
    return result, subtask(
        name,
        provider=provider,
        operation=operation,
        count=count,
        elapsed_seconds=time.monotonic() - started,
        categories=categories,
    )


def synchronous_response_fields(subtasks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "response_mode": "synchronous",
        "completion_status": "completed",
        "subtasks": subtasks,
    }


def news_items(records: list[dict[str, object]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for record in records:
        raw = raw_map(record)
        scores = score_summary_from_record(record)
        source_url = raw.get("originallink") or record.get("url")
        quality = assess_news_quality(record)
        items.append(
            {
                "title": record_title(record),
                "source": host_label(source_url) or host_label(record.get("url")),
                "article_url": str(record.get("url") or ""),
                "snippet": record_snippet(record),
                "published_at": record.get("published_at"),
                "scores": scores,
                "raw_payload_json": raw_payload_from_record(record, scores),
                "news_quality": quality,
            }
        )
    return items


def news_item_crawl_url(item: Mapping[str, object]) -> str:
    raw_payload = mapping_or_empty(item.get("raw_payload_json"))
    record = mapping_or_empty(raw_payload.get("record"))
    provider_raw = mapping_or_empty(record.get("raw_payload_json"))
    return str(provider_raw.get("originallink") or item.get("article_url") or "").strip()


def business_news_source_for_item(item: Mapping[str, object]) -> str:
    for value in (item.get("source"), news_item_crawl_url(item), item.get("article_url")):
        if source := korean_business_news_source(value):
            return source
    return ""


def crawl_business_news_items(
    items: list[dict[str, object]],
    *,
    crawler_factory: CrawlerFactory,
    deadline: float,
) -> dict[str, object]:
    targets: list[tuple[dict[str, object], str, str]] = []
    for item in items:
        source = business_news_source_for_item(item)
        if not source:
            continue
        item["business_news_source"] = source
        targets.append((item, source, news_item_crawl_url(item)))

    selected = targets[:NEWS_BUSINESS_CRAWL_LIMIT]
    attempts: list[dict[str, object]] = []
    if not selected:
        return {"target_count": 0, "attempted_count": 0, "success_count": 0, "attempts": attempts}

    def crawl(url: str) -> CrawlResult:
        return crawler_factory(
            "auto",
            respect_robots=True,
            max_latency_seconds=NEWS_BUSINESS_CRAWL_MAX_LATENCY_SECONDS,
        ).crawl(url)

    executor = ThreadPoolExecutor(max_workers=len(selected))
    try:
        futures = {executor.submit(crawl, url): (item, source, url) for item, source, url in selected}
        done, _pending = wait(
            futures,
            timeout=bounded_remaining_seconds(deadline, NEWS_BUSINESS_CRAWL_BUDGET_SECONDS),
        )
        for future, (item, source, url) in futures.items():
            if future not in done:
                attempts.append({"url": url, "source": source, "status": "timed_out"})
                continue
            try:
                result = future.result()
            except Exception as exc:
                attempts.append({"url": url, "source": source, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
                continue

            text = compact_text(result.markdown or result.text)
            bot_detection = mapping_or_empty(result.raw_payload.get("bot_detection"))
            failed = (
                crawl_failure(result)
                or bool(challenge_page_reason(result))
                or bool(bot_detection.get("detected"))
                or len(text) < NEWS_BUSINESS_CRAWL_MIN_TEXT_CHARS
            )
            crawl_payload = profile_crawl_payload(result)
            raw_payload = item.get("raw_payload_json")
            if isinstance(raw_payload, dict):
                raw_payload["business_news_crawl"] = crawl_payload
            if failed:
                attempts.append(
                    {
                        "url": url,
                        "source": source,
                        "status": "failed",
                        "crawler": result.crawler,
                        "text_length": len(text),
                        "error": result.error or challenge_page_reason(result) or "insufficient article text",
                    }
                )
                continue

            item["article_text_preview"] = text[:4000]
            item["thumb_urls"] = thumbnail_urls_from_item({"image_urls": result.image_urls})
            attempts.append(
                {
                    "url": url,
                    "source": source,
                    "status": "completed",
                    "crawler": result.crawler,
                    "text_length": len(text),
                }
            )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return {
        "target_count": len(targets),
        "attempted_count": len(selected),
        "success_count": sum(attempt.get("status") == "completed" for attempt in attempts),
        "attempts": attempts,
    }


def enrich_news_thumbnails(
    items: list[dict[str, object]],
    *,
    crawler_factory: CrawlerFactory,
    deadline: float | None = None,
) -> None:
    def crawl(item: Mapping[str, object]) -> list[str]:
        url = news_item_crawl_url(item)
        if not url:
            return []
        try:
            result = crawler_factory(
                "static",
                respect_robots=False,
                max_latency_seconds=NEWS_THUMBNAIL_MAX_LATENCY_SECONDS,
            ).crawl(url)
        except Exception:
            return []
        if result.error or (isinstance(result.status_code, int) and result.status_code >= 400):
            return []
        if challenge_page_reason(result):
            return []
        bot_detection = mapping_or_empty(result.raw_payload.get("bot_detection"))
        if bot_detection.get("detected"):
            return []
        return thumbnail_urls_from_item({"image_urls": result.image_urls})

    for item in items:
        item.setdefault("thumb_urls", [])
    if not items:
        return
    budget = NEWS_THUMBNAIL_BUDGET_SECONDS
    if deadline is not None:
        budget = bounded_remaining_seconds(deadline, NEWS_THUMBNAIL_BUDGET_SECONDS)
    if budget <= 0:
        return
    executor = ThreadPoolExecutor(max_workers=min(len(items), NEWS_THUMBNAIL_MAX_WORKERS))
    try:
        futures = {executor.submit(crawl, item): item for item in items}
        done, _pending = wait(futures, timeout=budget)
        for future in done:
            item = futures[future]
            try:
                item["thumb_urls"] = future.result()
            except Exception:
                item["thumb_urls"] = []
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def source_label_from_candidate(candidate: Mapping[str, object]) -> str:
    return compact_text(candidate.get("source_label") or candidate.get("source") or host_label(candidate.get("url")))


def description_from_candidate(candidate: Mapping[str, object], *, max_chars: int = 400) -> str:
    crawl_result = mapping_or_empty(candidate.get("crawl_result"))
    if crawl_result:
        text = compact_text(crawl_result.get("text_preview") or crawl_result.get("text") or "")
        if text:
            return text[:max_chars]
    primary_record = mapping_or_empty(candidate.get("primary_record"))
    text = record_snippet(primary_record)
    if text:
        return text[:max_chars]
    return record_title(primary_record)[:max_chars]


def publisher_from_candidate(candidate: Mapping[str, object]) -> str:
    primary_record = mapping_or_empty(candidate.get("primary_record"))
    raw = raw_map(primary_record)
    for key in ("publisher", "journal", "press", "agency", "organization"):
        value = compact_text(raw.get(key))
        if value:
            return value
    return source_label_from_candidate(candidate)


def author_role_from_candidate(candidate: Mapping[str, object]) -> str:
    primary_record = mapping_or_empty(candidate.get("primary_record"))
    raw = raw_map(primary_record)
    for key in ("author", "authors", "writer", "creator"):
        value = ", ".join(as_text_values(raw.get(key)))
        if value:
            return value
    return ""


def publication_items(result: Mapping[str, object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for candidate in result.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        if str(candidate.get("category") or "") not in {"books", "publications"}:
            continue
        scores = score_summary_from_candidate(candidate)
        item = {
            "title": compact_text(candidate.get("title")),
            "publisher": publisher_from_candidate(candidate),
            "publication_year": first_year(
                mapping_or_empty(candidate.get("primary_record")).get("published_at"),
                mapping_or_empty(raw_map(mapping_or_empty(candidate.get("primary_record")))).get("pubdate"),
                description_from_candidate(candidate),
                candidate.get("title"),
            ),
            "summary": description_from_candidate(candidate),
            "author_role": author_role_from_candidate(candidate),
            "url": str(candidate.get("url") or ""),
            "category": str(candidate.get("category") or ""),
            "scores": scores,
            "raw_payload_json": raw_payload_from_candidate(candidate, scores),
        }
        items.append(item)
    return items


def awarding_org_from_candidate(candidate: Mapping[str, object]) -> str:
    primary_record = mapping_or_empty(candidate.get("primary_record"))
    raw = raw_map(primary_record)
    for key in ("organization", "publisher", "press", "agency"):
        value = compact_text(raw.get(key))
        if value:
            return value
    label = source_label_from_candidate(candidate)
    return "대한민국 상훈" if label == "Republic of Korea honors" else label


def award_items(result: Mapping[str, object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for candidate in result.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        if str(candidate.get("category") or "") != "awards":
            continue
        description = description_from_candidate(candidate)
        scores = score_summary_from_candidate(candidate)
        items.append(
            {
                "award_name": compact_text(candidate.get("title")),
                "awarding_organization": awarding_org_from_candidate(candidate),
                "award_year": first_year(description, candidate.get("title")),
                "description": description,
                "url": str(candidate.get("url") or ""),
                "scores": scores,
                "raw_payload_json": raw_payload_from_candidate(candidate, scores),
            }
        )
    return items


def social_items(linkedin_result: Mapping[str, object], youtube_records: list[dict[str, object]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for candidate in linkedin_result.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        scores = score_summary_from_candidate(candidate)
        items.append(
            {
                "platform": "LinkedIn",
                "title": compact_text(candidate.get("title")),
                "published_at": mapping_or_empty(candidate.get("primary_record")).get("published_at"),
                "url": str(candidate.get("url") or ""),
                "scores": scores,
                "raw_payload_json": raw_payload_from_candidate(candidate, scores),
            }
        )
    for record in youtube_records:
        scores = score_summary_from_record(record)
        items.append(
            {
                "platform": "YouTube",
                "title": record_title(record),
                "published_at": record.get("published_at"),
                "url": str(record.get("url") or ""),
                "scores": scores,
                "raw_payload_json": raw_payload_from_record(record, scores),
            }
        )
    return items


def collapse_line(value: str) -> str:
    text = compact_text(value)
    text = re.sub(r"^[#>*\-\d.\s|]+", "", text).strip()
    return compact_text(text)


def normalized_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in str(text or "").replace("\\n", "\n").splitlines():
        line = collapse_line(raw)
        if not line:
            continue
        if URL_RE.fullmatch(line):
            continue
        if len(line) > 220:
            continue
        lines.append(line)
    return dedupe(lines)


def extract_section(lines: list[str], headings: tuple[str, ...], stop_headings: tuple[str, ...]) -> list[str]:
    collected: list[str] = []
    active = False
    for line in lines:
        lower = line.casefold()
        if any(lower == heading.casefold() or lower.startswith(f"{heading.casefold()} ") for heading in headings):
            active = True
            inline = line.split(" ", 1)[1].strip() if " " in line else ""
            if inline:
                collected.append(inline)
            continue
        if active and any(lower == heading.casefold() or lower.startswith(f"{heading.casefold()} ") for heading in stop_headings):
            break
        if active:
            collected.append(line)
    return dedupe(collected)


def extract_profile_fields(markdown_text: str) -> dict[str, list[str]]:
    lines = normalized_lines(markdown_text)
    education = extract_section(lines, ("학력", "학력사항", "education"), ("경력", "경력사항", "약력", "career"))
    career = extract_section(lines, ("경력", "경력사항", "약력", "career"), ("학력", "학력사항", "education"))

    if not education:
        education = [
            line
            for line in lines
            if any(token in line for token in ("대학교", "대학원", "University", "College", "School"))
        ][:8]
    if not career:
        career = [
            line
            for line in lines
            if any(token in line for token in ("대표", "이사", "재직", "근무", "경력", "Career", "CEO", "Founder", "교수", "박사"))
        ][:10]
    return {
        "education": dedupe(education),
        "career_history": dedupe(career),
    }


def profile_crawl_payload(result: CrawlResult) -> dict[str, object]:
    text = compact_text(result.text)
    return {
        "url": result.url,
        "canonical_url": result.canonical_url,
        "crawler": result.crawler,
        "title": result.title,
        "status_code": result.status_code,
        "text_length": len(text),
        "text_preview": text[:4000],
        "error": result.error,
        "raw_payload": dict(result.raw_payload or {}),
    }


def crawl_score_summary(result: CrawlResult) -> dict[str, object]:
    confidence = 0.7 if result.text and not result.error else 0.1
    reason = f"{result.crawler} crawl"
    if result.error:
        reason = f"{result.crawler} crawl error: {result.error}"
    return {
        "confidence": confidence,
        "confidence_reason": reason,
        "processing_status": "raw" if not result.error else "error",
    }


def explicit_public_url(payload: Mapping[str, object]) -> str:
    url = str(payload.get("url") or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if len(url) > 2048 or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be a public http/https URL")
    if parsed.username or parsed.password:
        raise ValueError("url credentials are not accepted")
    return url


def direct_crawler_name(value: object, *, default: str = "") -> str:
    name = str(value or default).strip().lower().replace("-", "_")
    if name and name not in DIRECT_URL_CRAWLERS:
        allowed = ", ".join(sorted(DIRECT_URL_CRAWLERS))
        raise ValueError(f"unsupported direct URL crawler; allowed: {allowed}")
    return name


def crawl_failure(result: CrawlResult) -> bool:
    if result.error:
        return True
    if isinstance(result.status_code, int) and result.status_code >= 400:
        return True
    return not bool(compact_text(result.text))


def challenge_page_reason(result: CrawlResult) -> str:
    title = compact_text(result.title).lower()
    leading_text = compact_text(result.text)[:1500].lower()
    if any(marker in title for marker in CHALLENGE_TITLE_MARKERS):
        return "challenge page title detected"
    if any(marker in leading_text for marker in CHALLENGE_TEXT_MARKERS):
        return "challenge page content detected"
    return ""


def crawl_explicit_url(
    payload: Mapping[str, object],
    *,
    crawler_factory: CrawlerFactory,
) -> tuple[CrawlResult, list[dict[str, object]], str, str]:
    url = explicit_public_url(payload)
    primary = direct_crawler_name(payload.get("crawler"), default="stealth_browser")
    fallback = direct_crawler_name(payload.get("fallback_crawler"))
    names = [primary]
    if fallback and fallback != primary:
        names.append(fallback)

    attempts: list[dict[str, object]] = []
    for name in names:
        try:
            result = crawler_factory(name).crawl(url)
        except Exception as exc:
            attempts.append({"crawler": name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            continue

        challenge = challenge_page_reason(result)
        bot_detection = (
            result.raw_payload.get("bot_detection")
            if isinstance(result.raw_payload.get("bot_detection"), Mapping)
            else {}
        )
        if bot_detection.get("detected"):
            challenge = f"bot challenge detected: {bot_detection.get('vendor') or 'unknown'}"
        failed = crawl_failure(result)
        attempts.append(
            {
                "crawler": name,
                "status": "failed" if failed or challenge else "completed",
                "status_code": result.status_code,
                "text_length": len(result.text or ""),
                "error": challenge or result.error,
            }
        )
        if challenge:
            raise SearchAgentError(f"{challenge}; refusing crawler fallback")
        if not failed:
            return result, attempts, primary, fallback

    detail = "; ".join(f"{item['crawler']}: {item.get('error') or 'empty result'}" for item in attempts)
    raise SearchAgentError(f"explicit URL crawl failed: {detail}")


def explicit_url_item(purpose: str, result: CrawlResult, attempts: list[dict[str, object]]) -> dict[str, object]:
    crawl_payload = profile_crawl_payload(result)
    scores = crawl_score_summary(result)
    raw_payload = {"crawl": crawl_payload, "attempts": attempts, "scores": scores}
    title = result.title or result.canonical_url
    summary = compact_text(result.markdown or result.text)[:4000]
    source = host_label(result.url)

    if purpose == "news":
        return {
            "title": title,
            "source": source,
            "article_url": result.url,
            "snippet": summary,
            "published_at": None,
            "thumb_urls": list(result.image_urls),
            "scores": scores,
            "raw_payload_json": raw_payload,
        }
    if purpose == "publication":
        return {
            "title": title,
            "publisher": source,
            "publication_year": first_year(title, summary),
            "summary": summary,
            "author_role": "",
            "url": result.url,
            "category": "publications",
            "scores": scores,
            "raw_payload_json": raw_payload,
        }
    if purpose == "award":
        return {
            "award_name": title,
            "awarding_organization": source,
            "award_year": first_year(title, summary),
            "description": summary,
            "url": result.url,
            "scores": scores,
            "raw_payload_json": raw_payload,
        }
    if purpose == "social":
        platform = "LinkedIn" if "linkedin.com" in source else "YouTube" if "youtube.com" in source or "youtu.be" in source else source
        return {
            "platform": platform,
            "title": title,
            "published_at": None,
            "url": result.url,
            "scores": scores,
            "raw_payload_json": raw_payload,
        }

    parsed = extract_profile_fields(result.markdown or result.text or "")
    parsed["scores"] = scores
    parsed["raw_payload_json"] = raw_payload
    return parsed


def complete_explicit_url(
    purpose: str,
    payload: Mapping[str, object],
    *,
    crawler_factory: CrawlerFactory,
) -> dict[str, object]:
    started = time.monotonic()
    prepared = prepare_payload(payload, purpose)
    result, attempts, primary, fallback = crawl_explicit_url(prepared, crawler_factory=crawler_factory)
    item = explicit_url_item(purpose, result, attempts)
    crawl_task = subtask(
        "explicit_url_crawl",
        provider=result.crawler,
        operation="crawl",
        count=1,
        elapsed_seconds=time.monotonic() - started,
    )
    request = {
        "person": prepared.get("person") or {},
        "company": prepared.get("company") or {},
        "url": result.url,
        "crawler": primary,
    }
    if fallback:
        request["fallback_crawler"] = fallback
    return {
        "schema_version": "noteapp_complete/v1",
        "purpose": purpose,
        "source_mode": "explicit_url",
        **synchronous_response_fields([crawl_task]),
        "query": str(prepared.get("query") or ""),
        "request": request,
        "count": 1,
        "items": [item],
        "raw": {"explicit_url_crawl": {"result": profile_crawl_payload(result), "attempts": attempts}},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def complete_company_homepage(
    payload: Mapping[str, object],
    *,
    crawler_factory: CrawlerFactory = create_crawler,
) -> dict[str, object]:
    started = time.monotonic()
    _person, company = build_structured_entities(payload)
    homepage = str(payload.get("url") or company.get("홈페이지") or "").strip()
    if not homepage:
        raise ValueError("company homepage requires company.홈페이지 or url")
    if "://" not in homepage:
        homepage = f"https://{homepage}"
    homepage = explicit_public_url({"url": homepage})
    prepared = prepare_payload({**payload, "url": homepage}, "company-homepage")

    attempts: list[dict[str, object]] = []
    subtasks: list[dict[str, object]] = []
    selected: CrawlResult | None = None
    for requested_crawler, max_latency in (
        ("auto", COMPANY_HOMEPAGE_AUTO_MAX_LATENCY_SECONDS),
        ("firecrawl", COMPANY_HOMEPAGE_FIRECRAWL_TIMEOUT_SECONDS),
    ):
        crawl_started = time.monotonic()
        try:
            result = crawler_factory(
                requested_crawler,
                respect_robots=True,
                max_latency_seconds=max_latency,
            ).crawl(homepage)
        except Exception as exc:
            attempts.append(
                {
                    "requested_crawler": requested_crawler,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            task = subtask(
                f"company_homepage_{requested_crawler}_crawl",
                provider=requested_crawler,
                operation="crawl",
                count=0,
                elapsed_seconds=time.monotonic() - crawl_started,
            )
            task["status"] = "failed"
            subtasks.append(task)
            continue

        challenge = challenge_page_reason(result)
        bot_detection = mapping_or_empty(result.raw_payload.get("bot_detection"))
        if bot_detection.get("detected"):
            challenge = f"bot challenge detected: {bot_detection.get('vendor') or 'unknown'}"
        text = compact_text(result.markdown or result.text)
        failed = crawl_failure(result) or len(text) < COMPANY_HOMEPAGE_MIN_TEXT_CHARS
        status = "failed" if failed or challenge else "completed"
        attempts.append(
            {
                "requested_crawler": requested_crawler,
                "actual_crawler": result.crawler,
                "status": status,
                "status_code": result.status_code,
                "text_length": len(text),
                "error": challenge or result.error or ("insufficient homepage text" if failed else None),
            }
        )
        task = subtask(
            f"company_homepage_{requested_crawler}_crawl",
            provider=requested_crawler,
            operation="crawl",
            count=0 if status == "failed" else 1,
            elapsed_seconds=time.monotonic() - crawl_started,
        )
        task["status"] = status
        task["actual_crawler"] = result.crawler
        subtasks.append(task)
        if challenge:
            raise SearchAgentError(f"{challenge}; refusing managed crawler fallback")
        if not failed:
            selected = result
            break

    if selected is None:
        detail = "; ".join(
            f"{attempt.get('requested_crawler')}: {attempt.get('error') or 'empty result'}" for attempt in attempts
        )
        raise SearchAgentError(f"company homepage crawl failed: {detail}")

    text = compact_text(selected.markdown or selected.text)
    scores = crawl_score_summary(selected)
    crawl_payload = profile_crawl_payload(selected)
    company_name = compact_text(mapping_or_empty(prepared.get("company")).get("회사명"))
    item = {
        "company_name": company_name,
        "homepage_url": selected.url,
        "title": selected.title or company_name or selected.canonical_url,
        "text_preview": text[:8000],
        "crawler": selected.crawler,
        "status_code": selected.status_code,
        "image_urls": list(selected.image_urls[:20]),
        "scores": scores,
        "raw_payload_json": {
            "homepage_crawl": crawl_payload,
            "attempts": attempts,
            "scores": scores,
        },
    }
    return {
        "schema_version": "noteapp_complete/v1",
        "purpose": "company-homepage",
        **synchronous_response_fields(subtasks),
        "query": str(prepared.get("query") or company_name),
        "request": {
            "company": prepared.get("company") or {},
            "url": homepage,
            "crawler": "auto",
            "fallback_crawler": "firecrawl",
        },
        "count": 1,
        "items": [item],
        "raw": {"company_homepage_crawl": {"result": crawl_payload, "attempts": attempts}},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def complete_news(
    payload: Mapping[str, object],
    *,
    search_client_factory: SearchClientFactory = create_search_client,
    crawler_factory: CrawlerFactory = create_crawler,
) -> dict[str, object]:
    started = time.monotonic()
    deadline = started + NEWS_RESPONSE_BUDGET_SECONDS
    prepared = prepare_payload(payload, "news")
    query = str(prepared.get("query") or "")
    limit = clamp_limit(prepared.get("limit"))
    queries = news_search_queries(prepared)
    company_names = as_text_values(mapping_or_empty(prepared.get("company")).get("회사명"))
    search_keyword = next((name for value in company_names if (name := news_company_name(value))), query)
    identity_terms = news_identity_terms(prepared)
    identity_title_only = bool(as_text_values(mapping_or_empty(prepared.get("company")).get("회사명")))
    crawl_business_news = clean_bool(prepared.get("crawl_business_news"), default=False)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=NEWS_RECENCY_DAYS)
    timed_out_providers: list[str] = []
    error_providers: list[str] = []
    search_queries: list[dict[str, str]] = []
    quality_stats = empty_news_quality_stats()
    quality_stats["analysis_version"] = NEWS_RESULT_ANALYSIS_VERSION
    noise_seen: set[str] = set()

    def finalize_with_quality(records: list[dict[str, object]]) -> list[dict[str, object]]:
        unique = finalize_news_records(records, now=now, limit=max(1, len(records)))
        quality_stats["title_duplicates_removed"] = int(quality_stats.get("title_duplicates_removed") or 0) + max(
            0, len(records) - len(unique)
        )
        finalized = unique[:limit]
        quality_stats["returned_count"] = len(finalized)
        quality_stats["valuable_returned"] = 0
        quality_stats["contextual_returned"] = 0
        quality_stats["weak_returned"] = 0
        quality_stats["unavailable_returned"] = 0
        quality_stats["value_intents"] = {}
        for record in finalized:
            record_value_stat(quality_stats, assess_news_quality(record))
        return finalized

    def search_news() -> list[dict[str, object]]:
        naver_query = queries[0].removesuffix(" 뉴스")
        jobs = [("serper", search_query) for search_query in queries] + [("naver_news", naver_query)]
        provider_limit = max(10, limit)

        def search_provider(job: tuple[str, str]) -> list[dict[str, object]]:
            provider, search_query = job
            client = cap_client_timeout(search_client_factory(provider), NEWS_SEARCH_PROVIDER_TIMEOUT_SECONDS)
            search_queries.append({"provider": provider, "query": search_query})
            return [
                record.to_dict()
                for record in client.search(
                    search_query,
                    limit=provider_limit,
                    query_intent="noteapp_complete:news",
                )
            ]

        executor = ThreadPoolExecutor(max_workers=len(jobs))
        try:
            futures = {job: executor.submit(search_provider, job) for job in jobs}
            done, _pending = wait(
                futures.values(),
                timeout=bounded_remaining_seconds(deadline, NEWS_SEARCH_BUDGET_SECONDS),
            )
            results: dict[tuple[str, str], list[dict[str, object]]] = {}
            errors: list[Exception] = []
            for job in jobs:
                provider, _search_query = job
                future = futures[job]
                if future not in done:
                    if provider not in timed_out_providers:
                        timed_out_providers.append(provider)
                    results[job] = []
                    continue
                try:
                    results[job] = future.result()
                except Exception as exc:
                    results[job] = []
                    errors.append(exc)
                    if provider not in error_providers:
                        error_providers.append(provider)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if len(errors) == len(jobs) and not timed_out_providers:
            raise errors[0]

        records: list[dict[str, object]] = []
        seen: set[str] = set()
        for job in jobs:
            for record in results[job]:
                quality_stats["candidates_seen"] = int(quality_stats.get("candidates_seen") or 0) + 1
                key = canonicalize_url(str(record.get("url") or ""))
                if key and key in seen:
                    quality_stats["url_duplicates_removed"] = int(quality_stats.get("url_duplicates_removed") or 0) + 1
                    continue
                if not _news_record_matches_base_policy(
                    record,
                    identity_terms=identity_terms,
                    cutoff=cutoff,
                    now=now,
                    identity_title_only=identity_title_only,
                ):
                    continue
                quality = assess_news_quality(record)
                if quality.get("is_noise"):
                    if key and key in noise_seen:
                        quality_stats["url_duplicates_removed"] = int(quality_stats.get("url_duplicates_removed") or 0) + 1
                        continue
                    if key:
                        noise_seen.add(key)
                    record_noise_stat(quality_stats, quality)
                    continue
                if key:
                    noise_seen.discard(key)
                    seen.add(key)
                records.append(record)
        return finalize_with_quality(records)

    records, task = timed_subtask(
        "naver_news_search",
        provider="naver_news",
        operation="search",
        callback=search_news,
    )
    if timed_out_providers:
        task["timed_out_providers"] = timed_out_providers
    if error_providers:
        task["error_providers"] = error_providers
    task["provider_calls"] = {"naver_news": 1, "serper": len(queries)}
    subtasks = [task]
    items = news_items(records)
    raw: dict[str, object] = {"news_search": {"provider": "naver_news", "records": records}}
    request = {"person": prepared.get("person") or {}, "company": prepared.get("company") or {}}

    attempted_urls: set[str] = set()
    if crawl_business_news:
        request["crawl_business_news"] = True
        crawl_started = time.monotonic()
        crawl_summary = crawl_business_news_items(items, crawler_factory=crawler_factory, deadline=deadline)
        crawl_task = subtask(
            "business_news_auto_crawl",
            provider="auto",
            operation="crawl",
            count=int(crawl_summary.get("success_count") or 0),
            elapsed_seconds=time.monotonic() - crawl_started,
        )
        crawl_task["attempted_count"] = int(crawl_summary.get("attempted_count") or 0)
        subtasks.append(crawl_task)
        attempted_urls = {
            str(attempt.get("url") or "")
            for attempt in crawl_summary.get("attempts") or []
            if isinstance(attempt, Mapping)
        }

        fallback_task: dict[str, object] | None = None
        if not crawl_summary.get("success_count"):
            fallback_started = time.monotonic()
            remaining = bounded_remaining_seconds(deadline, NEWS_FIRECRAWL_FALLBACK_TIMEOUT_SECONDS)
            if remaining <= 0:
                fallback_task = subtask(
                    "firecrawl_business_news_search",
                    provider="firecrawl_search",
                    operation="search",
                    count=0,
                    elapsed_seconds=0,
                )
                fallback_task["status"] = "skipped"
                fallback_task["reason"] = "response budget exhausted"
            else:
                try:
                    client = cap_client_timeout(search_client_factory("firecrawl_search"), remaining)
                    search_queries.append({"provider": "firecrawl_search", "query": queries[-1]})
                    fallback_records = [
                        record.to_dict()
                        for record in client.search(
                            queries[-1],
                            limit=10,
                            query_intent="noteapp_complete:news_business_fallback",
                        )
                    ]
                    seen = {canonicalize_url(str(record.get("url") or "")) for record in records}
                    added = 0
                    for record in fallback_records:
                        quality_stats["candidates_seen"] = int(quality_stats.get("candidates_seen") or 0) + 1
                        key = canonicalize_url(str(record.get("url") or ""))
                        if key and key in seen:
                            quality_stats["url_duplicates_removed"] = int(quality_stats.get("url_duplicates_removed") or 0) + 1
                            continue
                        if not _news_record_matches_base_policy(
                            record,
                            identity_terms=identity_terms,
                            cutoff=cutoff,
                            now=now,
                            business_only=True,
                            identity_title_only=identity_title_only,
                        ):
                            continue
                        quality = assess_news_quality(record)
                        if quality.get("is_noise"):
                            if key and key in noise_seen:
                                quality_stats["url_duplicates_removed"] = int(quality_stats.get("url_duplicates_removed") or 0) + 1
                                continue
                            if key:
                                noise_seen.add(key)
                            record_noise_stat(quality_stats, quality)
                            continue
                        if key:
                            noise_seen.discard(key)
                            seen.add(key)
                        records.append(record)
                        added += 1
                    records = finalize_with_quality(records)
                    items = news_items(records)
                    fallback_task = subtask(
                        "firecrawl_business_news_search",
                        provider="firecrawl_search",
                        operation="search",
                        count=added,
                        elapsed_seconds=time.monotonic() - fallback_started,
                    )
                except Exception as exc:
                    fallback_task = subtask(
                        "firecrawl_business_news_search",
                        provider="firecrawl_search",
                        operation="search",
                        count=0,
                        elapsed_seconds=time.monotonic() - fallback_started,
                    )
                    fallback_task["status"] = "failed"
                    fallback_task["error"] = f"{type(exc).__name__}: {exc}"
            subtasks.append(fallback_task)
            crawl_summary["firecrawl_fallback"] = fallback_task

        for item in items:
            if source := business_news_source_for_item(item):
                item["business_news_source"] = source
        raw["business_news"] = crawl_summary

    thumbnail_items = [item for item in items if news_item_crawl_url(item) not in attempted_urls]
    value_llm_executor: ThreadPoolExecutor | None = None
    value_llm_future = None
    if not news_value_llm_disabled() and records:
        value_llm_executor = ThreadPoolExecutor(max_workers=1)
        value_llm_future = value_llm_executor.submit(classify_value_intents_llm, records)
    enrich_news_thumbnails(thumbnail_items, crawler_factory=crawler_factory, deadline=deadline)
    if value_llm_executor is not None:
        remaining = bounded_remaining_seconds(deadline, NEWS_VALUE_LLM_COLLECT_SECONDS)
        classifications: dict[int, dict[str, object]] | None = None
        value_llm_status = "budget_exhausted"
        if remaining > 0:
            try:
                classifications = value_llm_future.result(timeout=remaining)
                value_llm_status = "ok" if classifications is not None else "failed"
            except Exception:
                value_llm_status = "timed_out"
        value_llm_executor.shutdown(wait=False, cancel_futures=True)
        if classifications:
            for index, classification in classifications.items():
                if 0 <= index < len(items) and isinstance(items[index].get("news_quality"), dict):
                    apply_llm_value_intents(items[index]["news_quality"], classification)
            for key in ("valuable_returned", "contextual_returned", "weak_returned", "unavailable_returned"):
                quality_stats[key] = 0
            quality_stats["value_intents"] = {}
            for item in items:
                if isinstance(item.get("news_quality"), dict):
                    record_value_stat(quality_stats, item["news_quality"])
        quality_stats["value_source"] = "llm" if classifications else "unavailable"
        quality_stats["value_llm"] = {"status": value_llm_status}
    elif news_value_llm_disabled():
        quality_stats["value_source"] = "disabled"
    raw["news_search"] = {"provider": "naver_news", "records": records}
    for item in items:
        item["search_keyword"] = search_keyword
    return {
        "schema_version": "noteapp_complete/v1",
        "purpose": "news",
        **synchronous_response_fields(subtasks),
        "query": query,
        "search_keyword": search_keyword,
        "search_metadata": {
            "searched_at": now.isoformat(),
            "queries": sorted(search_queries, key=lambda entry: (entry["provider"], entry["query"])),
            "quality": quality_stats,
        },
        "request": request,
        "count": len(records),
        "items": items,
        "raw": raw,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def complete_publication(
    payload: Mapping[str, object],
    *,
    evidence_finder: PersonEvidenceFinder = find_person_evidence,
) -> dict[str, object]:
    started = time.monotonic()
    prepared = prepare_payload(payload, "publication")
    categories = ["books", "publications"]
    result, task = timed_subtask(
        "person_evidence_publication",
        provider="workflow_person_evidence_finder",
        operation="find",
        categories=categories,
        callback=lambda: evidence_finder({**prepared, "categories": categories}),
    )
    items = publication_items(result)
    return {
        "schema_version": "noteapp_complete/v1",
        "purpose": "publication",
        **synchronous_response_fields([task]),
        "query": str(prepared.get("query") or ""),
        "request": {"person": prepared.get("person") or {}, "company": prepared.get("company") or {}},
        "count": len(items),
        "items": items,
        "raw": {"person_evidence": result},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def complete_award(
    payload: Mapping[str, object],
    *,
    evidence_finder: PersonEvidenceFinder = find_person_evidence,
) -> dict[str, object]:
    started = time.monotonic()
    prepared = prepare_payload(payload, "award")
    categories = ["awards", "institutions"]
    result, task = timed_subtask(
        "person_evidence_award",
        provider="workflow_person_evidence_finder",
        operation="find",
        categories=categories,
        callback=lambda: evidence_finder({**prepared, "categories": categories}),
    )
    items = award_items(result)
    return {
        "schema_version": "noteapp_complete/v1",
        "purpose": "award",
        **synchronous_response_fields([task]),
        "query": str(prepared.get("query") or ""),
        "request": {"person": prepared.get("person") or {}, "company": prepared.get("company") or {}},
        "count": len(items),
        "items": items,
        "raw": {"person_evidence": result},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def complete_social(
    payload: Mapping[str, object],
    *,
    linkedin_finder: LinkedInFinder = find_linkedin_profiles,
    search_client_factory: SearchClientFactory = create_search_client,
) -> dict[str, object]:
    started = time.monotonic()
    prepared = prepare_payload(payload, "social")
    if "providers" not in prepared and "provider" not in prepared:
        prepared["providers"] = ["serper"]
    if mapping_or_empty(prepared.get("company")).get("회사명"):
        prepared.setdefault("strict_company_filter", True)
    query = str(prepared.get("query") or "")
    limit = clamp_limit(prepared.get("limit"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        linkedin_future = executor.submit(
            timed_subtask,
            "linkedin_profile_finder",
            provider="workflow_linkedin_profile_finder",
            operation="find",
            callback=lambda: linkedin_finder(prepared),
        )
        youtube_future = executor.submit(
            timed_subtask,
            "youtube_search",
            provider="youtube",
            operation="search",
            callback=lambda: [
                record.to_dict()
                for record in search_client_factory("youtube").search(
                    query,
                    limit=limit,
                    query_intent="noteapp_complete:social",
                )
            ],
        )
        linkedin_result, linkedin_task = linkedin_future.result()
        youtube_records, youtube_task = youtube_future.result()
    items = social_items(linkedin_result, youtube_records)
    return {
        "schema_version": "noteapp_complete/v1",
        "purpose": "social",
        **synchronous_response_fields([linkedin_task, youtube_task]),
        "query": query,
        "request": {"person": prepared.get("person") or {}, "company": prepared.get("company") or {}},
        "count": len(items),
        "items": items,
        "raw": {
            "linkedin": linkedin_result,
            "youtube_search": {"provider": "youtube", "records": youtube_records},
        },
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def naver_profile_search_url(query: str) -> str:
    if not compact_text(query):
        raise ValueError("query or identifying person/company fields are required")
    return (
        "https://search.naver.com/search.naver"
        f"?where=nexearch&sm=top_hty&fbm=0&ie=utf8&query={quote_plus(query)}"
    )


def complete_profile(
    payload: Mapping[str, object],
    *,
    search_client_factory: SearchClientFactory = create_search_client,
    crawler_factory: CrawlerFactory = create_crawler,
) -> dict[str, object]:
    started = time.monotonic()
    prepared = prepare_payload(payload, "profile")
    query = str(prepared.get("query") or "")
    crawl_url = naver_profile_search_url(query)
    crawler_name = str(prepared.get("crawler") or "firecrawl").strip() or "firecrawl"
    if crawler_name != "firecrawl":
        raise SearchAgentError("noteapp complete profile currently supports firecrawl only")
    result, task = timed_subtask(
        "naver_profile_crawl",
        provider="firecrawl",
        operation="crawl",
        callback=lambda: crawler_factory("firecrawl").crawl(crawl_url),
    )
    parsed = extract_profile_fields(result.markdown or result.text or "")
    if not parsed["education"] and not parsed["career_history"]:
        company = mapping_or_empty(prepared.get("company"))
        person = mapping_or_empty(prepared.get("person"))
        company_host = host_label(company.get("홈페이지"))
        person_name = compact_text(person.get("이름") or person.get("영문명"))
        fallback_query = (
            f'site:{company_host} "{person_name or query}" 프로필 약력 학력 경력'
            if company_host
            else f"{query} 프로필 약력 학력 경력"
        )
        try:
            candidates = search_client_factory("serper").search(
                fallback_query,
                limit=3,
                query_intent="noteapp_complete:profile_fallback",
            )
            candidate = next(
                (
                    record
                    for record in candidates
                    if urlparse(record.url).scheme in {"http", "https"}
                    and host_label(record.url)
                    not in {"google.com", "search.naver.com", "linkedin.com", "youtube.com"}
                ),
                None,
            )
            if candidate is not None:
                fallback_result = crawler_factory("auto").crawl(candidate.url)
                fallback_fields = extract_profile_fields(fallback_result.markdown or fallback_result.text or "")
                if fallback_fields["education"] or fallback_fields["career_history"]:
                    parsed = fallback_fields
                    result = fallback_result
        except Exception:
            pass
    scores = crawl_score_summary(result)
    parsed["scores"] = scores
    parsed["raw_payload_json"] = {
        "profile_crawl": profile_crawl_payload(result),
        "scores": scores,
    }
    return {
        "schema_version": "noteapp_complete/v1",
        "purpose": "profile",
        **synchronous_response_fields([task]),
        "query": query,
        "request": {"person": prepared.get("person") or {}, "company": prepared.get("company") or {}},
        "count": 1,
        "items": [parsed],
        "raw": {"profile_crawl": profile_crawl_payload(result)},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def complete_for_purpose(
    purpose: str,
    payload: Mapping[str, object],
    *,
    search_client_factory: SearchClientFactory = create_search_client,
    crawler_factory: CrawlerFactory = create_crawler,
    linkedin_finder: LinkedInFinder = find_linkedin_profiles,
    evidence_finder: PersonEvidenceFinder = find_person_evidence,
) -> dict[str, object]:
    normalized_purpose = normalize_complete_purpose(purpose)
    if normalized_purpose not in NOTEAPP_COMPLETE_PURPOSES:
        raise ValueError("unsupported noteapp complete purpose")
    if normalized_purpose == "company-homepage":
        result = complete_company_homepage(payload, crawler_factory=crawler_factory)
    elif payload.get("url"):
        result = complete_explicit_url(
            normalized_purpose,
            payload,
            crawler_factory=crawler_factory,
        )
    elif normalized_purpose == "news":
        result = complete_news(
            payload,
            search_client_factory=search_client_factory,
            crawler_factory=crawler_factory,
        )
    elif normalized_purpose == "publication":
        result = complete_publication(payload, evidence_finder=evidence_finder)
    elif normalized_purpose == "award":
        result = complete_award(payload, evidence_finder=evidence_finder)
    elif normalized_purpose == "social":
        result = complete_social(
            payload,
            linkedin_finder=linkedin_finder,
            search_client_factory=search_client_factory,
        )
    elif normalized_purpose == "profile":
        result = complete_profile(
            payload,
            search_client_factory=search_client_factory,
            crawler_factory=crawler_factory,
        )
    else:
        raise ValueError("unsupported noteapp complete purpose")
    finalize_complete_items(result)
    result["endpoint"] = endpoint_for_purpose(normalized_purpose)
    return result
