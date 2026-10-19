from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse

from .entity_match import score_entity_match_batch
from .llm_confidence import clean_bool
from .schema import EvidenceRecord
from .search import create_search_client
from .utils import canonicalize_url, ordered_parallel_map


SearchClientFactory = Callable[[str], Any]

LINKEDIN_PROFILE_HOSTS = {"linkedin.com", "www.linkedin.com", "kr.linkedin.com"}
SUPPORTED_SERP_PROVIDERS = ("serper",)
DEFAULT_SERP_PROVIDERS: tuple[str, ...] = ()
DEFAULT_MAX_SERP_QUERIES = 4
DEFAULT_MAX_APIFY_QUERIES = 2
DEFAULT_SERP_LIMIT = 5
DEFAULT_APIFY_LIMIT = 5

SCHOOL_ALIASES = {
    "hansung_university": ("Hansung University", "한성대학교", "한성대"),
}

COMPANY_ALIASES = {
    "ispark": ("이즈파크", "ISPark", "ispark", "iSPARK"),
}

PERSON_FIELD_NAMES = (
    "name",
    "person_name",
    "korean_name",
    "이름",
    "성명",
)
ENGLISH_NAME_FIELD_NAMES = (
    "english_name",
    "englishName",
    "name_en",
    "eng_name",
    "영문명",
    "영문 이름",
)
SCHOOL_FIELD_NAMES = (
    "school",
    "schools",
    "university",
    "college",
    "education",
    "학교",
    "학력",
    "대학",
    "대학교",
)
COMPANY_FIELD_NAMES = (
    "company",
    "company_name",
    "current_company",
    "회사",
    "회사명",
    "소속",
)

KOREAN_NAME_RE = re.compile(r"[가-힣]{2,5}")
SURNAME_ROMANIZATION = {
    "김": ("Kim", "Gim"),
    "이": ("Lee", "Yi"),
    "박": ("Park", "Bak"),
    "최": ("Choi", "Choe"),
    "정": ("Jung", "Jeong"),
    "강": ("Kang", "Gang"),
    "조": ("Cho", "Jo"),
    "윤": ("Yoon", "Yun"),
    "장": ("Jang", "Chang"),
    "임": ("Lim", "Im"),
    "한": ("Han",),
    "오": ("Oh", "O"),
    "서": ("Seo", "Suh"),
    "신": ("Shin",),
    "권": ("Kwon", "Gwon"),
    "황": ("Hwang",),
    "안": ("Ahn", "An"),
    "송": ("Song",),
    "전": ("Jun", "Jeon"),
    "홍": ("Hong",),
}

SYLLABLE_ROMANIZATION = {
    "갑": ("Gap", "Gab"),
    "산": ("San",),
    "정": ("Jeong", "Jung"),
    "애": ("Ae",),
    "훈": ("Hoon", "Hun"),
    "준": ("Joon", "Jun"),
    "현": ("Hyun", "Hyeon"),
    "수": ("Soo", "Su"),
    "민": ("Min",),
    "지": ("Ji",),
    "영": ("Young", "Yeong"),
    "진": ("Jin",),
    "석": ("Seok", "Suk"),
    "호": ("Ho",),
    "우": ("Woo", "U"),
    "연": ("Yeon", "Yun"),
    "재": ("Jae",),
    "성": ("Sung", "Seong"),
    "혁": ("Hyuk", "Hyeok"),
    "환": ("Hwan",),
    "후": ("Hu", "Hoo"),
    "원": ("Won",),
    "희": ("Hee", "Hui"),
}


