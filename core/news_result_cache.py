from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from .usage import usage_dir


NEWS_RESULT_CACHE_TTL_SECONDS = 24 * 60 * 60
_NEWS_CACHE_DB_LOCK = threading.Lock()
# Separate from Serper: computing a news miss can acquire its search locks.
# ponytail: striped locks bound memory; unrelated colliding keys run sequentially.
_NEWS_CACHE_KEY_LOCKS = tuple(threading.Lock() for _ in range(64))
_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS responses "
    "(cache_key TEXT PRIMARY KEY, stored_at REAL NOT NULL, payload_json TEXT NOT NULL)"
)


def _news_cache_path() -> Path:
    return usage_dir().parent / "cache" / "complete_news.sqlite3"


def _cache_now() -> float:
    return time.time()


def _cache_read(cache_key: str) -> dict[str, object] | None:
    try:
        path = _news_cache_path()
        with _NEWS_CACHE_DB_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path, timeout=5.0)) as database:
                database.execute(_CREATE_TABLE)
                row = database.execute(
                    "SELECT stored_at, payload_json FROM responses WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
            path.chmod(0o600)
        if row is None or _cache_now() - float(row[0]) >= NEWS_RESULT_CACHE_TTL_SECONDS:
            return None
        payload = json.loads(row[1])
        return payload if isinstance(payload, dict) else None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _cache_write(cache_key: str, payload: dict[str, object]) -> None:
    try:
        path = _news_cache_path()
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        stored_at = _cache_now()
        with _NEWS_CACHE_DB_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path, timeout=5.0)) as database, database:
                database.execute(_CREATE_TABLE)
                database.execute(
                    "DELETE FROM responses WHERE stored_at <= ?",
                    (stored_at - NEWS_RESULT_CACHE_TTL_SECONDS,),
                )
                database.execute(
                    "INSERT OR REPLACE INTO responses(cache_key, stored_at, payload_json) VALUES (?, ?, ?)",
                    (cache_key, stored_at, serialized),
                )
            path.chmod(0o600)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return


def _cacheable(payload: dict[str, object]) -> bool:
    cluster = payload.get("article_cluster")
    sentiment = payload.get("article_sentiment")
    if not isinstance(cluster, dict) or cluster.get("status") != "ok" or not isinstance(sentiment, dict):
        return False
    empty = payload.get("count") == 0
    subtasks = payload.get("subtasks")
    if empty and isinstance(subtasks, list) and any(
        isinstance(task, dict) and (
            task.get("status") == "failed" or task.get("timed_out_providers") or task.get("error_providers")
        ) for task in subtasks
    ):
        return False
    return sentiment.get("status") in {"ok", "insufficient"} or (
        empty and sentiment.get("status") == "skipped"
    )


def cached_news_result(
    key: str, compute: Callable[[], dict[str, object]]
) -> tuple[dict[str, object], bool]:
    cache_key = hashlib.sha256(key.encode("utf-8")).hexdigest()
    lock = _NEWS_CACHE_KEY_LOCKS[int(cache_key[:8], 16) % len(_NEWS_CACHE_KEY_LOCKS)]
    with lock:
        cached = _cache_read(cache_key)
        if cached is not None:
            return cached, True
        result = compute()
        if _cacheable(result):
            _cache_write(cache_key, result)
        return result, False
