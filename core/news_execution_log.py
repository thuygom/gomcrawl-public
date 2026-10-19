"""Durable, best-effort export of direct news executions."""
from __future__ import annotations

import datetime as _datetime
import hmac
import json
import logging
import os
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
NEWS_LOG_URL = os.getenv(
    "WEB_SEARCH_ORCH_NEWS_LOG_URL",
    "http://poc-mas-api:8001/api/internal/web-search/news-execution",
)
TOKEN_FILE = Path(os.getenv("WEB_SEARCH_ORCH_NEWS_LOG_TOKEN_FILE", str(ROOT / "key" / "orch_news_log.key")))
OUTBOX_PATH = ROOT / ".logs" / "news_execution_outbox.sqlite3"
MAX_EVENT_BYTES = 1_900_000
LOGGER = logging.getLogger(__name__)
TOKEN_COUNT_KEYS = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens", "inputtokens", "outputtokens", "prompttokens", "completiontokens", "totaltokens", "llm_tokens_total", "cached_input_tokens", "cache_read_input_tokens", "cache_write_input_tokens", "reasoning_tokens"}
_outbox: "NewsExecutionOutbox | None" = None
_worker: threading.Thread | None = None


def _token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def orch_news_logged(headers: Any) -> bool:
    token = _token()
    marker = headers.get("X-Orch-News-Logged", "") if hasattr(headers, "get") else ""
    try:
        return bool(token and marker and hmac.compare_digest(str(marker).encode(), token.encode()))
    except UnicodeEncodeError:
        return False


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    if _depth >= 8:
        return "[truncated]"
    if isinstance(value, dict):
        sensitive = ("authorization", "token", "api_key", "apikey", "password", "secret", "credential", "cookie")
        return {
            str(key)[:200]: (item if str(key).lower() in TOKEN_COUNT_KEYS and type(item) in (int, float) else "[redacted]" if any(part in str(key).lower() for part in sensitive) else sanitize(item, _depth=_depth + 1))
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [sanitize(item, _depth=_depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4000]


def _timestamp() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


class NewsExecutionOutbox:
    def __init__(self, path: Path = OUTBOX_PATH, url: str = NEWS_LOG_URL) -> None:
        self.path, self.url = Path(path), url
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS outbox (record_id TEXT PRIMARY KEY, event TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def enqueue(self, request: Any, result: Any, status_code: int) -> str:
        record_id = str(uuid.uuid4())
        event = {
            "recordId": record_id,
            "timestamp": _timestamp(),
            "request": sanitize(request),
            "result": sanitize(result),
            "status_code": int(status_code),
        }
        encoded = json.dumps(event, ensure_ascii=False)
        if len(encoded.encode()) >= MAX_EVENT_BYTES:
            event["request"] = {"truncated": True}
            event["result"] = {"truncated": True}
            encoded = json.dumps(event, ensure_ascii=False)
        with self.lock, closing(self._connect()) as db, db:
            db.execute("INSERT INTO outbox(record_id,event,attempt) VALUES(?,?,0)", (record_id, encoded))
        return record_id

    def pending(self) -> tuple[str, str, int] | None:
        with closing(self._connect()) as db, db:
            return db.execute("SELECT record_id,event,attempt FROM outbox ORDER BY rowid LIMIT 1").fetchone()

    def _next_attempt(self, record_id: str) -> int:
        with self.lock, closing(self._connect()) as db, db:
            row = db.execute("SELECT attempt FROM outbox WHERE record_id=?", (record_id,)).fetchone()
            if row is None:
                return 0
            attempt = int(row[0])
            db.execute("UPDATE outbox SET attempt=? WHERE record_id=?", (attempt + 1, record_id))
            return attempt

    def send_pending(self) -> bool:
        row = self.pending()
        if row is None:
            return False
        record_id, event, _ = row
        attempt = self._next_attempt(record_id)
        token = _token()
        if not token:
            return False
        body = json.dumps({**json.loads(event), "attempt": attempt}, ensure_ascii=False).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                ok = 200 <= int(response.status) < 300
        except (OSError, urllib.error.URLError):
            ok = False
        if ok:
            with self.lock, closing(self._connect()) as db, db:
                db.execute("DELETE FROM outbox WHERE record_id=?", (record_id,))
        return ok

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                sent = self.send_pending()
            except Exception as exc:
                LOGGER.warning("news execution export failed: %s", type(exc).__name__)
                sent = False
            if not sent:
                stop.wait(5)


def get_outbox() -> NewsExecutionOutbox:
    global _outbox
    if _outbox is None:
        _outbox = NewsExecutionOutbox()
    return _outbox


def log_news_execution(request: Any, result: Any, status_code: int) -> None:
    try:
        get_outbox().enqueue(request, result, status_code)
    except Exception as exc:
        LOGGER.warning("news execution log failed: %s", type(exc).__name__)


def start_news_execution_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=get_outbox().run, name="news-execution-export", daemon=True)
        _worker.start()
