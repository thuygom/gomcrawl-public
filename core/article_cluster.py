"""Small, explainable article-event clustering PoC.

The PoC deliberately keeps the search contract separate from clustering.  It
uses a local lexical vectorizer by default (no network, GPU, or new runtime
dependency), and can opt into a local sentence-transformers model when one is
installed.  Both paths feed the same threshold/centroid clustering code.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import importlib.util
import math
import re
from typing import Any, Mapping, Sequence

from .utils import canonicalize_url, compact_text


SCHEMA_VERSION = "article_cluster_poc/v1"
METHOD = "centroid_threshold_agglomerative/v1"
LEXICAL_BACKEND = "tfidf_word_char_v1"
DEFAULT_SIMILARITY_THRESHOLD = 0.30
DEFAULT_TIME_WINDOW_DAYS = 90
MAX_ARTICLES = 50
MAX_TEXT_CHARS = 8_000
ALLOWED_EMBEDDING_MODELS = frozenset({"intfloat/multilingual-e5-small"})

_TOKEN_RE = re.compile(r"[가-힣]+|[A-Za-z]+\d+|[A-Za-z]{2,}|\d{2,}")
_SPACE_RE = re.compile(r"\s+")
_STOPWORDS = {
    "뉴스",
    "기사",
    "관련",
    "대한",
    "통해",
    "및",
    "등",
    "기자",
    "전했다",
    "밝혔다",
    "지난",
    "올해",
    "이번",
    "대해",
    "있는",
    "있다",
    "한다",
    "했다",
    "것으로",
    "으로",
    "에서",
    "하는",
    "하며",
    "이후",
    "예정",
    "보도",
    "소식",
    "공개",
}
_TEXT_FIELDS = (
    ("title", 2.5),
    ("snippet", 1.0),
    ("article_text_preview", 1.0),
    ("text_preview", 1.0),
    ("description", 0.8),
    ("summary", 0.8),
    ("content", 1.0),
)


def _number(value: object, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(low, min(parsed, high))


def _integer(value: object, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _string(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    return compact_text(str(value or ""))[:limit]


def _identity_terms(values: Sequence[object]) -> list[str]:
    terms: list[str] = []
    for value in values:
        text = _string(value, 300).strip()
        if not text:
            continue
        for marker in ("주식회사", "(주)", "㈜"):
            text = text.replace(marker, "")
        text = compact_text(text)
        if text and text.casefold() not in {term.casefold() for term in terms}:
            terms.append(text)
    return terms


def _remove_identity(text: str, terms: Sequence[str]) -> str:
    result = text
    for term in sorted(_identity_terms(terms), key=len, reverse=True):
        if len(term) < 2:
            continue
        result = re.sub(re.escape(term), " ", result, flags=re.IGNORECASE)
    return _SPACE_RE.sub(" ", result).strip()


def _article_text(item: Mapping[str, object], identity_terms: Sequence[str]) -> tuple[str, list[str]]:
    segments: list[str] = []
    used: list[str] = []
    for field, weight in _TEXT_FIELDS:
        value = _string(item.get(field))
        if not value:
            continue
        value = _remove_identity(value, identity_terms)
        if not value:
            continue
        # Repeating a title is a cheap, deterministic field weight and keeps
        # the vectorizer dependency-free.
        repeats = max(1, round(weight))
        segments.extend([value] * repeats)
        used.append(field)
    return " ".join(segments)[:MAX_TEXT_CHARS], used


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    suffixes = ("으로", "에서", "에게", "까지", "부터", "처럼", "보다", "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "과", "와", "로")
    for raw_token in _TOKEN_RE.findall(text):
        token = raw_token.casefold()
        if token in _STOPWORDS or len(token) <= 1:
            continue
        tokens.append(token)
        if re.search(r"[가-힣]", token):
            for suffix in suffixes:
                if token.endswith(suffix) and len(token) - len(suffix) > 1:
                    stem = token[: -len(suffix)]
                    if stem not in _STOPWORDS:
                        tokens.append(stem)
                    break
    return tokens


def _char_ngrams(text: str, minimum: int = 2, maximum: int = 5) -> list[str]:
    compact = re.sub(r"[^0-9A-Za-z가-힣]", "", text.casefold())
    return [f"{compact[index:index + size]}" for size in range(minimum, maximum + 1) for index in range(len(compact) - size + 1)]


def _normalise(values: Mapping[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(value * value for value in values.values()))
    if norm <= 0:
        return {}
    return {key: value / norm for key, value in values.items()}


def _sparse_cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    if not left or not right:
        return 0.0
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    cosine = sum(value * larger.get(key, 0.0) for key, value in smaller.items())
    left_words = {key for key in left if key.startswith("w:")}
    right_words = {key for key in right if key.startswith("w:")}
    if left_words and right_words:
        overlap = len(left_words & right_words) / min(len(left_words), len(right_words))
        # Cosine alone is too sparse for short Korean headlines.  This
        # content-overlap term is applied after identity removal, so a common
        # company name cannot make unrelated articles join.
        cosine = max(cosine, 0.5 * cosine + 0.5 * overlap)
    return max(0.0, min(1.0, cosine))


def _lexical_vectors(texts: Sequence[str]) -> tuple[list[dict[str, float]], list[dict[str, float]], list[str]]:
    documents: list[Counter[str]] = []
    lexical_terms: list[Counter[str]] = []
    document_frequency: Counter[str] = Counter()
    for text in texts:
        words = Counter(f"w:{token}" for token in _tokens(text))
        chars = Counter(f"c:{gram}" for gram in _char_ngrams(" ".join(_tokens(text))))
        # Character evidence is useful for Korean spacing and inflections but
        # deliberately has lower weight than word evidence.
        features = words + Counter({key: count * 0.35 for key, count in chars.items()})
        lexical_terms.append(words)
        documents.append(features)
        document_frequency.update(features.keys())

    count = len(documents)
    vectors: list[dict[str, float]] = []
    for features in documents:
        weighted: dict[str, float] = {}
        for key, term_frequency in features.items():
            inverse_document_frequency = math.log((count + 1) / (document_frequency[key] + 1)) + 1.0
            weighted[key] = term_frequency * inverse_document_frequency
        vectors.append(_normalise(weighted))
    terms = [key[2:] for key in sorted(document_frequency) if key.startswith("w:")]
    return vectors, [dict(term) for term in lexical_terms], terms


def _parse_datetime(value: object) -> datetime | None:
    text = _string(value, 120)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            parsed = None
    if parsed is None:
        for date_format in ("%Y. %m. %d.", "%Y.%m.%d.", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
    if parsed is None:
        relative = re.fullmatch(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", text, re.IGNORECASE)
        if relative:
            units = {"minute": 1 / 1_440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}
            parsed = datetime.now(timezone.utc) - timedelta(days=int(relative.group(1)) * units[relative.group(2).lower()])
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _article_id(item: Mapping[str, object], index: int) -> str:
    url = _string(item.get("article_url") or item.get("url"), 2_048)
    title = _string(item.get("title"), 500)
    seed = canonicalize_url(url) if url else f"{title}|{index}"
    if not seed:
        seed = f"article-{index}"
    return f"article-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"


def _load_sentence_encoder(model_name: str) -> tuple[Any | None, str | None]:
    if not model_name or model_name.casefold() in {"auto", "tfidf", "lexical", "none"}:
        return None, None
    if model_name not in ALLOWED_EMBEDDING_MODELS:
        return None, "embedding model is not allowlisted; used lexical fallback"
    if importlib.util.find_spec("sentence_transformers") is None:
        return None, "sentence-transformers is not installed; used lexical fallback"
    try:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(model_name, device="cpu"), None
    except Exception as exc:  # model download/config errors must not break news search
        return None, f"embedding model unavailable ({type(exc).__name__}); used lexical fallback"


def _dense_vectors(texts: Sequence[str], model_name: str) -> tuple[list[list[float]] | None, str | None, str | None]:
    if not texts:
        return None, None, None
    model, warning = _load_sentence_encoder(model_name)
    if model is None:
        return None, None, warning
    try:
        prefixed = [f"passage: {text}" for text in texts]
        encoded = model.encode(prefixed, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        if getattr(encoded, "ndim", 2) == 1:
            encoded = [encoded]
        rows = [[float(value) for value in row] for row in encoded]
        if not rows or any(not row for row in rows):
            return None, None, "embedding model returned empty vectors; used lexical fallback"
        return rows, f"sentence_transformers/{model_name}", warning
    except Exception as exc:
        return None, None, f"embedding inference failed ({type(exc).__name__}); used lexical fallback"


def _dense_cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right))))


def _dense_mean(rows: Sequence[Sequence[float]]) -> list[float]:
    if not rows:
        return []
    result = [sum(row[index] for row in rows) / len(rows) for index in range(len(rows[0]))]
    norm = math.sqrt(sum(value * value for value in result))
    return [value / norm for value in result] if norm else result


def _cluster_similarity(vector: Any, centroid: Any, dense: bool) -> float:
    return _dense_cosine(vector, centroid) if dense else _sparse_cosine(vector, centroid)


def _cluster_news(
    entries: Sequence[dict[str, object]],
    lexical_vectors: Sequence[dict[str, float]],
    dense_vectors: Sequence[Sequence[float]] | None,
    *,
    threshold: float,
    time_window_days: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    dense = dense_vectors is not None
    clusters: list[dict[str, object]] = []
    unclustered: list[dict[str, object]] = []
    for index, entry in enumerate(entries):
        vector = dense_vectors[index] if dense else lexical_vectors[index]
        if not vector:
            unclustered.append({"article_id": entry["article_id"], "reason": "insufficient_text_after_identity_removal"})
            continue
        published = _parse_datetime(entry.get("published_at"))
        best_index = -1
        best_score = -1.0
        for candidate_index, cluster in enumerate(clusters):
            dates = [value for value in cluster["dates"] if isinstance(value, datetime)]
            if published and dates and min(abs((published - date).total_seconds()) for date in dates) > time_window_days * 86_400:
                continue
            score = _cluster_similarity(vector, cluster["centroid"], dense)
            if score > best_score:
                best_index, best_score = candidate_index, score
        if best_index < 0 or best_score < threshold:
            cluster = {
                "indices": [index],
                "vectors": [vector],
                "centroid": vector,
                "dates": [published] if published else [],
                "scores": [1.0],
            }
            clusters.append(cluster)
            best_index, best_score = len(clusters) - 1, 1.0
        else:
            cluster = clusters[best_index]
            cluster["indices"].append(index)
            cluster["vectors"].append(vector)
            cluster["centroid"] = _dense_mean(cluster["vectors"]) if dense else _normalise(
                Counter({key: sum(row.get(key, 0.0) for row in cluster["vectors"]) for key in {key for row in cluster["vectors"] for key in row}})
            )
            if published:
                cluster["dates"].append(published)
            cluster["scores"].append(best_score)
    return clusters, unclustered


def _top_terms(indices: Sequence[int], lexical_terms: Sequence[Mapping[str, float]], limit: int = 5) -> list[str]:
    scores: Counter[str] = Counter()
    for index in indices:
        for key, value in lexical_terms[index].items():
            scores[key.removeprefix("w:")] += float(value)
    return [term for term, _score in scores.most_common(limit)]


def cluster_news_items(
    items: Sequence[Mapping[str, object]],
    *,
    query: str = "",
    identity_terms: Sequence[object] = (),
    embedding_model: str = "",
    similarity_threshold: object = DEFAULT_SIMILARITY_THRESHOLD,
    time_window_days: object = DEFAULT_TIME_WINDOW_DAYS,
    max_articles: object = MAX_ARTICLES,
) -> dict[str, object]:
    """Cluster already-fetched news items without searching or reordering them.

    The returned ``items`` are copies annotated with cluster membership.  A
    caller can therefore render the result without retaining private vectors.
    """
    threshold = _number(similarity_threshold, DEFAULT_SIMILARITY_THRESHOLD, 0.10, 0.99)
    window = _integer(time_window_days, DEFAULT_TIME_WINDOW_DAYS, 0, 3650)
    limit = _integer(max_articles, MAX_ARTICLES, 1, MAX_ARTICLES)
    selected = [dict(item) for item in items[:limit] if isinstance(item, Mapping)]
    identity = _identity_terms(identity_terms)
    texts: list[str] = []
    used_fields: list[list[str]] = []
    entries: list[dict[str, object]] = []
    for index, item in enumerate(selected):
        text, fields = _article_text(item, identity)
        article = dict(item)
        article_id = _article_id(article, index)
        article["article_id"] = article_id
        article["cluster_id"] = None
        article["cluster_similarity"] = None
        article["cluster_role"] = None
        entries.append(article)
        texts.append(text)
        used_fields.append(fields)

    lexical_vectors, lexical_terms, _feature_names = _lexical_vectors(texts)
    dense, dense_backend, warning = _dense_vectors(texts, _string(embedding_model, 200))
    clusters, unclustered = _cluster_news(
        entries,
        lexical_vectors,
        dense,
        threshold=threshold,
        time_window_days=window,
    )
    output_clusters: list[dict[str, object]] = []
    for cluster_index, cluster in enumerate(clusters, start=1):
        indices = list(cluster["indices"])
        centroid = cluster["centroid"]
        similarities = [
            _cluster_similarity((dense[index] if dense else lexical_vectors[index]), centroid, dense is not None)
            for index in indices
        ]
        representative_offset = max(range(len(indices)), key=lambda offset: similarities[offset])
        representative_index = indices[representative_offset]
        cluster_id = f"cluster-{cluster_index:03d}"
        for index in indices:
            entries[index]["cluster_id"] = cluster_id
            entries[index]["cluster_similarity"] = round(float(similarities[indices.index(index)]), 4)
            entries[index]["cluster_role"] = "representative" if index == representative_index else "member"
            entries[index]["cluster_text_fields"] = used_fields[index]
        dates = [_parse_datetime(entries[index].get("published_at")) for index in indices]
        dates = [date for date in dates if date]
        output_clusters.append(
            {
                "cluster_id": cluster_id,
                "article_count": len(indices),
                "article_ids": [entries[index]["article_id"] for index in indices],
                "representative_article_id": entries[representative_index]["article_id"],
                "representative_article": entries[representative_index],
                "representative_similarity": round(float(similarities[representative_offset]), 4),
                "cohesion": round(sum(float(score) for score in similarities) / len(similarities), 4),
                "top_terms": _top_terms(indices, lexical_terms),
                "published_at_min": min(dates).isoformat() if dates else None,
                "published_at_max": max(dates).isoformat() if dates else None,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD,
        "query": _string(query, 500),
        "count": len(entries),
        "cluster_count": len(output_clusters),
        "clustered_count": sum(int(cluster["article_count"]) for cluster in output_clusters),
        "unclustered_count": len(unclustered),
        "parameters": {
            "embedding_backend": dense_backend or LEXICAL_BACKEND,
            "embedding_model": _string(embedding_model, 200) or None,
            "similarity_threshold": threshold,
            "time_window_days": window,
            "max_articles": limit,
            "identity_terms_removed": identity,
            "text_fields": [field for field, _weight in _TEXT_FIELDS],
            "representative_selection": "nearest_to_cluster_centroid",
            "ann_index": "not_used_in_poc_exact_scan; HNSW-compatible vector boundary",
        },
        "warnings": [warning] if warning else [],
        "clusters": output_clusters,
        "unclustered": unclustered,
        "items": entries,
    }
