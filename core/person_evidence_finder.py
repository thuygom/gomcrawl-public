from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import quote_plus

from .crawlers import create_crawler
from .entity_match import score_entity_match_batch
from .linkedin_finder import as_text_values, dedupe, subject_from_payload
from .llm_confidence import clean_bool, evaluate_confidence_batch
from .schema import CrawlResult, EvidenceRecord
from .search import create_search_client
from .usage import record_api_usage
from .utils import canonicalize_url, ordered_parallel_map


SearchClientFactory = Callable[[str], Any]
CrawlerFactory = Callable[[str], Any]

DEFAULT_SERP_PROVIDERS = ("serper",)
DEFAULT_CATEGORIES = ("books", "publications", "awards", "institutions", "career")
DEFAULT_MAX_QUERIES_PER_SOURCE = 2
DEFAULT_SERP_LIMIT = 3
DEFAULT_CRAWL_LIMIT = 3
MAX_CRAWL_TEXT_CHARS = 5000
WORD_RE = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True, slots=True)
class EvidenceSourceSpec:
    source: str
    category: str
    label: str
    url: str
    provider_kind: str
    templates: tuple[str, ...]
    direct_url_template: str | None = None


SOURCE_SPECS: tuple[EvidenceSourceSpec, ...] = (
    EvidenceSourceSpec(
        source="kyobo_books",
        category="books",
        label="Kyobo book search",
        url="https://search.kyobobook.co.kr/search?keyword=",
        provider_kind="direct",
        templates=(
            'site:product.kyobobook.co.kr "{name}" "{context}"',
            'site:search.kyobobook.co.kr/search "{name}" "{context}"',
        ),
        direct_url_template="https://search.kyobobook.co.kr/search?keyword={query}",
    ),
    EvidenceSourceSpec(
        source="google_scholar",
        category="publications",
        label="Google Scholar",
        url="https://scholar.google.com/scholar",
        provider_kind="direct",
        templates=(
            'site:scholar.google.com "{name}" "{context}"',
            '"{name}" "{context}" "Google Scholar"',
        ),
        direct_url_template="https://scholar.google.com/scholar?hl=ko&q={query}",
    ),
    EvidenceSourceSpec(
        source="earticle",
        category="publications",
        label="eArticle",
        url="https://www.earticle.net/",
        provider_kind="direct",
        templates=(
            'site:earticle.net/Article "{name}" "{context}"',
            'site:earticle.net "{name}" "{context}"',
        ),
        direct_url_template="https://www.earticle.net/Search/Result?sf=0&q={query}",
    ),
    EvidenceSourceSpec(
        source="kci",
        category="publications",
        label="KCI",
        url="https://www.kci.go.kr/",
        provider_kind="direct",
        templates=(
            'site:kci.go.kr "{name}" "{context}"',
            '"{name}" "{context}" "KCI"',
        ),
        direct_url_template=(
            "https://www.kci.go.kr/kciportal/po/search/poArtiSearList.kci"
            "?poSearchBean.queryText={query}&queryTextPiece={query}"
        ),
    ),
    EvidenceSourceSpec(
        source="riss",
        category="publications",
        label="RISS",
        url="https://www.riss.kr/",
        provider_kind="direct",
        templates=(
            'site:riss.kr "{name}" "{context}"',
            '"{name}" "{context}" "RISS"',
        ),
        direct_url_template="https://www.riss.kr/search/Search.do?query={query}",
    ),
    EvidenceSourceSpec(
        source="dbpia",
        category="publications",
        label="DBpia",
        url="https://www.dbpia.co.kr/",
        provider_kind="direct",
        templates=(
            'site:dbpia.co.kr "{name}" "{context}"',
            '"{name}" "{context}" "DBpia"',
        ),
        direct_url_template="https://www.dbpia.co.kr/search/topSearch?searchOption=all&query={query}",
    ),
    EvidenceSourceSpec(
        source="koreascience",
        category="publications",
        label="KoreaScience",
        url="https://www.koreascience.kr/",
        provider_kind="direct",
        templates=(
            'site:koreascience.kr "{name}" "{context}"',
            '"{name}" "{context}" "KoreaScience"',
        ),
        direct_url_template="https://www.koreascience.kr/main.page?keywords={query}",
    ),
    EvidenceSourceSpec(
        source="academic_web",
        category="publications",
        label="Academic web",
        url="https://www.google.com/search",
        provider_kind="serp",
        templates=(
            '"{name}" "{context}" 논문',
            '"{name}" "{context}" 학술지',
        ),
    ),
    EvidenceSourceSpec(
        source="sanghun",
        category="awards",
        label="Republic of Korea honors",
        url="https://www.sanghun.go.kr/nation/nationMain.do",
        provider_kind="direct",
        templates=(
            'site:sanghun.go.kr "{name}" "{context}"',
            '"{name}" "{context}" "대한민국 상훈"',
            '"{name}" "{context}" "정부포상"',
        ),
        direct_url_template="https://www.sanghun.go.kr/nation/nationMain.do",
    ),
    EvidenceSourceSpec(
        source="korean_honor_web",
        category="awards",
        label="Korean public honors web",
        url="https://www.google.com/search",
        provider_kind="serp",
        templates=(
            '("{name}" "{context}") ("훈장" OR "포장" OR "대통령 표창" OR "국무총리 표창" OR "장관 표창")',
            '("{name}" "{context}") ("정부포상" OR "상훈" OR "표창" OR "수상")',
        ),
    ),
    EvidenceSourceSpec(
        source="asan_nanum",
        category="institutions",
        label="Asan Nanum Foundation",
        url="https://asan-nanum.org/",
        provider_kind="direct",
        templates=(
            'site:asan-nanum.org "{name}" "{context}"',
            '"{name}" "{context}" "아산나눔재단"',
        ),
        direct_url_template="https://asan-nanum.org/?s={query}",
    ),
    EvidenceSourceSpec(
        source="kita",
        category="institutions",
        label="Korea International Trade Association",
        url="https://www.kita.net/",
        provider_kind="direct",
        templates=(
            'site:kita.net "{name}" "{context}"',
            '"{name}" "{context}" "한국무역협회"',
        ),
        direct_url_template=(
            "https://www.kita.net/aiSearch/aiSearch/aiSearchResult.do"
            "?aiSearchValue={query}&searchAiOnOff=N"
        ),
    ),
    EvidenceSourceSpec(
        source="korcham",
        category="institutions",
        label="Korea Chamber of Commerce and Industry",
        url="https://www.korcham.net/nCham/Service/Main/appl/Main.asp",
        provider_kind="direct",
        templates=(
            'site:korcham.net "{name}" "{context}"',
            '"{name}" "{context}" "대한상공회의소"',
        ),
        direct_url_template=(
            "https://www.korcham.net/nCham/Service/Search/appl/TotalSearch.asp"
            "?category_main=TOTAL&kwd={query}"
        ),
    ),
    EvidenceSourceSpec(
        source="kef",
        category="institutions",
        label="Korea Enterprises Federation",
        url="https://www.kefplaza.com/web/index.do",
        provider_kind="direct",
        templates=(
            'site:kefplaza.com "{name}" "{context}"',
            '"{name}" "{context}" "한국경영자총협회"',
        ),
        direct_url_template="https://www.kefplaza.com/web/site/search.do?kwd={query}",
    ),
    EvidenceSourceSpec(
        source="public_profile_web",
        category="institutions",
        label="Public profile and press web",
        url="https://www.google.com/search",
        provider_kind="serp",
        templates=(
            '"{name}" "{context}" 보도자료',
            '"{name}" "{context}" 임원 인터뷰 프로필',
        ),
    ),
    EvidenceSourceSpec(
        source="saramin",
        category="career",
        label="Saramin",
        url="https://www.saramin.co.kr/",
        provider_kind="direct",
        templates=('"{name}" "{context}" 경력',),
        direct_url_template="https://www.saramin.co.kr/zf_user/search?searchword={query}",
    ),
    EvidenceSourceSpec(
        source="jobkorea",
        category="career",
        label="JobKorea",
        url="https://www.jobkorea.co.kr/",
        provider_kind="direct",
        templates=('"{name}" "{context}" 경력',),
        direct_url_template="https://www.jobkorea.co.kr/Search/?stext={query}",
    ),
    EvidenceSourceSpec(
        source="wanted",
        category="career",
        label="Wanted",
        url="https://www.wanted.co.kr/",
        provider_kind="direct",
        templates=('"{name}" "{context}" 경력',),
        direct_url_template="https://www.wanted.co.kr/search?query={query}&tab=overview",
    ),
    EvidenceSourceSpec(
        source="career_platforms",
        category="career",
        label="Korean career platforms",
        url="https://www.saramin.co.kr/",
        provider_kind="serp",
        templates=(
            '"{name}" "{context}" 경력 프로필',
            '"{name}" "{context}" 재직 인터뷰 약력',
        ),
    ),
)