@dataclass(slots=True)
class LinkedInSubject:
    query: str
    names: list[str] = field(default_factory=list)
    english_names: list[str] = field(default_factory=list)
    schools: list[str] = field(default_factory=list)
    school_aliases: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    company_aliases: list[str] = field(default_factory=list)
    company_linkedin_urls: list[str] = field(default_factory=list)

    def reference(self) -> dict[str, object]:
        return {
            "kind": "linkedin_profile_finder",
            "query": self.query,
            "target": " ".join(dedupe(self.names + self.english_names + self.school_aliases + self.company_aliases)),
            "person": {
                "name": self.names,
                "english_name": self.english_names,
                "school": self.school_aliases,
                "company": self.company_aliases,
            },
            "company": {
                "company_name": self.company_aliases,
                "school": self.school_aliases,
            },
        }


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def as_text_values(value: object) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, Mapping):
        values: list[str] = []
        for item in value.values():
            values.extend(as_text_values(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(as_text_values(item))
        return values
    text = str(value).strip()
    return [text] if text else []


def mapping_values(mapping: Mapping[str, object], keys: tuple[str, ...]) -> list[str]:
    normalized_keys = {normalize_key(key) for key in keys}
    values: list[str] = []
    for key, value in mapping.items():
        if normalize_key(key) in normalized_keys:
            values.extend(as_text_values(value))
    return values


def normalize_key(value: object) -> str:
    return re.sub(r"[\s_\-./]+", "", str(value or "").strip().lower())


def compact_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def expand_school_aliases(values: list[str]) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    aliases: list[str] = []
    haystack = " ".join(values)
    known_aliases = {alias.casefold() for alias_group in SCHOOL_ALIASES.values() for alias in alias_group}
    for alias_group in SCHOOL_ALIASES.values():
        canonical = alias_group[0]
        if any(alias.lower() in haystack.lower() for alias in alias_group):
            normalized.append(canonical)
            aliases.extend(alias_group)
    for value in values:
        text = compact_text(value)
        if not text:
            continue
        if text not in aliases:
            aliases.append(text)
        if text.casefold() in known_aliases:
            continue
        if not any(text.lower() in item.lower() or item.lower() in text.lower() for item in normalized):
            normalized.append(text)
    return dedupe(normalized), dedupe(aliases)


def expand_company_aliases(values: list[str]) -> tuple[list[str], list[str]]:
    canonical_values: list[str] = []
    aliases: list[str] = []
    haystack = " ".join(values)
    for alias_group in COMPANY_ALIASES.values():
        canonical = alias_group[0]
        if any(alias.lower() in haystack.lower() for alias in alias_group):
            canonical_values.append(canonical)
            aliases.extend(alias_group)
    for value in values:
        text = compact_text(value)
        if text:
            canonical_values.append(text)
            aliases.append(text)
    return dedupe(canonical_values), dedupe(aliases)


def is_linkedin_profile_url(value: str) -> bool:
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if host not in LINKEDIN_PROFILE_HOSTS:
        return False
    path = parsed.path.strip("/")
    if not path.startswith("in/"):
        return False
    return len(path.split("/")) >= 2


def is_linkedin_company_url(value: str) -> bool:
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    return host in LINKEDIN_PROFILE_HOSTS and path.startswith("company/")


def korean_names_from_text(value: str) -> list[str]:
    names: list[str] = []
    for match in KOREAN_NAME_RE.finditer(value):
        candidate = match.group(0)
        if any(alias in candidate for aliases in SCHOOL_ALIASES.values() for alias in aliases):
            continue
        if candidate in {"대학교", "한성대", "이즈파크"}:
            continue
        if len(candidate) <= 5:
            names.append(candidate)
    return dedupe(names)


def romanize_korean_name(name: str) -> list[str]:
    if not re.fullmatch(r"[가-힣]{3,4}", name):
        return []
    surname = name[:1]
    given = name[1:]
    surnames = SURNAME_ROMANIZATION.get(surname)
    if not surnames:
        return []
    parts = [SYLLABLE_ROMANIZATION.get(char, (char,))[:2] for char in given]
    if any(any(re.fullmatch(r"[가-힣]", item) for item in group) for group in parts):
        return []

    given_joined: list[str] = [""]
    for group in parts:
        given_joined = [
            left + (right if not left else right[:1].lower() + right[1:])
            for left in given_joined
            for right in group
        ]
    spaced = [" ".join(items) for items in zip(*parts)] if len(parts) == 2 else []

    names: list[str] = []
    for last_name in surnames[:2]:
        for given_name in given_joined[:6]:
            names.append(f"{given_name} {last_name}")
        for given_name in spaced[:4]:
            names.append(f"{given_name} {last_name}")
    return dedupe(names)[:10]


def mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def merged_reference_from_payload(subject: LinkedInSubject, payload: Mapping[str, object]) -> dict[str, object]:
    reference = subject.reference()
    reference_payload = mapping_or_empty(payload.get("reference"))
    person = dict(reference.get("person") if isinstance(reference.get("person"), Mapping) else {})
    company = dict(reference.get("company") if isinstance(reference.get("company"), Mapping) else {})
    for source in (
        mapping_or_empty(reference_payload.get("person") or reference_payload.get("Person")),
        mapping_or_empty(payload.get("person") or payload.get("Person") or payload.get("person_data")),
    ):
        person.update(source)
    for source in (
        mapping_or_empty(reference_payload.get("company") or reference_payload.get("Company")),
        mapping_or_empty(payload.get("company") or payload.get("Company") or payload.get("company_data")),
    ):
        company.update(source)
    reference["person"] = person
    reference["company"] = company
    return reference


def subject_from_payload(payload: Mapping[str, object]) -> LinkedInSubject:
    query = compact_text(payload.get("query") or payload.get("target") or payload.get("subject") or "")
    reference = payload.get("reference") if isinstance(payload.get("reference"), Mapping) else {}
    person = payload.get("person") or payload.get("Person") or payload.get("person_data")
    company = payload.get("company") or payload.get("Company") or payload.get("company_data")
    person_map = person if isinstance(person, Mapping) else {}
    company_map = company if isinstance(company, Mapping) else {}
    reference_map = reference if isinstance(reference, Mapping) else {}
    ref_person = reference_map.get("person") if isinstance(reference_map.get("person"), Mapping) else {}
    ref_company = reference_map.get("company") if isinstance(reference_map.get("company"), Mapping) else {}

    name_values = mapping_values(person_map, PERSON_FIELD_NAMES)
    name_values.extend(mapping_values(ref_person, PERSON_FIELD_NAMES))
    if not name_values:
        name_values.extend(korean_names_from_text(query))
    names = dedupe(name_values)

    english_values = mapping_values(person_map, ENGLISH_NAME_FIELD_NAMES)
    english_values.extend(mapping_values(ref_person, ENGLISH_NAME_FIELD_NAMES))
    for name in names:
        english_values.extend(romanize_korean_name(name))
    english_names = dedupe(english_values)

    school_values = []
    for source in (payload, person_map, company_map, ref_person, ref_company):
        school_values.extend(mapping_values(source, SCHOOL_FIELD_NAMES))
    school_values.extend(alias for aliases in SCHOOL_ALIASES.values() for alias in aliases if alias.lower() in query.lower())
    schools, school_aliases = expand_school_aliases(school_values)

    direct_payload_fields = {
        key: value
        for key, value in payload.items()
        if normalize_key(key) not in {"person", "persondata", "company", "companydata", "reference"}
    }
    company_values = []
    for source in (direct_payload_fields, person_map, company_map, ref_person, ref_company):
        company_values.extend(mapping_values(source, COMPANY_FIELD_NAMES))
    company_values.extend(alias for aliases in COMPANY_ALIASES.values() for alias in aliases if alias.lower() in query.lower())
    companies, company_aliases = expand_company_aliases(company_values)

    return LinkedInSubject(
        query=query,
        names=names,
        english_names=english_names,
        schools=schools,
        school_aliases=school_aliases,
        companies=companies,
        company_aliases=company_aliases,
    )


def build_serp_queries(subject: LinkedInSubject, *, max_queries: int = DEFAULT_MAX_SERP_QUERIES) -> list[str]:
    names = dedupe(subject.names + subject.english_names) or korean_names_from_text(subject.query)
    contexts: list[str] = []
    for index in range(max(len(subject.school_aliases), len(subject.company_aliases))):
        if index < len(subject.school_aliases):
            contexts.append(subject.school_aliases[index])
        if index < len(subject.company_aliases):
            contexts.append(subject.company_aliases[index])
    contexts = dedupe(contexts)
    queries: list[str] = []
    for name in names[:6]:
        if contexts:
            for context in contexts[:5]:
                queries.append(f'"{name}" "{context}" LinkedIn')
                queries.append(f'site:linkedin.com/in "{name}" "{context}"')
        else:
            queries.append(f'"{name}" LinkedIn')
            queries.append(f'site:linkedin.com/in "{name}"')
    if subject.query:
        queries.append(f"{subject.query} LinkedIn")
        queries.append(f'site:linkedin.com/in "{subject.query}"')
    return dedupe(queries)[:max_queries]


def provider_list_from_payload(payload: Mapping[str, object]) -> list[str]:
    raw = payload.get("providers") or payload.get("serp_providers")
    if raw:
        values = [str(value).strip().lower() for value in as_text_values(raw)]
        return [value for value in values if value in SUPPORTED_SERP_PROVIDERS]
    provider = str(payload.get("provider") or "").strip().lower()
    if provider in SUPPORTED_SERP_PROVIDERS:
        return [provider]
    return list(DEFAULT_SERP_PROVIDERS)


def extract_company_urls(records: list[EvidenceRecord], subject: LinkedInSubject) -> list[str]:
    aliases = [normalized_person_name(alias) for alias in subject.company_aliases]
    urls: list[str] = []
    for record in records:
        label = normalized_person_name(f"{record.title} {unquote(urlparse(record.url).path)}")
        if is_linkedin_company_url(record.url) and any(alias and alias in label for alias in aliases):
            urls.append(record.url)
    return dedupe(urls)[:1]


def normalized_person_name(value: object) -> str:
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def record_matches_subject_name(record: EvidenceRecord, subject: LinkedInSubject) -> bool:
    haystack = normalized_person_name(f"{record.title} {record.snippet}")
    return any(
        normalized and normalized in haystack
        for normalized in map(normalized_person_name, subject.names + subject.english_names)
    )


def record_to_candidate_item(record: EvidenceRecord) -> dict[str, object]:
    payload = record.to_dict()
    payload["item_id"] = payload.get("result_id") or payload.get("url")
    return payload


def merge_candidate(existing: dict[str, object], record: EvidenceRecord, *, signals: list[str]) -> None:
    records = existing.setdefault("records", [])
    if isinstance(records, list):
        records.append(record.to_dict())
    signal_values = existing.setdefault("workflow_signals", [])
    if isinstance(signal_values, list):
        signal_values.extend(signal for signal in signals if signal not in signal_values)
    snippets = existing.setdefault("evidence_snippets", [])
    if isinstance(snippets, list) and record.snippet:
        snippets.append(record.snippet)
    if not existing.get("title") and record.title:
        existing["title"] = record.title
    if not existing.get("url") and record.url:
        existing["url"] = record.url


def candidate_item(candidate: Mapping[str, object]) -> dict[str, object]:
    snippets = candidate.get("evidence_snippets") if isinstance(candidate.get("evidence_snippets"), list) else []
    signals = candidate.get("workflow_signals") if isinstance(candidate.get("workflow_signals"), list) else []
    records = candidate.get("records") if isinstance(candidate.get("records"), list) else []
    raw_values: list[str] = []
    for record in records:
        if isinstance(record, Mapping):
            raw_payload = record.get("raw_payload_json")
            if isinstance(raw_payload, Mapping):
                for value in raw_payload.values():
                    if isinstance(value, (str, int, float)):
                        raw_values.append(str(value))
                    elif isinstance(value, (list, tuple, set, Mapping)):
                        raw_values.extend(as_text_values(value))
    return {
        "item_id": candidate.get("canonical_url") or candidate.get("url"),
        "title": candidate.get("title"),
        "url": candidate.get("url"),
        "snippet": " ".join(str(item) for item in snippets + signals + raw_values),
        "raw_payload_json": {"workflow_signals": signals},
    }


def workflow_score(identity_score: float, candidate: Mapping[str, object]) -> tuple[float, list[str]]:
    signals = list(candidate.get("workflow_signals") if isinstance(candidate.get("workflow_signals"), list) else [])
    score = identity_score
    reasons: list[str] = []
    if "apify_school_filtered" in signals and "person_name_match" in signals:
        score = max(score, 0.86)
        reasons.append("apify school-filtered profile with matching name")
    if "direct_linkedin_profile_from_serp" in signals and "serp_school_context" in signals:
        score = max(score, 0.82)
        reasons.append("direct LinkedIn profile appeared in school/company SERP context")
    if "person_name_match" in signals and "apify_profile_result" in signals and not (
        "apify_school_filtered" in signals or "serp_school_context" in signals or "apify_full_profile" in signals
    ):
        score = min(max(score, 0.56), 0.72)
        reasons.append("name-only Apify candidate; capped")
    return round(min(score, 1.0), 4), reasons


def find_linkedin_profiles(
    payload: Mapping[str, object],
    *,
    client_factory: SearchClientFactory = create_search_client,
) -> dict[str, object]:
    started = time.monotonic()
    subject = subject_from_payload(payload)
    providers = provider_list_from_payload(payload)
    max_serp_queries = max(0, min(int(payload.get("max_serp_queries") or DEFAULT_MAX_SERP_QUERIES), 12))
    max_apify_queries = max(1, min(int(payload.get("max_apify_queries") or DEFAULT_MAX_APIFY_QUERIES), 12))
    serp_limit = max(1, min(int(payload.get("serp_limit") or DEFAULT_SERP_LIMIT), 10))
    apify_limit = max(1, min(int(payload.get("apify_limit") or payload.get("limit") or DEFAULT_APIFY_LIMIT), 10))

    operations: list[dict[str, object]] = []
    serp_records: list[EvidenceRecord] = []
    serp_queries = build_serp_queries(subject, max_queries=max_serp_queries)
    if bool(payload.get("strict_company_filter")):
        company_queries = [f'site:linkedin.com/company "{company}"' for company in subject.company_aliases[:2]]
        serp_queries = dedupe(company_queries + serp_queries)[:max_serp_queries]
    serp_jobs = [
        (query, provider)
        for query in serp_queries
        for provider in providers
    ]

    def run_serp(job: tuple[str, str]) -> tuple[list[EvidenceRecord], Exception | None]:
        query, provider = job
        try:
            return client_factory(provider).search(
                query,
                limit=serp_limit,
                query_intent="linkedin_profile_finder",
            ), None
        except Exception as exc:
            return [], exc

    for (query, provider), (records, error) in zip(serp_jobs, ordered_parallel_map(run_serp, serp_jobs)):
        if error is not None:
            operations.append(
                {
                    "stage": "serp",
                    "provider": provider,
                    "query": query,
                    "ok": False,
                    "error_type": type(error).__name__,
                    "error": str(error)[:300],
                }
            )
            continue
        serp_records.extend(records)
        operations.append({"stage": "serp", "provider": provider, "query": query, "ok": True, "count": len(records)})

    subject.company_linkedin_urls = extract_company_urls(serp_records, subject)

    candidates: dict[str, dict[str, object]] = {}
    for record in serp_records:
        if not is_linkedin_profile_url(record.url) or not record_matches_subject_name(record, subject):
            continue
        canonical = canonicalize_url(record.url)
        candidate = candidates.setdefault(
            canonical,
            {
                "canonical_url": canonical,
                "url": record.url,
                "title": record.title,
                "records": [],
                "evidence_snippets": [],
                "workflow_signals": ["direct_linkedin_profile_from_serp", "person_name_match"],
            },
        )
        signals = ["direct_linkedin_profile_from_serp", "person_name_match"]
        if any(alias.lower() in f"{record.title} {record.snippet}".lower() for alias in subject.school_aliases):
            signals.append("serp_school_context")
        merge_candidate(candidate, record, signals=signals)

    apify_client = client_factory("apify_linkedin_profile_search")
    apify_queries = dedupe(subject.names + subject.english_names)
    apify_filters: dict[str, object] = {}
    if subject.schools:
        apify_filters["schools"] = subject.schools
    if subject.company_linkedin_urls and bool(payload.get("strict_company_filter")):
        apify_filters["currentCompanies"] = subject.company_linkedin_urls

    def run_apify(query: str) -> tuple[list[EvidenceRecord], Exception | None]:
        try:
            search_with_filters = getattr(apify_client, "search_with_filters", None)
            if callable(search_with_filters):
                records = search_with_filters(
                    query,
                    filters=apify_filters,
                    limit=apify_limit,
                    query_intent="linkedin_profile_finder",
                )
            else:
                records = apify_client.search(query, limit=apify_limit, query_intent="linkedin_profile_finder")
        except Exception as exc:
            return [], exc
        return records, None

    selected_apify_queries = apify_queries[:max_apify_queries]
    for query, (records, error) in zip(
        selected_apify_queries,
        ordered_parallel_map(run_apify, selected_apify_queries),
    ):
        if error is not None:
            operations.append(
                {
                    "stage": "apify",
                    "provider": "apify_linkedin_profile_search",
                    "query": query,
                    "ok": False,
                    "error_type": type(error).__name__,
                    "error": str(error)[:300],
                }
            )
            continue
        operations.append(
            {
                "stage": "apify",
                "provider": "apify_linkedin_profile_search",
                "query": query,
                "ok": True,
                "count": len(records),
                "filters": apify_filters,
            }
        )
        for record in records:
            canonical = canonicalize_url(record.url)
            candidate = candidates.setdefault(
                canonical,
                {
                    "canonical_url": canonical,
                    "url": record.url,
                    "title": record.title,
                    "records": [],
                    "evidence_snippets": [],
                    "workflow_signals": [],
                },
            )
            signals = ["apify_profile_result"]
            if subject.schools:
                signals.append("apify_school_filtered")
            title = normalized_person_name(record.title)
            if any(
                name and name in title
                for name in map(normalized_person_name, subject.names + subject.english_names)
            ):
                signals.append("person_name_match")
            merge_candidate(candidate, record, signals=signals)

    candidate_items = [candidate_item(candidate) for candidate in candidates.values()]
    match_reference = merged_reference_from_payload(subject, payload)
    _preliminary_summary, preliminary_annotations = score_entity_match_batch(match_reference, candidate_items)
    enrich_profile = getattr(apify_client, "enrich_profile", None)
    if candidates and callable(enrich_profile) and clean_bool(payload.get("enrich_linkedin_profile"), default=True):
        def preliminary_rank(candidate: Mapping[str, object]) -> tuple[float, int]:
            key = str(candidate.get("canonical_url") or candidate.get("url"))
            annotation = preliminary_annotations.get(key) or {}
            score, _reasons = workflow_score(float(annotation.get("score") or 0.0), candidate)
            records = candidate.get("records") if isinstance(candidate.get("records"), list) else []
            return score, len(records)

        selected = max(candidates.values(), key=preliminary_rank)
        selected_url = str(selected.get("url") or "")
        try:
            full_record = enrich_profile(
                selected_url,
                query=subject.query,
                query_intent="linkedin_profile_finder:full_profile",
            )
            merge_candidate(selected, full_record, signals=["apify_full_profile"])
            operations.append(
                {
                    "stage": "apify_profile",
                    "provider": "apify_linkedin",
                    "url": selected_url,
                    "ok": True,
                    "count": 1,
                }
            )
        except Exception as exc:
            operations.append(
                {
                    "stage": "apify_profile",
                    "provider": "apify_linkedin",
                    "url": selected_url,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                }
            )

    candidate_items = [candidate_item(candidate) for candidate in candidates.values()]
    identity_summary, identity_annotations = score_entity_match_batch(match_reference, candidate_items)

    ranked: list[dict[str, object]] = []
    for candidate in candidates.values():
        key = str(candidate.get("canonical_url") or candidate.get("url"))
        annotation = identity_annotations.get(key) or {}
        base_score = float(annotation.get("score") or 0.0)
        score, workflow_reasons = workflow_score(base_score, candidate)
        records = candidate.get("records") if isinstance(candidate.get("records"), list) else []
        primary_record = records[-1] if records else {}
        ranked.append(
            {
                "url": candidate.get("url"),
                "canonical_url": candidate.get("canonical_url"),
                "title": candidate.get("title"),
                "workflow_score": score,
                "workflow_score_reasons": workflow_reasons,
                "identity_label": annotation.get("identity_label"),
                "identity_match": annotation,
                "workflow_signals": dedupe(list(candidate.get("workflow_signals") or [])),
                "evidence_count": len(records),
                "primary_record": primary_record,
                "records": records,
            }
        )

    ranked.sort(key=lambda item: (float(item.get("workflow_score") or 0.0), int(item.get("evidence_count") or 0)), reverse=True)
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index

    return {
        "schema_version": "linkedin_profile_finder/v1",
        "query": subject.query,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "normalized_subject": {
            "names": subject.names,
            "english_names": subject.english_names,
            "schools": subject.schools,
            "school_aliases": subject.school_aliases,
            "companies": subject.companies,
            "company_aliases": subject.company_aliases,
            "company_linkedin_urls": subject.company_linkedin_urls,
        },
        "providers": providers,
        "best_match": ranked[0] if ranked else None,
        "candidates": ranked,
        "identity_match": identity_summary,
        "operations": operations,
        "serp_evidence": [
            record.to_dict()
            for record in serp_records
            if not is_linkedin_profile_url(record.url) or record_matches_subject_name(record, subject)
        ][:30],
    }
