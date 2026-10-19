from __future__ import annotations

import math
import re
import time
from typing import Any, Mapping
from urllib.parse import urlparse

MAX_TEXT_CHARS = 5000

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.I)
TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣._%+\-@]+")
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b", re.I)
DIGIT_SEQUENCE_RE = re.compile(r"\d[\d\s().\-+]{5,}\d")

PERSON_FIELD_ALIASES = {
    "name": ("name", "person_name", "korean_name", "이름", "성명"),
    "email": ("email", "mail", "email_address", "이메일", "메일"),
    "english_name": ("english_name", "englishName", "name_en", "eng_name", "영문명", "영문 이름"),
    "title": ("title", "position", "role", "job_title", "직책", "직위"),
    "department": ("department", "dept", "team", "division", "부서", "소속"),
    "company": ("company", "company_name", "current_company", "employer", "organization", "회사", "회사명", "직장"),
    "school": ("school", "schools", "university", "college", "education", "학교", "학력", "대학", "대학교"),
    "phone": ("phone", "telephone", "mobile", "tel", "phone_number", "전화번호", "연락처", "휴대폰"),
}

COMPANY_FIELD_ALIASES = {
    "company_name": ("company_name", "company", "name", "corp_name", "회사명", "법인명", "기업명"),
    "ceo_name": ("ceo_name", "ceo", "representative", "대표자", "대표자명", "대표"),
    "business_registration_number": ("business_registration_number", "business_number", "biz_no", "brn", "사업자번호", "사업자등록번호"),
    "corporate_registration_number": ("corporate_registration_number", "corporate_number", "corp_no", "crn", "법인번호", "법인등록번호"),
    "founded_at": ("founded_at", "founded_date", "established_at", "established", "설립일", "설립연월일"),
    "listed": ("listed", "is_listed", "listing_status", "상장여부", "상장"),
    "industry": ("industry", "sector", "business_type", "업종", "산업"),
    "patents_certifications": ("patents_certifications", "patents", "certifications", "ip", "특허/인증", "특허", "인증"),
    "address": ("address", "addr", "location", "주소", "소재지"),
    "school": ("school", "schools", "university", "college", "education", "학교", "학력", "대학", "대학교"),
    "main_products_services": ("main_products_services", "products_services", "products", "services", "주요제품/서비스", "주요제품", "주요서비스", "서비스"),
    "homepage": ("homepage", "home_page", "website", "url", "site", "홈페이지", "웹사이트"),
}

IDENTITY_LABEL_RESULT_FIELDS = (
    "title",
    "snippet",
    "text_preview",
    "text",
    "summary",
    "description",
    "award_name",
    "awarding_organization",
    "publisher",
    "author_role",
    "education",
    "career_history",
    "url",
    "article_url",
)

STOPWORDS = {
    "a",
    "an",
    "and",
    "article",
    "bio",
    "blog",
    "book",
    "ceo",
    "co",
    "com",
    "company",
    "corp",
    "email",
    "founder",
    "homepage",
    "inc",
    "info",
    "ir",
    "ltd",
    "news",
    "official",
    "page",
    "profile",
    "site",
    "the",
    "web",
    "website",
    "검색",
    "공식",
    "기사",
    "뉴스",
    "대표",
    "블로그",
    "사이트",
    "이메일",
    "인물",
    "정보",
    "프로필",
    "홈페이지",
    "회사",
}