def payload_categories(payload: Mapping[str, object], forced: tuple[str, ...] | None = None) -> list[str]:
    if forced:
        return list(forced)
    raw = payload.get("categories") or payload.get("category")
    if not raw:
        return list(DEFAULT_CATEGORIES)
    values = [str(value).strip().lower() for value in as_text_values(raw)]
    aliases = {
        "book": "books",
        "books": "books",
        "publication": "publications",
        "publications": "publications",
        "paper": "publications",
        "papers": "publications",
        "award": "awards",
        "awards": "awards",
        "honor": "awards",
        "honors": "awards",
        "institution": "institutions",
        "institutions": "institutions",
        "career": "career",
        "jobs": "career",
    }
    selected = [aliases[value] for value in values if value in aliases]
    return dedupe(selected) or list(DEFAULT_CATEGORIES)


def provider_list_from_payload(payload: Mapping[str, object]) -> list[str]:
    raw = payload.get("providers") or payload.get("serp_providers")
    if raw:
        values = [str(value).strip().lower() for value in as_text_values(raw)]
        providers = [value for value in values if value in DEFAULT_SERP_PROVIDERS]
        if providers:
            return dedupe(providers)
    provider = str(payload.get("provider") or "").strip().lower()
    if provider in DEFAULT_SERP_PROVIDERS:
        return [provider]
    return list(DEFAULT_SERP_PROVIDERS)


