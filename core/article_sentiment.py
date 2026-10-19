from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = PROJECT_ROOT / "models" / "article_sentiment"
METHOD = "local_target_sentiment_onnx/v1"
TARGET_TOKEN = "[TGT]"
LABELS = ("negative", "neutral", "positive")
MAX_TARGET_CHARS = 500
MAX_TITLE_CHARS = 600
MAX_SNIPPET_CHARS = 1800
MAX_PREVIEW_CHARS = 4800
EVIDENCE_SCOPES = {"title", "title_snippet", "article_preview", "insufficient"}
DECISION_POLICY = "conservative_abstain/v1"
DEFAULT_MIN_CONFIDENCE = 0.55
DEFAULT_MIN_DIRECTIONAL_MARGIN = 0.12

_RUNTIME_LOCK = threading.Lock()
_RUNTIME: LocalArticleSentimentClassifier | None = None


def compact_string(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def article_sentiment_disabled() -> bool:
    return os.getenv("WEB_SEARCH_ARTICLE_SENTIMENT_DISABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def article_sentiment_model_dir() -> Path:
    configured = os.getenv("WEB_SEARCH_ARTICLE_SENTIMENT_MODEL_DIR", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_MODEL_DIR


def load_model_metadata(model_dir: Path | None = None) -> dict[str, object]:
    directory = model_dir or article_sentiment_model_dir()
    raw = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("article sentiment metadata must be an object")
    return raw


def configured_model_name() -> str:
    try:
        return str(load_model_metadata().get("model") or "local-article-sentiment")
    except (OSError, ValueError, json.JSONDecodeError):
        return "local-article-sentiment"


def article_sentiment_readiness() -> dict[str, object]:
    directory = article_sentiment_model_dir()
    required_files = ("metadata.json", "model.int8.onnx", "tokenizer.json")
    missing_files = [name for name in required_files if not (directory / name).is_file()]
    missing_packages = [
        name for name in ("numpy", "onnxruntime", "tokenizers") if importlib.util.find_spec(name) is None
    ]
    return {
        "ready": not missing_files and not missing_packages and not article_sentiment_disabled(),
        "disabled": article_sentiment_disabled(),
        "model": configured_model_name(),
        "missing_files": missing_files,
        "missing_packages": missing_packages,
    }


def cpu_threads() -> int:
    try:
        value = int(os.getenv("WEB_SEARCH_ARTICLE_SENTIMENT_CPU_THREADS", "2"))
    except ValueError:
        value = 2
    return max(1, min(value, 8))


def _configured_float(name: str, default: float, *, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(low, min(value, high))


def sentiment_min_confidence() -> float:
    return _configured_float(
        "WEB_SEARCH_ARTICLE_SENTIMENT_MIN_CONFIDENCE",
        DEFAULT_MIN_CONFIDENCE,
        low=0.34,
        high=0.99,
    )


def sentiment_min_directional_margin() -> float:
    return _configured_float(
        "WEB_SEARCH_ARTICLE_SENTIMENT_MIN_DIRECTIONAL_MARGIN",
        DEFAULT_MIN_DIRECTIONAL_MARGIN,
        low=0.0,
        high=0.65,
    )


class LocalArticleSentimentClassifier:
    def __init__(self, model_dir: Path) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.model_dir = model_dir
        self.metadata = load_model_metadata(model_dir)
        self.model = str(self.metadata.get("model") or "local-article-sentiment")
        input_config = self.metadata.get("input")
        input_config = input_config if isinstance(input_config, dict) else {}
        self.max_length = max(16, int(input_config.get("max_length") or 192))
        self.temperature = max(0.05, float(self.metadata.get("temperature") or 1.0))
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.no_padding()
        self.tokenizer.enable_truncation(max_length=self.max_length)
        self.pad_id = self.tokenizer.token_to_id("[PAD]") or 0

        options = ort.SessionOptions()
        options.intra_op_num_threads = cpu_threads()
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            model_dir / "model.int8.onnx",
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def predict(self, masked_texts: list[str]) -> list[list[float]]:
        if not masked_texts:
            return []
        encodings = self.tokenizer.encode_batch(masked_texts)
        sequence_length = max(len(encoding.ids) for encoding in encodings)
        input_ids = self.np.full((len(encodings), sequence_length), self.pad_id, dtype=self.np.int64)
        attention_mask = self.np.zeros_like(input_ids)
        token_type_ids = self.np.zeros_like(input_ids)
        for row, encoding in enumerate(encodings):
            length = len(encoding.ids)
            input_ids[row, :length] = encoding.ids
            attention_mask[row, :length] = encoding.attention_mask
            token_type_ids[row, :length] = encoding.type_ids
        logits = self.session.run(
            ["logits"],
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )[0]
        logits = logits / self.temperature
        logits = logits - logits.max(axis=1, keepdims=True)
        probabilities = self.np.exp(logits)
        probabilities = probabilities / probabilities.sum(axis=1, keepdims=True)
        return probabilities.tolist()


def reset_article_sentiment_runtime() -> None:
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = None


def get_article_sentiment_runtime() -> LocalArticleSentimentClassifier:
    global _RUNTIME
    directory = article_sentiment_model_dir()
    with _RUNTIME_LOCK:
        if _RUNTIME is None or _RUNTIME.model_dir != directory:
            _RUNTIME = LocalArticleSentimentClassifier(directory)
        return _RUNTIME


def normalize_articles(items: list[Mapping[str, object]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        normalized.append(
            {
                "item_id": compact_string(item.get("item_id") or item.get("result_id") or f"article-{index}", 160),
                "source": compact_string(item.get("source"), 120),
                "title": compact_string(item.get("title"), MAX_TITLE_CHARS),
                "snippet": compact_string(item.get("snippet"), MAX_SNIPPET_CHARS),
                "article_preview": compact_string(
                    item.get("article_text_preview") or item.get("text_preview") or item.get("text"),
                    MAX_PREVIEW_CHARS,
                ),
                "published_at": compact_string(item.get("published_at"), 80),
            }
        )
    return normalized


def article_evidence(item: Mapping[str, str]) -> tuple[str, str]:
    title = item.get("title") or ""
    snippet = item.get("snippet") or ""
    preview = item.get("article_preview") or ""
    parts = [part for part in (title, snippet, preview) if part]
    if preview:
        scope = "article_preview"
    elif snippet:
        scope = "title_snippet"
    elif title:
        scope = "title"
    else:
        scope = "insufficient"
    return "\n".join(parts), scope


def mask_sentiment_target(text: str, target: str) -> str | None:
    if not text or not target:
        return None
    masked, count = re.subn(re.escape(target), TARGET_TOKEN, text, flags=re.IGNORECASE)
    return masked if count else None


def normalize_probabilities(value: object) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("article sentiment model must return three probabilities")
    parsed = [float(item) for item in value]
    if any(not math.isfinite(item) or item < 0 for item in parsed):
        raise ValueError("article sentiment model returned invalid probabilities")
    total = sum(parsed)
    if total <= 0:
        raise ValueError("article sentiment probabilities sum to zero")
    return parsed[0] / total, parsed[1] / total, parsed[2] / total


def directional_intensity(label: str, score: float) -> int:
    if label == "neutral":
        return 0
    return max(1, min(5, math.ceil(abs(score) * 5)))


def unavailable_annotation(
    status: str,
    reason: str,
    *,
    model: str,
    target: str,
    evidence_scope: str = "insufficient",
) -> dict[str, object]:
    return {
        "status": status,
        "method": METHOD,
        "decision_policy": DECISION_POLICY,
        "model": model,
        "target": target,
        "label": None,
        "score": None,
        "intensity": None,
        "confidence": None,
        "probabilities": None,
        "evidence_scope": evidence_scope if evidence_scope in EVIDENCE_SCOPES else "insufficient",
        "reason": reason,
    }


def unavailable_result(
    items: list[Mapping[str, object]],
    status: str,
    reason: str,
    *,
    model: str,
    target: str,
    enabled: bool,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    normalized = normalize_articles(items)
    return (
        {
            "enabled": enabled,
            "status": status,
            "method": METHOD,
            "decision_policy": DECISION_POLICY,
            "min_confidence": sentiment_min_confidence(),
            "min_directional_margin": sentiment_min_directional_margin(),
            "model": model,
            "target": target,
            "item_count": len(normalized),
            "items_scored": 0,
            "items_inferred": 0,
            "items_uncertain": 0,
            "items_insufficient": len(normalized),
            "elapsed_seconds": 0.0,
            "reason": reason,
        },
        {
            item["item_id"]: unavailable_annotation(status, reason, model=model, target=target)
            for item in normalized
        },
    )


def evaluate_article_sentiment_batch(
    target: str,
    items: list[Mapping[str, object]],
    *,
    usage_context: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    del usage_context
    model = configured_model_name()
    clean_target = compact_string(target, MAX_TARGET_CHARS)
    if article_sentiment_disabled():
        return unavailable_result(
            items,
            "skipped",
            "disabled by WEB_SEARCH_ARTICLE_SENTIMENT_DISABLED",
            model=model,
            target=clean_target,
            enabled=False,
        )
    if not clean_target:
        return unavailable_result(items, "skipped", "sentiment target is empty", model=model, target="", enabled=True)
    if not items:
        return unavailable_result(
            items,
            "skipped",
            "no articles to classify",
            model=model,
            target=clean_target,
            enabled=True,
        )

    normalized = normalize_articles(items)
    annotations: dict[str, dict[str, object]] = {}
    pending: list[tuple[dict[str, str], str, str]] = []
    for item in normalized:
        evidence, scope = article_evidence(item)
        masked = mask_sentiment_target(evidence, clean_target)
        if not masked:
            annotations[item["item_id"]] = unavailable_annotation(
                "insufficient",
                "target not found in supplied article text",
                model=model,
                target=clean_target,
                evidence_scope=scope,
            )
            continue
        pending.append((item, masked, scope))

    if not pending:
        reason = "target not found in supplied article text"
        summary = {
            "enabled": True,
            "status": "insufficient",
            "method": METHOD,
            "decision_policy": DECISION_POLICY,
            "min_confidence": sentiment_min_confidence(),
            "min_directional_margin": sentiment_min_directional_margin(),
            "model": model,
            "target": clean_target,
            "item_count": len(normalized),
            "items_scored": 0,
            "items_inferred": 0,
            "items_uncertain": 0,
            "items_insufficient": len(normalized),
            "elapsed_seconds": 0.0,
            "reason": reason,
        }
        return summary, annotations

    started = time.monotonic()
    min_confidence = sentiment_min_confidence()
    min_directional_margin = sentiment_min_directional_margin()
    try:
        runtime = get_article_sentiment_runtime()
        model = runtime.model
        predictions = runtime.predict([masked for _item, masked, _scope in pending])
        if len(predictions) != len(pending):
            raise ValueError("article sentiment model returned the wrong batch size")
        for (item, _masked, scope), raw_probabilities in zip(pending, predictions):
            negative, neutral, positive = normalize_probabilities(raw_probabilities)
            probabilities = (negative, neutral, positive)
            label = LABELS[max(range(3), key=probabilities.__getitem__)]
            score = positive - negative
            confidence = max(probabilities)
            confidence_margin = confidence - sorted(probabilities, reverse=True)[1]
            decision_status = "ok"
            decision_label: str | None = label
            reason: str | None = None
            if confidence < min_confidence:
                decision_status = "uncertain"
                decision_label = None
                reason = f"confidence below threshold ({confidence:.3f} < {min_confidence:.3f})"
            elif label in {"negative", "positive"} and confidence_margin < min_directional_margin:
                decision_status = "uncertain"
                decision_label = None
                reason = f"directional margin below threshold ({confidence_margin:.3f} < {min_directional_margin:.3f})"
            annotations[item["item_id"]] = {
                "status": decision_status,
                "method": METHOD,
                "decision_policy": DECISION_POLICY,
                "model": model,
                "target": clean_target,
                "label": decision_label,
                "model_label": label,
                "score": round(score, 4),
                "intensity": directional_intensity(label, score) if decision_status == "ok" else None,
                "confidence": round(confidence, 4),
                "confidence_margin": round(confidence_margin, 4),
                "probabilities": {
                    "negative": round(negative, 4),
                    "neutral": round(neutral, 4),
                    "positive": round(positive, 4),
                },
                "evidence_scope": scope,
            }
            if reason:
                annotations[item["item_id"]]["reason"] = reason
    except Exception as exc:
        reason = f"local article sentiment unavailable: {type(exc).__name__}"
        for item, _masked, scope in pending:
            annotations[item["item_id"]] = unavailable_annotation(
                "error",
                reason,
                model=model,
                target=clean_target,
                evidence_scope=scope,
            )
        return (
            {
                "enabled": True,
                "status": "error",
                "method": METHOD,
                "decision_policy": DECISION_POLICY,
                "min_confidence": min_confidence,
                "min_directional_margin": min_directional_margin,
                "model": model,
                "target": clean_target,
                "item_count": len(normalized),
                "items_scored": 0,
                "items_inferred": 0,
                "items_uncertain": 0,
                "items_insufficient": len(normalized),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "reason": reason,
            },
            annotations,
        )

    items_scored = sum(annotation.get("status") == "ok" for annotation in annotations.values())
    items_uncertain = sum(annotation.get("status") == "uncertain" for annotation in annotations.values())
    items_inferred = items_scored + items_uncertain
    items_insufficient = len(normalized) - items_scored
    return (
        {
            "enabled": True,
            "status": "ok",
            "method": METHOD,
            "decision_policy": DECISION_POLICY,
            "min_confidence": min_confidence,
            "min_directional_margin": min_directional_margin,
            "model": model,
            "target": clean_target,
            "item_count": len(normalized),
            "items_scored": items_scored,
            "items_inferred": items_inferred,
            "items_uncertain": items_uncertain,
            "items_insufficient": items_insufficient,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        },
        annotations,
    )