def clamp01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def compact_text(value: object, max_chars: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def normalize_text(value: object) -> str:
    text = compact_text(value).lower()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_entity_text(value: object) -> str:
    return re.sub(r"[^0-9a-z가-힣@.]+", "", normalize_text(value))


def normalize_key(value: object) -> str:
    return re.sub(r"[\s_\-./]+", "", str(value or "").strip().lower())


def coerce_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def meaningful_value(value: object) -> bool:
    return value not in (None, "", [], {})


def as_text_values(value: object) -> list[str]:
    if not meaningful_value(value):
        return []
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(as_text_values(item))
        return values
    if isinstance(value, Mapping):
        values = []
        for item in value.values():
            values.extend(as_text_values(item))
        return values
    text = str(value).strip()
    return [text] if text else []


def field_values(source: Mapping[str, object], aliases: tuple[str, ...]) -> list[str]:
    alias_keys = {normalize_key(alias) for alias in aliases}
    values: list[str] = []
    for key, value in source.items():
        if normalize_key(key) in alias_keys:
            values.extend(as_text_values(value))
    return dedupe(values)


def canonical_fields(source: Mapping[str, object], aliases_by_field: Mapping[str, tuple[str, ...]]) -> dict[str, list[str]]:
    return {field: field_values(source, aliases) for field, aliases in aliases_by_field.items()}


def structured_reference(reference: Mapping[str, object]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    person = coerce_mapping(reference.get("person") or reference.get("Person") or reference.get("person_data"))
    company = coerce_mapping(reference.get("company") or reference.get("Company") or reference.get("company_data"))
    return canonical_fields(person, PERSON_FIELD_ALIASES), canonical_fields(company, COMPANY_FIELD_ALIASES)


def has_structured_values(fields: Mapping[str, list[str]]) -> bool:
    return any(bool(values) for values in fields.values())


def flatten_field_text(*field_groups: Mapping[str, list[str]]) -> str:
    parts: list[str] = []
    for fields in field_groups:
        for values in fields.values():
            parts.extend(values)
    return " ".join(parts)


def compact_candidates(values: list[str]) -> list[str]:
    candidates: list[str] = []
    for value in values:
        normalized = normalize_text(value)
        compact = compact_entity_text(value)
        if len(normalized) >= 2:
            candidates.append(normalized)
        if len(compact) >= 2:
            candidates.append(compact)
    return dedupe(candidates)


def extract_emails(value: object) -> set[str]:
    return {match.group(0).lower() for match in EMAIL_RE.finditer(str(value or ""))}


def email_domain(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def email_local_tokens(email: str) -> set[str]:
    local = email.split("@", 1)[0].lower()
    return {
        token
        for token in re.split(r"[^0-9a-z가-힣]+", local)
        if useful_token(token)
    }


def useful_token(token: str) -> bool:
    clean = token.strip("._-+@").lower()
    if len(clean) < 2:
        return False
    if clean in STOPWORDS:
        return False
    if clean.isdigit() and len(clean) < 4:
        return False
    return True


def extract_tokens(value: object) -> list[str]:
    text = normalize_text(value)
    emails = extract_emails(text)
    clean = text
    for email in emails:
        clean = clean.replace(email, " ")

    tokens: list[str] = []
    for raw in TOKEN_RE.findall(clean):
        token = raw.strip("._-+@").lower()
        if useful_token(token):
            tokens.append(token)

    for email in emails:
        domain = email_domain(email)
        if domain:
            root = domain.split(".", 1)[0]
            if useful_token(root):
                tokens.append(root)
        tokens.extend(sorted(email_local_tokens(email)))
    return dedupe(tokens)


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def char_ngrams(value: object, *, sizes: tuple[int, ...] = (2, 3)) -> set[str]:
    compact = compact_entity_text(value)
    if not compact:
        return set()
    grams: set[str] = set()
    for size in sizes:
        if len(compact) < size:
            continue
        grams.update(compact[index : index + size] for index in range(0, len(compact) - size + 1))
    return grams


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def weighted_overlap(query_tokens: set[str], result_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    total = sum(token_weight(token) for token in query_tokens)
    if total <= 0:
        return 0.0
    matched = sum(token_weight(token) for token in query_tokens & result_tokens)
    return matched / total


def token_weight(token: str) -> float:
    weight = math.log1p(len(token))
    if any(ch.isdigit() for ch in token):
        weight += 0.35
    if len(token) >= 6:
        weight += 0.2
    return weight


def query_phrase_candidates(reference: Mapping[str, object]) -> list[str]:
    source = str(
        reference.get("target")
        or reference.get("query")
        or reference.get("url")
        or reference.get("subject")
        or ""
    )
    clean = EMAIL_RE.sub(" ", source)
    tokens = extract_tokens(clean)
    candidates: list[str] = []
    if tokens:
        candidates.append(" ".join(tokens[:5]))
        candidates.append("".join(tokens[:5]))
    compact = compact_entity_text(clean)
    if 2 <= len(compact) <= 80:
        candidates.append(compact)
    return [candidate for candidate in dedupe(candidates) if len(candidate) >= 2]


def result_text_from_item(item: Mapping[str, object]) -> str:
    parts = [
        item.get("title"),
        item.get("url"),
        item.get("snippet"),
        item.get("text_preview"),
        item.get("text"),
        item.get("raw_payload_excerpt"),
    ]
    raw_payload = item.get("raw_payload_json") or item.get("raw_payload")
    if isinstance(raw_payload, dict):
        parts.extend(as_text_values(raw_payload))
    return compact_text(" ".join(str(part or "") for part in parts), MAX_TEXT_CHARS)


def domain_from_url(value: object) -> str:
    parsed = urlparse(str(value or ""))
    return parsed.netloc.lower().removeprefix("www.")


def domain_candidates(value: object) -> set[str]:
    text = str(value or "").strip()
    candidates: set[str] = set()
    values = [text]
    if text and "://" not in text:
        values.append("https://" + text)
    for candidate in values:
        domain = domain_from_url(candidate)
        if domain:
            candidates.add(domain)
    candidates.update(match.group(0).lower().removeprefix("www.") for match in DOMAIN_RE.finditer(text))
    return {candidate for candidate in candidates if "." in candidate}


def normalize_digits(value: object) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def digit_candidates(values: object, *, min_len: int = 7, max_len: int = 16) -> set[str]:
    candidates: set[str] = set()
    for value in as_text_values(values):
        direct = normalize_digits(value)
        if min_len <= len(direct) <= max_len:
            candidates.add(direct)
        for match in DIGIT_SEQUENCE_RE.finditer(value):
            digits = normalize_digits(match.group(0))
            if min_len <= len(digits) <= max_len:
                candidates.add(digits)
    return candidates


def compact_phrase_match(values: list[str], result_text: str, result_compact: str) -> bool:
    normalized_result = normalize_text(result_text)
    for candidate in compact_candidates(values):
        if " " in candidate:
            if candidate in normalized_result:
                return True
        elif candidate in result_compact:
            return True
    return False


def token_support_score(values: list[str], result_tokens: set[str]) -> float:
    tokens = set(extract_tokens(" ".join(values)))
    return weighted_overlap(tokens, result_tokens)


def label_for_score(score: float) -> str:
    if score >= 0.85:
        return "strong_match"
    if score >= 0.65:
        return "likely_match"
    if score >= 0.4:
        return "possible_match"
    if score >= 0.2:
        return "weak_match"
    return "no_match"


def identity_label_for_result(reference: Mapping[str, object], item: Mapping[str, object]) -> str | None:
    """Classify distinct structured keyword matches in visible result fields."""
    person, company = structured_reference(reference)
    result_text = compact_text(
        " ".join(
            text
            for key in IDENTITY_LABEL_RESULT_FIELDS
            for text in as_text_values(item.get(key))
        )
    )
    result_compact = compact_entity_text(result_text)

    names = dedupe(person.get("name", []) + person.get("english_name", []))
    name_keys = {compact_entity_text(value) for value in names}
    companies = [
        value
        for value in dedupe(company.get("company_name", []) + person.get("company", []))
        if compact_entity_text(value) not in name_keys
    ]
    used_keys = name_keys | {compact_entity_text(value) for value in companies}
    roles = [
        value
        for value in dedupe(person.get("title", []) + person.get("department", []))
        if compact_entity_text(value) not in used_keys
    ]

    name_match = compact_phrase_match(names, result_text, result_compact)
    company_match = compact_phrase_match(companies, result_text, result_compact)
    role_match = compact_phrase_match(roles, result_text, result_compact)
    if name_match and company_match:
        return "A"
    if name_match and role_match:
        return "B"
    if company_match:
        return "C"
    if name_match:
        return "D"
    return None


def score_entity_match(reference: Mapping[str, object], item: Mapping[str, object]) -> dict[str, object]:
    person, company = structured_reference(reference)
    structured = has_structured_values(person) or has_structured_values(company)
    fallback_text = " ".join(
        str(reference.get(key) or "")
        for key in ("target", "query", "url", "subject", "entity", "original_query")
    )
    structured_text = flatten_field_text(person, company)
    reference_text = structured_text or fallback_text
    result_text = result_text_from_item(item)
    result_compact = compact_entity_text(result_text)
    reference_tokens = set(extract_tokens(reference_text))
    result_tokens = set(extract_tokens(result_text))
    token_overlap = reference_tokens & result_tokens

    token_jaccard = jaccard(reference_tokens, result_tokens)
    token_coverage = len(token_overlap) / len(reference_tokens) if reference_tokens else 0.0
    ngram_score = jaccard(char_ngrams(reference_text), char_ngrams(result_text))
    lexical_score = weighted_overlap(reference_tokens, result_tokens)

    result_emails = extract_emails(result_text)
    reference_emails = extract_emails(reference_text)
    for value in person.get("email", []):
        reference_emails.update(extract_emails(value))
        if "@" in value:
            reference_emails.add(value.lower().strip())
    exact_email_match = bool(reference_emails & result_emails)
    reference_email_domains = {email_domain(email) for email in reference_emails if email_domain(email)}
    result_domains = set(domain_candidates(result_text))
    item_domain = domain_from_url(item.get("url") or item.get("canonical_url"))
    if item_domain:
        result_domains.add(item_domain)
    result_email_domains = {email_domain(email) for email in result_emails if email_domain(email)}
    email_domain_match = bool(reference_email_domains & (result_domains | result_email_domains))
    email_local_token_set = set().union(*(email_local_tokens(email) for email in reference_emails)) if reference_emails else set()
    email_local_match = bool(email_local_token_set & result_tokens)

    result_digits = digit_candidates(result_text, min_len=4, max_len=16)
    phone_digits = digit_candidates(person.get("phone", []), min_len=8, max_len=13)
    exact_phone_match = bool(phone_digits & result_digits)
    phone_suffix_match = any(
        len(phone) >= 8 and any(candidate.endswith(phone[-8:]) or phone.endswith(candidate[-8:]) for candidate in result_digits)
        for phone in phone_digits
    )
    business_numbers = digit_candidates(company.get("business_registration_number", []), min_len=10, max_len=10)
    corporate_numbers = digit_candidates(company.get("corporate_registration_number", []), min_len=13, max_len=13)
    exact_business_number_match = bool(business_numbers & result_digits)
    exact_corporate_number_match = bool(corporate_numbers & result_digits)
    founded_match = bool(digit_candidates(company.get("founded_at", []), min_len=4, max_len=8) & result_digits)

    homepage_domains: set[str] = set()
    for value in company.get("homepage", []):
        homepage_domains.update(domain_candidates(value))
    homepage_domain_match = bool(homepage_domains & result_domains)

    person_name_match = compact_phrase_match(person.get("name", []), result_text, result_compact)
    english_name_match = compact_phrase_match(person.get("english_name", []), result_text, result_compact)
    company_name_match = compact_phrase_match(company.get("company_name", []), result_text, result_compact)
    ceo_name_match = compact_phrase_match(company.get("ceo_name", []), result_text, result_compact)
    title_match = compact_phrase_match(person.get("title", []), result_text, result_compact)
    department_match = compact_phrase_match(person.get("department", []), result_text, result_compact)
    person_company_match = compact_phrase_match(person.get("company", []), result_text, result_compact)
    school_match = (
        compact_phrase_match(person.get("school", []) + company.get("school", []), result_text, result_compact)
        or token_support_score(person.get("school", []) + company.get("school", []), result_tokens) >= 0.45
    )
    industry_match = compact_phrase_match(company.get("industry", []), result_text, result_compact)
    listed_match = compact_phrase_match(company.get("listed", []), result_text, result_compact)
    address_match = token_support_score(company.get("address", []), result_tokens) >= 0.45
    product_match = token_support_score(company.get("main_products_services", []), result_tokens) >= 0.45
    certification_match = token_support_score(company.get("patents_certifications", []), result_tokens) >= 0.45

    signals: list[str] = []
    risk_flags: list[str] = []
    for flag, matched in (
        ("exact_person_email_match", exact_email_match),
        ("person_email_or_url_domain_match", email_domain_match),
        ("person_email_local_token_match", email_local_match),
        ("exact_person_phone_match", exact_phone_match),
        ("person_phone_suffix_match", phone_suffix_match and not exact_phone_match),
        ("exact_business_registration_number_match", exact_business_number_match),
        ("exact_corporate_registration_number_match", exact_corporate_number_match),
        ("company_homepage_domain_match", homepage_domain_match),
        ("person_name_phrase_match", person_name_match),
        ("person_english_name_match", english_name_match),
        ("company_name_phrase_match", company_name_match),
        ("ceo_name_phrase_match", ceo_name_match),
        ("person_title_match", title_match),
        ("person_department_match", department_match),
        ("person_company_match", person_company_match),
        ("school_match", school_match),
        ("company_industry_match", industry_match),
        ("company_address_token_support", address_match),
        ("company_product_service_token_support", product_match),
        ("company_patent_certification_support", certification_match),
        ("company_founded_date_match", founded_match),
        ("company_listing_status_match", listed_match),
    ):
        if matched:
            signals.append(flag)

    identifier_score = 0.0
    if exact_email_match:
        identifier_score = max(identifier_score, 1.0)
    if exact_phone_match:
        identifier_score = max(identifier_score, 0.92)
    elif phone_suffix_match:
        identifier_score = max(identifier_score, 0.72)
    if exact_business_number_match or exact_corporate_number_match:
        identifier_score = max(identifier_score, 0.92)
    if homepage_domain_match:
        identifier_score = max(identifier_score, 0.72)
    if email_domain_match and email_local_match:
        identifier_score = max(identifier_score, 0.78)
    elif email_domain_match:
        identifier_score = max(identifier_score, 0.55)

    person_name_score = 1.0 if person_name_match or english_name_match else 0.0
    company_score = 0.0
    if company_name_match:
        company_score = max(company_score, 1.0)
    if person_company_match:
        company_score = max(company_score, 0.75)
    if ceo_name_match:
        company_score = max(company_score, 0.45)
    if homepage_domain_match:
        company_score = max(company_score, 0.8)
    if exact_business_number_match or exact_corporate_number_match:
        company_score = max(company_score, 0.95)
    context_score = min(
        sum(
            1
            for matched in (
                title_match,
                department_match,
                person_company_match,
                school_match,
                industry_match,
                address_match,
                product_match,
                certification_match,
                founded_match,
                listed_match,
            )
            if matched
        )
        / 3,
        1.0,
    )

    score = 0.30 * identifier_score + 0.25 * person_name_score + 0.20 * company_score + 0.10 * context_score + 0.10 * lexical_score + 0.05 * ngram_score
    if exact_email_match:
        score = max(score, 0.98)
    elif exact_phone_match and (person_name_match or company_name_match):
        score = max(score, 0.94)
    elif exact_business_number_match or exact_corporate_number_match:
        score = max(score, 0.9 if has_structured_values(person) and not person_name_score else 0.96)
    elif person_name_score and (company_name_match or homepage_domain_match or email_domain_match):
        score = max(score, 0.86)
    elif person_name_score and (school_match or person_company_match):
        score = max(score, 0.86)
    elif person_name_score and context_score >= 0.34:
        score = max(score, 0.78)
    elif not structured and query_phrase_candidates(reference) and any(candidate in result_compact for candidate in query_phrase_candidates(reference)):
        score = max(score, 0.58)
    elif company_score >= 0.95 and not has_structured_values(person):
        score = max(score, 0.88)
    elif company_name_match and not has_structured_values(person):
        score = max(score, 0.72)
    elif person_name_score:
        score = max(score, 0.56)

    if reference_emails and not exact_email_match:
        risk_flags.append("person_email_not_exactly_seen")
    if phone_digits and not exact_phone_match and not phone_suffix_match:
        risk_flags.append("person_phone_not_seen")
    if business_numbers and not exact_business_number_match:
        risk_flags.append("business_registration_number_not_seen")
    if corporate_numbers and not exact_corporate_number_match:
        risk_flags.append("corporate_registration_number_not_seen")
    if homepage_domains and not homepage_domain_match:
        risk_flags.append("company_homepage_domain_not_seen")
    if has_structured_values(person) and (person.get("name") or person.get("english_name")) and not person_name_score:
        risk_flags.append("person_name_not_seen")
        if identifier_score < 0.9:
            score = min(score, 0.62)
    if has_structured_values(person) and person_name_score and not (company_name_match or email_domain_match or homepage_domain_match or context_score or school_match or person_company_match):
        risk_flags.append("name_only_match")
        score = min(score, 0.72)
    if has_structured_values(person) and not person_name_score and company_score > 0 and identifier_score < 0.9:
        risk_flags.append("company_only_match_for_person_target")
        score = min(score, 0.58)
    if not token_overlap and identifier_score == 0 and person_name_score == 0 and company_score == 0:
        risk_flags.append("no_direct_reference_overlap")
        score = min(score, 0.18)
    if token_coverage >= 0.65:
        signals.append("high_reference_token_coverage")
    if token_jaccard >= 0.22:
        signals.append("token_jaccard_support")
    if lexical_score >= 0.55:
        signals.append("weighted_lexical_support")

    score = clamp01(score)
    return {
        "score": round(score, 4),
        "label": label_for_score(score),
        "identity_label": identity_label_for_result(reference, item),
        "method": "identity_match_formula_v2" if structured else "identity_match_formula_v2_generic_fallback",
        "components": {
            "identifier_score": round(identifier_score, 4),
            "person_name_score": round(person_name_score, 4),
            "company_score": round(company_score, 4),
            "context_score": round(context_score, 4),
            "lexical_score": round(lexical_score, 4),
            "char_ngram_score": round(ngram_score, 4),
            "token_jaccard": round(token_jaccard, 4),
            "reference_token_coverage": round(token_coverage, 4),
        },
        "features": {
            "person_field_count": sum(1 for values in person.values() if values),
            "company_field_count": sum(1 for values in company.values() if values),
            "reference_token_count": len(reference_tokens),
            "result_token_count": len(result_tokens),
            "matched_token_count": len(token_overlap),
            "reference_email_count": len(reference_emails),
            "result_email_count": len(result_emails),
        },
        "signals": dedupe(signals),
        "risk_flags": dedupe(risk_flags),
    }

def score_entity_match_batch(
    reference: Mapping[str, object],
    items: list[Mapping[str, object]],
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    started = time.monotonic()
    annotations: dict[str, dict[str, object]] = {}
    for index, item in enumerate(items, start=1):
        item_id = str(item.get("item_id") or item.get("result_id") or item.get("url") or f"item-{index}")
        annotations[item_id] = score_entity_match(reference, item)
    scores = [float(item["score"]) for item in annotations.values()]
    summary = {
        "status": "ok",
        "method": "identity_match_formula_v2",
        "item_count": len(items),
        "elapsed_seconds": round(time.monotonic() - started, 4),
        "max_score": round(max(scores), 4) if scores else None,
        "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
    }
    return summary, annotations