def context_values(subject: object) -> list[str]:
    schools = list(getattr(subject, "school_aliases", []) or [])
    companies = list(getattr(subject, "company_aliases", []) or [])
    query = str(getattr(subject, "query", "") or "").casefold()
    schools = sorted(schools, key=lambda value: (0 if str(value).casefold() in query else 1, len(str(value))))
    companies = sorted(companies, key=lambda value: (0 if str(value).casefold() in query else 1, len(str(value))))
    contexts: list[str] = []
    for index in range(max(len(schools), len(companies), 1)):
        if index < len(schools):
            contexts.append(schools[index])
        if index < len(companies):
            contexts.append(companies[index])
    return dedupe(contexts)


def english_name_query_variants(value: str) -> list[str]:
    clean = re.sub(r"[^A-Za-z\-\s]+", " ", str(value or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return []
    tokens = clean.replace("-", " ").split()
    if len(tokens) < 2 or len(tokens) > 4:
        return []

    last_name = tokens[-1]
    given = " ".join(tokens[:-1])
    given_compact = "".join(tokens[:-1])
    variants = [f"{given} {last_name}", f"{given_compact} {last_name}"]

    lower_given = given_compact.casefold()
    if lower_given in {"jeonghun", "jeonghoon", "junghun", "junghoon"}:
        if lower_given.startswith("jeong"):
            first = "Jeong"
        elif lower_given.startswith("jung"):
            first = "Jung"
        else:
            first = given_compact[:5].title()
        second = "Hoon" if lower_given.endswith("hoon") else "Hun"
        alternates = ["Hun", "Hoon"] if second == "Hun" else ["Hoon", "Hun"]
        for alt in alternates:
            variants.append(f"{first}-{alt} {last_name}")
            variants.append(f"{first} {alt} {last_name}")
            variants.append(f"{first}{alt.lower()} {last_name}")

    return dedupe([titlecase_english_name(item) for item in variants])


def titlecase_english_name(value: str) -> str:
    parts = []
    for token in str(value or "").split():
        if "-" in token:
            parts.append("-".join(part[:1].upper() + part[1:].lower() for part in token.split("-") if part))
        else:
            parts.append(token[:1].upper() + token[1:].lower())
    return " ".join(parts)


def english_names_from_query(subject: object) -> list[str]:
    query = str(getattr(subject, "query", "") or "").strip()
    if not query:
        return []
    contexts = context_values(subject)
    lowered = query.casefold()
    cut = len(query)
    for marker in contexts + ["mro", "multimodal", "multomodal", "routing", "neural architecture", "논문"]:
        marker_lower = marker.casefold()
        index = lowered.find(marker_lower)
        if index > 0:
            cut = min(cut, index)
    leading = query[:cut].strip()
    candidates: list[str] = []
    words = WORD_RE.findall(leading)
    if 2 <= len(words) <= 4:
        candidates.append(" ".join(words))
    elif len(words) > 4:
        candidates.append(" ".join(words[:2]))
    variants: list[str] = []
    for candidate in candidates:
        variants.append(candidate)
        variants.extend(english_name_query_variants(candidate))
    return dedupe(variants)


def subject_names(subject: object) -> list[str]:
    names = list(getattr(subject, "names", []) or [])
    english_names = list(getattr(subject, "english_names", []) or [])
    english_names.extend(english_names_from_query(subject))
    variants: list[str] = []
    for name in english_names:
        variants.append(str(name))
        variants.extend(english_name_query_variants(str(name)))
    return dedupe(names + variants)


def build_queries(spec: EvidenceSourceSpec, subject: object, *, max_queries: int) -> list[str]:
    names = subject_names(subject)
    contexts = context_values(subject)
    query_text = str(getattr(subject, "query", "") or "").strip()
    if not names and query_text:
        names = [query_text]
    if not contexts:
        contexts = [query_text]

    queries: list[str] = []
    for name in names[:6]:
        for context in contexts[:6]:
            for template in spec.templates:
                query = template.format(name=name, context=context).strip()
                if query:
                    queries.append(query)
    return dedupe(queries)[:max_queries]


def direct_source_query(subject: object) -> str:
    values = subject_names(subject)[:1] + context_values(subject)[:1]
    return " ".join(dedupe(values)) or str(getattr(subject, "query", "") or "").strip()


def direct_source_url(spec: EvidenceSourceSpec, subject: object) -> str:
    template = spec.direct_url_template or spec.url
    return template.replace("{query}", quote_plus(direct_source_query(subject)))


def direct_result_excerpt(result: CrawlResult, subject: object) -> str:
    text = str(result.text or "")
    lowered = text.casefold()
    matches = [
        lowered.find(name.casefold())
        for name in subject_names(subject)
        if name and lowered.find(name.casefold()) >= 0
    ]
    if not matches:
        return ""
    start = max(0, min(matches) - 400)
    return text[start : start + 2000]


def sanghun_result_excerpt(result: CrawlResult, subject: object) -> str:
    names = {name.casefold() for name in subject_names(subject)}
    companies = [
        normalized
        for company in getattr(subject, "company_aliases", []) or []
        if (normalized := re.sub(r"[\s._/()\-]+", "", company.casefold()))
    ]
    rows = result.raw_payload.get("table_rows")
    matched_rows: list[list[str]] = []
    person_rows_seen = False
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            values = [str(value or "").strip() for value in row[:6]]
            if values[0].casefold() not in names:
                continue
            person_rows_seen = True
            affiliation = re.sub(r"[\s._/()\-]+", "", values[1].casefold())
            if companies and not any(company in affiliation for company in companies):
                continue
            matched_rows.append(values)
    if matched_rows:
        labels = ("성명", "소속", "훈격", "수여일", "사유", "추천기관")
        return "\n".join(
            " | ".join(f"{label}: {value}" for label, value in zip(labels, row))
            for row in matched_rows
        )[:2000]
    if person_rows_seen:
        return ""

    excerpt = direct_result_excerpt(result, subject)
    if companies:
        normalized = re.sub(r"[\s._/()\-]+", "", excerpt.casefold())
        if not any(company in normalized for company in companies):
            return ""
    return excerpt


def sanghun_search_actions(name: str) -> list[dict[str, object]]:
    return [
        {
            "kind": "click",
            "selector": 'a[href*="/nation/participation/sangopen/sangInfoOpen.do"]',
        },
        {"kind": "wait_for", "selector": "#searchNmBasic", "timeout_ms": 5000},
        {"kind": "fill", "selector": "#searchNmBasic", "value": name},
        {"kind": "fill", "selector": "#pages", "value": "0"},
        {"kind": "fill", "selector": "#listSearchCheck", "value": "Y"},
        {"kind": "fill", "selector": "#searchSe", "value": "B"},
        {"kind": "submit", "selector": 'form[name="sangInfoOpenForm"]'},
        {"kind": "wait", "ms": 1000},
        {
            "kind": "wait_for",
            "selector": '#listSearchCheck[value="Y"]',
            "timeout_ms": 8000,
        },
    ]


def source_specs_for(categories: list[str]) -> list[EvidenceSourceSpec]:
    allowed = set(categories)
    return [spec for spec in SOURCE_SPECS if spec.category in allowed]


def candidate_key(record: EvidenceRecord, spec: EvidenceSourceSpec) -> str:
    return f"{spec.category}:{canonicalize_url(record.url)}"


def record_to_item(candidate: Mapping[str, object]) -> dict[str, object]:
    records = candidate.get("records") if isinstance(candidate.get("records"), list) else []
    snippets: list[str] = []
    raw_values: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        snippets.append(str(record.get("title") or ""))
        snippets.append(str(record.get("snippet") or ""))
        raw_payload = record.get("raw_payload_json")
        if isinstance(raw_payload, Mapping):
            raw_values.extend(as_text_values(raw_payload))
    crawl_result = candidate.get("crawl_result") if isinstance(candidate.get("crawl_result"), Mapping) else {}
    return {
        "item_id": candidate.get("candidate_id"),
        "source": candidate.get("source"),
        "source_type": candidate.get("category"),
        "title": candidate.get("title"),
        "url": candidate.get("url"),
        "snippet": " ".join(snippets + raw_values + as_text_values(crawl_result)),
        "text_preview": crawl_result.get("text_preview") if crawl_result else "",
        "raw_payload_json": {
            "category": candidate.get("category"),
            "source": candidate.get("source"),
            "source_label": candidate.get("source_label"),
            "crawl_result": crawl_result,
        },
    }


def merge_record(
    candidates: dict[str, dict[str, object]],
    record: EvidenceRecord,
    spec: EvidenceSourceSpec,
    *,
    provider: str,
    query: str,
) -> None:
    key = candidate_key(record, spec)
    candidate = candidates.setdefault(
        key,
        {
            "candidate_id": key,
            "category": spec.category,
            "source": spec.source,
            "source_label": spec.label,
            "source_url": spec.url,
            "title": record.title,
            "url": record.url,
            "canonical_url": canonicalize_url(record.url),
            "providers": [],
            "queries": [],
            "records": [],
        },
    )
    providers = candidate.setdefault("providers", [])
    if isinstance(providers, list) and provider not in providers:
        providers.append(provider)
    queries = candidate.setdefault("queries", [])
    if isinstance(queries, list) and query not in queries:
        queries.append(query)
    records = candidate.setdefault("records", [])
    if isinstance(records, list):
        records.append(record.to_dict())


def mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def reference_from_subject(subject: object, payload: Mapping[str, object] | None = None) -> dict[str, object]:
    reference: dict[str, object] = {
        "kind": "person_evidence_finder",
        "query": str(getattr(subject, "query", "") or ""),
        "target": " ".join(subject_names(subject) + context_values(subject)),
        "person": {
            "name": list(getattr(subject, "names", []) or []),
            "english_name": list(getattr(subject, "english_names", []) or []),
            "company": list(getattr(subject, "company_aliases", []) or []),
            "school": list(getattr(subject, "school_aliases", []) or []),
        },
        "company": {
            "company_name": list(getattr(subject, "company_aliases", []) or []),
            "school": list(getattr(subject, "school_aliases", []) or []),
        },
    }
    if not payload:
        return reference
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


def publication_page_kind(candidate: Mapping[str, object]) -> str:
    title = str(candidate.get("title") or "").casefold()
    url = str(candidate.get("url") or "").casefold()
    if ".pdf" in url or "/bitstream/" in url or "pdfview" in url:
        return "publication_pdf"
    if (
        "earticle.net/article/" in url
        or "koreascience.kr/article/" in url
        or "serearticleview" in url
        or "sereartiview" in url
        or "artiview" in url
        or "dbpia.co.kr/journal/articledetail" in url
        or "riss.kr/search/detail/" in url
    ):
        return "publication_article"
    if "downloadsocofmanuscript" in url or "manuscriptlink.com" in url:
        return "publication_pdf"
    if any(token in title for token in ("논문 검색 결과", "통합 검색", "browsing", "repository", "목차", "program", "임원구성도")):
        return "search_or_index_page"
    if any(token in url for token in ("search", "browse", "serearits", "poartisearlist", "schedule")):
        return "search_or_index_page"
    return "publication_candidate"


def crawl_failed_or_blocked(candidate: Mapping[str, object]) -> bool:
    crawl_result = candidate.get("crawl_result")
    if not isinstance(crawl_result, Mapping):
        return False
    error = str(crawl_result.get("error") or "").strip()
    text = str(crawl_result.get("text_preview") or "").casefold()
    if error:
        return True
    return "403" in text[:300] or "access is denied" in text[:500] or "사용 권한 없음" in text[:500]


def workflow_score(
    category: str,
    identity_score: float,
    signals: list[str],
    *,
    page_kind: str = "",
    crawl_blocked: bool = False,
) -> tuple[float, list[str]]:
    score = identity_score
    reasons: list[str] = []
    if category == "books" and identity_score >= 0.4:
        score = max(score, 0.72)
        reasons.append("book/source candidate overlaps with person context")
    if category == "awards" and identity_score >= 0.4:
        score = max(score, 0.74)
        reasons.append("award/honor source candidate overlaps with person context")
    if category == "publications" and identity_score >= 0.4:
        score = max(score, 0.7)
        reasons.append("publication source candidate overlaps with person context")
    if category == "publications" and page_kind == "search_or_index_page":
        score = min(score, 0.58)
        reasons.append("publication search/index page; needs detail-page verification")
    if category == "publications" and crawl_blocked:
        score = min(score, 0.35)
        reasons.append("crawl returned blocked/error content")
    if "name_only_match" in signals:
        score = min(score, 0.68)
        reasons.append("name-only match capped")
    return round(min(score, 1.0), 4), reasons


def preliminary_ranked_candidates(
    candidates: Mapping[str, dict[str, object]],
    reference: Mapping[str, object],
) -> list[dict[str, object]]:
    items = [record_to_item(candidate) for candidate in candidates.values()]
    _summary, annotations = score_entity_match_batch(reference, items)
    ranked = []
    for candidate in candidates.values():
        candidate_id = str(candidate.get("candidate_id") or "")
        annotation = annotations.get(candidate_id) or {}
        signals = list(annotation.get("signals") if isinstance(annotation.get("signals"), list) else [])
        page_kind = publication_page_kind(candidate) if candidate.get("category") == "publications" else ""
        score, _reasons = workflow_score(
            str(candidate.get("category") or ""),
            float(annotation.get("score") or 0.0),
            signals,
            page_kind=page_kind,
            crawl_blocked=crawl_failed_or_blocked(candidate),
        )
        ranked.append(
            {
                "candidate": candidate,
                "score": score,
                "evidence_count": len(candidate.get("records") if isinstance(candidate.get("records"), list) else []),
            }
        )
    ranked.sort(key=lambda item: (float(item["score"]), int(item["evidence_count"])), reverse=True)
    return ranked


def maybe_crawl_top_candidates(
    candidates: dict[str, dict[str, object]],
    reference: Mapping[str, object],
    payload: Mapping[str, object],
    operations: list[dict[str, object]],
    *,
    crawler_factory: CrawlerFactory,
) -> None:
    if not clean_bool(payload.get("crawl_top"), default=False):
        return
    crawler_name = str(payload.get("crawler") or "firecrawl").strip() or "firecrawl"
    crawl_limit = max(1, min(int(payload.get("crawl_limit") or DEFAULT_CRAWL_LIMIT), 10))
    ranked = preliminary_ranked_candidates(candidates, reference)[:crawl_limit]
    if not ranked:
        return
    try:
        crawler = crawler_factory(crawler_name)
    except Exception as exc:
        operations.append(
            {
                "stage": "crawl",
                "provider": crawler_name,
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
            }
        )
        return
    for item in ranked:
        candidate = item["candidate"]
        url = str(candidate.get("url") or "").strip()
        if not url:
            continue
        try:
            result = crawler.crawl(url)
        except Exception as exc:
            candidate["crawl_result"] = {
                "crawler": crawler_name,
                "url": url,
                "error": str(exc)[:500],
            }
            operations.append(
                {
                    "stage": "crawl",
                    "provider": crawler_name,
                    "url": url,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                }
            )
            continue
        text = str(result.text or "")
        candidate["crawl_result"] = {
            "crawler": result.crawler,
            "url": result.url,
            "title": result.title,
            "status_code": result.status_code,
            "error": result.error,
            "text_length": len(text),
            "text_preview": text[:MAX_CRAWL_TEXT_CHARS],
            "raw_payload": {
                key: value
                for key, value in result.raw_payload.items()
                if key
                in {"managed_service", "format", "markdown_length", "target_politeness", "content_type", "bot_detection"}
            },
        }
        operations.append(
            {
                "stage": "crawl",
                "provider": result.crawler,
                "url": result.url,
                "ok": not bool(result.error),
                "status_code": result.status_code,
                "text_length": len(text),
                "error": result.error,
                "bot_detection": result.raw_payload.get("bot_detection"),
            }
        )


def find_person_evidence(
    payload: Mapping[str, object],
    *,
    forced_categories: tuple[str, ...] | None = None,
    client_factory: SearchClientFactory = create_search_client,
    crawler_factory: CrawlerFactory = create_crawler,
) -> dict[str, object]:
    started = time.monotonic()
    subject = subject_from_payload(payload)
    categories = payload_categories(payload, forced_categories)
    serp_providers = provider_list_from_payload(payload)
    max_queries = max(1, min(int(payload.get("max_queries_per_source") or DEFAULT_MAX_QUERIES_PER_SOURCE), 8))
    serp_limit = max(1, min(int(payload.get("serp_limit") or payload.get("limit") or DEFAULT_SERP_LIMIT), 10))
    include_llm = clean_bool(payload.get("llm_confidence"), default=False)
    direct_crawler = str(payload.get("direct_crawler") or "auto").strip().lower().replace("-", "_") or "auto"

    operations: list[dict[str, object]] = []
    candidates: dict[str, dict[str, object]] = {}
    search_jobs: list[tuple[EvidenceSourceSpec, str, str, int, str]] = []
    serp_fallbacks: dict[tuple[str, str], list[tuple[EvidenceSourceSpec, str, str, int, str]]] = {}
    for spec in source_specs_for(categories):
        if spec.provider_kind == "direct":
            names = subject_names(subject)
            if spec.source == "sanghun" and not names:
                continue
            query = names[0] if spec.source == "sanghun" else direct_source_query(subject)
            crawler_name = "stealth_browser" if spec.source == "sanghun" else direct_crawler
            search_jobs.append((spec, query, crawler_name, 1, direct_source_url(spec, subject)))
            continue
        queries = build_queries(spec, subject, max_queries=max_queries)
        if not queries:
            continue
        providers = serp_providers
        limit = serp_limit
        for provider in providers:
            provider_jobs = [(spec, query, provider, limit, "") for query in queries]
            if spec.provider_kind == "serp":
                search_jobs.append(provider_jobs[0])
                serp_fallbacks[(spec.source, provider)] = provider_jobs[1:]
            else:
                search_jobs.extend(provider_jobs)

    def run_search(
        job: tuple[EvidenceSourceSpec, str, str, int, str],
    ) -> tuple[list[EvidenceRecord], Exception | None, CrawlResult | None]:
        spec, query, provider, limit, direct_url = job
        try:
            if direct_url:
                crawler = (
                    create_crawler(provider, respect_robots=False)
                    if crawler_factory is create_crawler
                    else crawler_factory(provider)
                )
                if spec.source == "sanghun":
                    crawl_with_actions = getattr(crawler, "crawl_with_actions", None)
                    result = (
                        crawl_with_actions(direct_url, sanghun_search_actions(query))
                        if callable(crawl_with_actions)
                        else crawler.crawl(direct_url)
                    )
                else:
                    result = crawler.crawl(direct_url)
                excerpt = (
                    sanghun_result_excerpt(result, subject)
                    if spec.source == "sanghun"
                    else direct_result_excerpt(result, subject)
                )
                if result.error or (result.status_code is not None and result.status_code >= 400) or not excerpt:
                    return [], None, result
                record = EvidenceRecord.build(
                    source=spec.source,
                    source_type="direct_source_crawl",
                    title=result.title or spec.label,
                    url=result.url,
                    snippet=excerpt,
                    raw_payload_json={
                        "crawler": result.crawler,
                        "status_code": result.status_code,
                        "text_length": len(result.text or ""),
                        "error": result.error,
                        "bot_detection": result.raw_payload.get("bot_detection"),
                    },
                    retrieved_at=result.retrieved_at,
                    query=query,
                    query_intent=f"person_evidence:{spec.category}:{spec.source}:direct",
                    confidence=0.7,
                    confidence_reason=f"{result.crawler} direct source crawl",
                    processing_status="raw",
                )
                return [record], None, result
            return (
                client_factory(provider).search(
                    query,
                    limit=limit,
                    query_intent=f"person_evidence:{spec.category}:{spec.source}",
                ),
                None,
                None,
            )
        except Exception as exc:
            return [], exc, None

    completed_jobs = list(zip(search_jobs, ordered_parallel_map(run_search, search_jobs)))
    serp_counts: dict[tuple[str, str], int] = {}
    wave = completed_jobs
    while wave:
        next_jobs: list[tuple[EvidenceSourceSpec, str, str, int, str]] = []
        for (spec, _query, provider, limit, direct_url), (records, error, _crawl_result) in wave:
            if direct_url or spec.provider_kind != "serp":
                continue
            key = (spec.source, provider)
            serp_counts[key] = serp_counts.get(key, 0) + len(records)
            fallbacks = serp_fallbacks.get(key) or []
            if (error is not None or serp_counts[key] < limit) and fallbacks:
                next_jobs.append(fallbacks.pop(0))
        if not next_jobs:
            break
        wave = list(zip(next_jobs, ordered_parallel_map(run_search, next_jobs)))
        completed_jobs.extend(wave)

    for (spec, query, provider, _limit, direct_url), (records, error, crawl_result) in completed_jobs:
        if direct_url:
            ok = bool(
                error is None
                and crawl_result is not None
                and not crawl_result.error
                and crawl_result.text
                and (crawl_result.status_code is None or crawl_result.status_code < 400)
            )
            record_api_usage(
                f"crawler_{provider}",
                "crawl",
                ok=ok,
                status_code=crawl_result.status_code if crawl_result is not None else None,
                result_count=1 if ok else 0,
                metadata={
                    "error_type": type(error).__name__ if error is not None else None,
                    "mode": "direct_source",
                    "crawler": crawl_result.crawler if crawl_result is not None else provider,
                },
            )
            operations.append(
                {
                    "stage": spec.category,
                    "source": spec.source,
                    "provider": provider,
                    "mode": "direct_url",
                    "url": direct_url,
                    "query": query,
                    "ok": ok,
                    "status_code": crawl_result.status_code if crawl_result is not None else None,
                    "text_length": len(crawl_result.text or "") if crawl_result is not None else 0,
                    "count": len(records),
                    "error_type": type(error).__name__ if error is not None else None,
                    "error": str(error)[:300] if error is not None else (crawl_result.error if crawl_result else None),
                    "bot_detection": (
                        crawl_result.raw_payload.get("bot_detection") if crawl_result is not None else None
                    ),
                }
            )
            for record in records:
                merge_record(candidates, record, spec, provider=provider, query=query)
            continue
        if error is not None:
            operations.append(
                {
                    "stage": spec.category,
                    "source": spec.source,
                    "provider": provider,
                    "query": query,
                    "ok": False,
                    "error_type": type(error).__name__,
                    "error": str(error)[:300],
                }
            )
            continue
        operations.append(
            {
                "stage": spec.category,
                "source": spec.source,
                "provider": provider,
                "query": query,
                "ok": True,
                "count": len(records),
            }
        )
        for record in records:
            merge_record(candidates, record, spec, provider=provider, query=query)

    reference = reference_from_subject(subject, payload)
    maybe_crawl_top_candidates(
        candidates,
        reference,
        payload,
        operations,
        crawler_factory=crawler_factory,
    )
    candidate_items = [record_to_item(candidate) for candidate in candidates.values()]
    identity_summary, identity_annotations = score_entity_match_batch(reference, candidate_items)
    if include_llm and candidate_items:
        llm_summary, llm_annotations = evaluate_confidence_batch(reference, candidate_items, enabled=True, usage_context=payload)
    else:
        llm_summary, llm_annotations = (
            {"status": "skipped", "reason": "disabled by request", "item_count": len(candidate_items)},
            {},
        )

    ranked: list[dict[str, object]] = []
    for candidate in candidates.values():
        candidate_id = str(candidate.get("candidate_id") or "")
        identity = identity_annotations.get(candidate_id) or {}
        llm = llm_annotations.get(candidate_id)
        signals = list(identity.get("signals") if isinstance(identity.get("signals"), list) else [])
        page_kind = publication_page_kind(candidate) if candidate.get("category") == "publications" else ""
        score, reasons = workflow_score(
            str(candidate.get("category") or ""),
            float(identity.get("score") or 0.0),
            signals,
            page_kind=page_kind,
            crawl_blocked=crawl_failed_or_blocked(candidate),
        )
        if llm and isinstance(llm, Mapping) and llm.get("confidence") is not None:
            try:
                score = round(max(score, min(float(llm.get("confidence") or 0.0), 1.0)), 4)
            except (TypeError, ValueError):
                pass
        records = candidate.get("records") if isinstance(candidate.get("records"), list) else []
        ranked.append(
            {
                "candidate_id": candidate_id,
                "category": candidate.get("category"),
                "source": candidate.get("source"),
                "source_label": candidate.get("source_label"),
                "source_url": candidate.get("source_url"),
                "title": candidate.get("title"),
                "url": candidate.get("url"),
                "canonical_url": candidate.get("canonical_url"),
                "page_kind": page_kind,
                "workflow_score": score,
                "workflow_score_reasons": reasons,
                "identity_match": identity,
                "llm_confidence": llm,
                "providers": candidate.get("providers"),
                "queries": candidate.get("queries"),
                "crawl_result": candidate.get("crawl_result"),
                "evidence_count": len(records),
                "primary_record": records[-1] if records else None,
                "records": records,
            }
        )
    ranked.sort(
        key=lambda item: (
            float(item.get("workflow_score") or 0.0),
            int(item.get("evidence_count") or 0),
        ),
        reverse=True,
    )
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index

    by_category: dict[str, list[dict[str, object]]] = {}
    for item in ranked:
        by_category.setdefault(str(item.get("category") or "unknown"), []).append(item)

    return {
        "schema_version": "person_evidence_finder/v1",
        "query": str(getattr(subject, "query", "") or ""),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "normalized_subject": {
            "names": list(getattr(subject, "names", []) or []),
            "english_names": list(getattr(subject, "english_names", []) or []),
            "schools": list(getattr(subject, "schools", []) or []),
            "school_aliases": list(getattr(subject, "school_aliases", []) or []),
            "companies": list(getattr(subject, "companies", []) or []),
            "company_aliases": list(getattr(subject, "company_aliases", []) or []),
        },
        "categories": categories,
        "serp_providers": serp_providers,
        "best_by_category": {category: items[0] for category, items in by_category.items() if items},
        "candidates": ranked,
        "candidates_by_category": by_category,
        "identity_match": identity_summary,
        "llm_confidence": llm_summary,
        "operations": operations,
        "source_specs": [
            {
                "source": spec.source,
                "category": spec.category,
                "label": spec.label,
                "url": spec.url,
                "provider_kind": spec.provider_kind,
            }
            for spec in source_specs_for(categories)
        ],
    }
