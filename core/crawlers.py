from __future__ import annotations

import atexit
import asyncio
import base64
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import nullcontext
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .bot_detection import crawl_result_has_block_signal, observe_crawl_exception, observe_crawl_result
from .connectors.watercrawl import WatercrawlApiClient
from .credentials import resolve_binary, secret_for_env
from .politeness import DEFAULT_USER_AGENT, PoliteRequestGate, PolitenessPolicy, fetch_robots_info, host_for
from .registry import available_crawlers
from .schema import CrawlResult
from .search import MissingCredentialError, SearchAgentError
from .usage import record_api_usage
from .utils import canonicalize_url, compact_text, detect_pii, extract_image_page_links, extract_snapshot_image_links, looks_js_heavy, soup_main_text, soup_title


ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = DEFAULT_USER_AGENT
STEALTH_BROWSER_REPO = ROOT / "external" / "stealth-browser"
log = logging.getLogger(__name__)
CGROUP_PID_CURRENT_FILES = (
    Path("/sys/fs/cgroup/pids.current"),
    Path("/sys/fs/cgroup/pids/pids.current"),
)


def _container_pid_threads() -> int | None:
    for path in CGROUP_PID_CURRENT_FILES:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
    return None


def _stealth_chrome_process_ids() -> list[int]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,comm=,args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command = parts
        if "chrome" not in command.lower() or "--user-data-dir=/tmp/uc_" not in command:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _chrome_process_count() -> int:
    return len(_stealth_chrome_process_ids())


def _terminate_stealth_chrome_processes() -> int:
    pids = _stealth_chrome_process_ids()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
        time.sleep(0.5)
        pids = _stealth_chrome_process_ids()
    return len(pids)


class _StealthAsyncRuntime:
    """Keep stealth-browser's shared async browser pool on one event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._call_lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._state_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            started = threading.Event()
            thread = threading.Thread(
                target=self._serve,
                args=(loop, started),
                name="stealth-browser-event-loop",
                daemon=True,
            )
            self._loop = loop
            self._thread = thread
            thread.start()
            if not started.wait(timeout=5):
                raise RuntimeError("stealth-browser event loop did not start")
            return loop

    @staticmethod
    def _serve(loop: asyncio.AbstractEventLoop, started: threading.Event) -> None:
        asyncio.set_event_loop(loop)
        started.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    def run(
        self,
        coroutine_factory: Callable[[], Any],
        *,
        parallel: bool = False,
        timeout_seconds: float | None = None,
    ) -> Any:
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        acquired = False
        if not parallel:
            # A cancelled nodriver task can otherwise hold this lock forever and
            # consume every coordinator slot behind it.
            if timeout_seconds is None:
                self._call_lock.acquire()
                acquired = True
            else:
                acquired = self._call_lock.acquire(timeout=timeout_seconds)
                if not acquired:
                    raise TimeoutError(
                        f"stealth-browser runtime remained busy for {timeout_seconds:g}s"
                    )
        try:
            loop = self._ensure_loop()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError(
                    f"stealth-browser runtime remained busy for {timeout_seconds:g}s"
                )
            future = asyncio.run_coroutine_threadsafe(coroutine_factory(), loop)
            try:
                return future.result(timeout=remaining)
            except FutureTimeoutError as exc:
                if future.done():
                    raise
                future.cancel()
                raise TimeoutError(
                    f"stealth-browser runtime timed out after {timeout_seconds:g}s"
                ) from exc
        finally:
            if acquired:
                self._call_lock.release()

    def shutdown(self, cleanup_factory: Callable[[], Any] | None = None) -> None:
        with self._call_lock:
            with self._state_lock:
                loop = self._loop
                thread = self._thread
            if loop is None or thread is None:
                return
            if cleanup_factory is not None and thread.is_alive():
                try:
                    asyncio.run_coroutine_threadsafe(cleanup_factory(), loop).result(timeout=10)
                except Exception:
                    pass
            if thread.is_alive():
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=5)
            if thread.is_alive():
                return
            with self._state_lock:
                if self._loop is loop:
                    self._loop = None
                    self._thread = None


_STEALTH_RUNTIME = _StealthAsyncRuntime()


class _StealthRequestCoordinator:
    def __init__(
        self,
        *,
        max_pending: int | None = None,
        queue_wait_seconds: float | None = None,
        recycle_after_requests: int | None = None,
        max_pid_threads: int | None = None,
        max_chrome_processes: int | None = None,
        cooldown_seconds: float | None = None,
        government_cooldown_seconds: float | None = None,
        block_cooldown_seconds: float | None = None,
        gate: PoliteRequestGate | None = None,
    ) -> None:
        self.max_pending = (
            self._int_env("STEALTH_BROWSER_MAX_PENDING", 16, 1, 128)
            if max_pending is None
            else max_pending
        )
        self.queue_wait_seconds = (
            self._float_env("STEALTH_BROWSER_QUEUE_WAIT_SECONDS", 10.0, 1.0, 600.0)
            if queue_wait_seconds is None
            else queue_wait_seconds
        )
        self.recycle_after_requests = (
            self._int_env("STEALTH_BROWSER_RECYCLE_AFTER_REQUESTS", 8, 0, 1000)
            if recycle_after_requests is None
            else recycle_after_requests
        )
        self.max_pid_threads = (
            self._int_env("STEALTH_BROWSER_MAX_PID_THREADS", 512, 0, 4096)
            if max_pid_threads is None
            else max_pid_threads
        )
        self.max_chrome_processes = (
            self._int_env("STEALTH_BROWSER_MAX_CHROME_PROCESSES", 64, 0, 512)
            if max_chrome_processes is None
            else max_chrome_processes
        )
        self.cooldown_seconds = (
            self._float_env("STEALTH_BROWSER_HOST_COOLDOWN_SECONDS", 10.0, 0.0, 300.0)
            if cooldown_seconds is None
            else cooldown_seconds
        )
        self.government_cooldown_seconds = (
            self._float_env("STEALTH_BROWSER_GOVERNMENT_COOLDOWN_SECONDS", 15.0, 0.0, 300.0)
            if government_cooldown_seconds is None
            else government_cooldown_seconds
        )
        self.block_cooldown_seconds = (
            self._float_env("STEALTH_BROWSER_BLOCK_COOLDOWN_SECONDS", 300.0, 30.0, 3600.0)
            if block_cooldown_seconds is None
            else block_cooldown_seconds
        )
        self._pending = threading.BoundedSemaphore(self.max_pending)
        self._active = 0
        self._completed_since_recycle = 0
        self._activity_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_locks_guard = threading.Lock()
        self._notice_lock = threading.Lock()
        self._proxy_notice_sent = False
        self._gate = gate or PoliteRequestGate(
            PolitenessPolicy(
                base_delay_seconds=self.cooldown_seconds,
                government_delay_seconds=self.government_cooldown_seconds,
                max_delay_seconds=max(self.block_cooldown_seconds, self.government_cooldown_seconds),
                jitter_min=1.0,
                jitter_max=1.0,
                cooldown_seconds=self.block_cooldown_seconds,
                respect_robots=False,
            )
        )

    @staticmethod
    def _int_env(name: str, default: int, lower: int, upper: int) -> int:
        try:
            value = int(os.getenv(name, str(default)).strip())
        except ValueError:
            value = default
        return max(lower, min(value, upper))

    @staticmethod
    def _float_env(name: str, default: float, lower: float, upper: float) -> float:
        try:
            value = float(os.getenv(name, str(default)).strip())
        except ValueError:
            value = default
        return max(lower, min(value, upper))

    def run(
        self,
        url: str,
        operation: Callable[[], CrawlResult],
        *,
        parallel: bool = False,
    ) -> CrawlResult:
        self._notify_if_proxy_configured()
        if not self._pending.acquire(timeout=self.queue_wait_seconds):
            record_api_usage(
                "crawler_stealth_browser",
                "queue_full",
                ok=False,
                result_count=0,
                metadata={"mode": "bounded_queue"},
            )
            raise SearchAgentError(
                f"stealth-browser queue is full after {self.queue_wait_seconds:g}s "
                f"(max pending: {self.max_pending})"
            )
        with self._maintenance_lock:
            with self._activity_lock:
                self._active += 1
        try:
            with nullcontext() if parallel else self._host_lock(url):
                self._gate.wait_before_request(url)
                try:
                    result = operation()
                except Exception as exc:
                    self._gate.record_error(url, str(exc))
                    raise
                blocked = crawl_result_has_block_signal(result)
                self._gate.record_response(
                    url,
                    429 if blocked else result.status_code,
                    {"Retry-After": str(int(round(self.block_cooldown_seconds)))} if blocked else None,
                )
                result.raw_payload["politeness"] = self._gate.latest_event_for(url)
                return result
        finally:
            reason, stats = self._finish_request()
            self._pending.release()
            if reason:
                self._run_maintenance(reason, stats)

    def _finish_request(self) -> tuple[str, dict[str, int | None]]:
        with self._activity_lock:
            self._active = max(0, self._active - 1)
            self._completed_since_recycle += 1
            if self._active:
                return "", {}
            pid_threads = _container_pid_threads()
            chrome_processes = _chrome_process_count()
            if self.max_pid_threads and pid_threads is not None and pid_threads >= self.max_pid_threads:
                return "pid_thread_budget_exceeded", {
                    "pid_threads": pid_threads,
                    "max_pid_threads": self.max_pid_threads,
                    "chrome_processes": chrome_processes,
                }
            if self.max_chrome_processes and chrome_processes >= self.max_chrome_processes:
                return "chrome_process_budget_exceeded", {
                    "pid_threads": pid_threads,
                    "chrome_processes": chrome_processes,
                    "max_chrome_processes": self.max_chrome_processes,
                }
            if self.recycle_after_requests and self._completed_since_recycle >= self.recycle_after_requests:
                return "request_budget_reached", {
                    "completed_since_recycle": self._completed_since_recycle,
                    "pid_threads": pid_threads,
                    "chrome_processes": chrome_processes,
                }
        return "", {}

    def _run_maintenance(self, reason: str, stats: dict[str, int | None]) -> None:
        with self._maintenance_lock:
            with self._activity_lock:
                if self._active:
                    return
                self._completed_since_recycle = 0
            shutdown_error = ""
            try:
                shutdown_stealth_browser_runtime()
            except Exception as exc:
                shutdown_error = f"{type(exc).__name__}: {exc}"
            remaining_chrome = _chrome_process_count()
            if remaining_chrome:
                remaining_chrome = _terminate_stealth_chrome_processes()
            record_api_usage(
                "crawler_stealth_browser",
                "pid_recycle",
                ok=not shutdown_error and remaining_chrome == 0,
                result_count=0,
                metadata={
                    "reason": reason,
                    "shutdown_error": shutdown_error or None,
                    "remaining_chrome_processes": remaining_chrome,
                    **stats,
                },
            )

    def _host_lock(self, url: str) -> threading.Lock:
        host = host_for(url)
        with self._host_locks_guard:
            return self._host_locks.setdefault(host, threading.Lock())

    def _notify_if_proxy_configured(self) -> None:
        proxy_configured = bool(os.getenv("RESIDENTIAL_PROXIES_JSON", "").strip()) or (
            os.getenv("PROXIES_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
            and bool(os.getenv("PROXIES_JSON", "").strip())
        )
        if not proxy_configured or self._proxy_notice_sent:
            return
        with self._notice_lock:
            if self._proxy_notice_sent:
                return
            self._proxy_notice_sent = True
            log.warning(
                "stealth-browser proxy is configured; review cooldown settings before reducing them "
                "(host=%ss, government=%ss, block=%ss)",
                self.cooldown_seconds,
                self.government_cooldown_seconds,
                self.block_cooldown_seconds,
            )
            record_api_usage(
                "crawler_stealth_browser",
                "proxy_cooldown_review_required",
                ok=True,
                result_count=0,
                metadata={"mode": "proxy_configured"},
            )


_STEALTH_COORDINATOR = _StealthRequestCoordinator()


async def _stop_stealth_browser_pool() -> None:
    try:
        from stealth_browser.browser import pool
    except ImportError:
        return
    await pool.stop()


def shutdown_stealth_browser_runtime() -> None:
    _STEALTH_RUNTIME.shutdown(_stop_stealth_browser_pool)


atexit.register(shutdown_stealth_browser_runtime)


class Crawler(Protocol):
    name: str

    def crawl(self, url: str) -> CrawlResult:
        ...


class ObservedCrawler:
    def __init__(self, crawler: Crawler) -> None:
        self._crawler = crawler
        self.name = crawler.name

    def crawl(self, url: str) -> CrawlResult:
        try:
            result = self._crawler.crawl(url)
        except Exception as exc:
            observe_crawl_exception(url, requested_crawler=self.name, error=exc)
            raise
        observe_crawl_result(result, requested_crawler=self.name)
        return result

    def crawl_with_actions(self, url: str, actions: list[dict[str, Any]]) -> CrawlResult:
        crawl_with_actions = getattr(self._crawler, "crawl_with_actions", None)
        if not callable(crawl_with_actions):
            return self.crawl(url)
        try:
            result = crawl_with_actions(url, actions)
        except Exception as exc:
            observe_crawl_exception(url, requested_crawler=self.name, error=exc)
            raise
        observe_crawl_result(result, requested_crawler=self.name)
        return result


class RobotsDeniedError(SearchAgentError):
    pass


def robots_allowed(url: str, *, user_agent: str = USER_AGENT, timeout: int = 10) -> bool:
    return fetch_robots_info(url, user_agent=user_agent, timeout=timeout).allowed


def make_polite_gate(
    *,
    respect_robots: bool = True,
    base_delay_seconds: float = 2.0,
    government_delay_seconds: float = 10.0,
    polite_gate: PoliteRequestGate | None = None,
) -> PoliteRequestGate:
    if polite_gate is not None:
        return polite_gate
    return PoliteRequestGate(
        PolitenessPolicy(
            respect_robots=respect_robots,
            base_delay_seconds=base_delay_seconds,
            government_delay_seconds=government_delay_seconds,
        )
    )


def ensure_public_allowed(url: str, polite_gate: PoliteRequestGate) -> None:
    robots = polite_gate.assert_allowed(url)
    if not robots.allowed:
        raise RobotsDeniedError(robots.error or f"robots.txt disallows crawling: {url}")


def ensure_public_url_shape(url: str) -> None:
    lowered = url.lower()
    if any(token in lowered for token in ("captcha", "login", "signin", "account/login")):
        raise RobotsDeniedError("refusing likely login/captcha URL")


class StaticSoupCrawler:
    name = "static"

    def __init__(
        self,
        *,
        timeout: int = 20,
        respect_robots: bool = True,
        polite_gate: PoliteRequestGate | None = None,
    ) -> None:
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.polite_gate = make_polite_gate(respect_robots=respect_robots, polite_gate=polite_gate)

    def crawl(self, url: str) -> CrawlResult:
        ensure_public_allowed(url, self.polite_gate)
        self.polite_gate.wait_before_request(url)
        headers = {"User-Agent": self.polite_gate.user_agent, "Accept": "text/html,application/xhtml+xml"}
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
                response = client.get(url)
                response.raise_for_status()
        except Exception as exc:
            self.polite_gate.record_error(url, str(exc))
            return CrawlResult.build(url=url, crawler=self.name, error=str(exc))
        self.polite_gate.record_response(url, response.status_code, response.headers)

        html_text = response.text
        soup = BeautifulSoup(html_text, "html.parser")
        title = soup_title(soup, fallback=canonicalize_url(str(response.url)))
        text = soup_main_text(soup)
        return CrawlResult.build(
            url=str(response.url),
            crawler=self.name,
            title=title,
            text=text,
            status_code=response.status_code,
            html=html_text,
            raw_payload={
                "content_type": response.headers.get("content-type"),
                "html_length": len(html_text),
                "js_heavy_hint": looks_js_heavy(html_text, text),
                "politeness": self.polite_gate.latest_event_for(url),
            },
        )


class PlaywrightCrawler:
    name = "playwright"

    def __init__(
        self,
        *,
        browser: str = "chromium",
        timeout_ms: int = 30000,
        wait_until: str = "networkidle",
        respect_robots: bool = True,
        screenshot_path: str | Path | None = None,
        polite_gate: PoliteRequestGate | None = None,
    ) -> None:
        self.browser = browser
        self.timeout_ms = timeout_ms
        self.wait_until = wait_until
        self.respect_robots = respect_robots
        self.screenshot_path = Path(screenshot_path) if screenshot_path else None
        self.polite_gate = make_polite_gate(
            respect_robots=respect_robots,
            base_delay_seconds=5.0,
            polite_gate=polite_gate,
        )

    def crawl(self, url: str) -> CrawlResult:
        ensure_public_url_shape(url)
        ensure_public_allowed(url, self.polite_gate)
        self.polite_gate.wait_before_request(url)
        try:
            return asyncio.run(self._crawl(url))
        except Exception as exc:
            self.polite_gate.record_error(url, str(exc))
            return CrawlResult.build(url=url, crawler=self.name, error=str(exc))

    async def _crawl(self, url: str) -> CrawlResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SearchAgentError("playwright package is missing; install with `uv pip install -e '.[playwright]'`") from exc

        async with async_playwright() as playwright:
            browser_type = getattr(playwright, self.browser, None)
            if browser_type is None:
                raise ValueError(f"unsupported Playwright browser: {self.browser}")
            browser = await browser_type.launch(headless=True)
            context = await browser.new_context(user_agent=self.polite_gate.user_agent, viewport={"width": 1440, "height": 1600})
            await self._prepare_context(context)
            page = await context.new_page()
            response = await page.goto(url, wait_until=self.wait_until, timeout=self.timeout_ms)
            await page.wait_for_timeout(750)
            html_text = await page.content()
            title = compact_text(await page.title())
            screenshot = None
            if self.screenshot_path:
                self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(self.screenshot_path), full_page=True)
                screenshot = str(self.screenshot_path)
            final_url = page.url
            status_code = response.status if response else None
            response_headers = await response.all_headers() if response else {}
            await browser.close()
        self.polite_gate.record_response(url, status_code, response_headers)

        soup = BeautifulSoup(html_text, "html.parser")
        text = soup_main_text(soup)
        return CrawlResult.build(
            url=final_url,
            crawler=self.name,
            title=title or soup_title(soup, fallback=canonicalize_url(final_url)),
            text=text,
            status_code=status_code,
            html=html_text,
            screenshot_path=screenshot,
            raw_payload={
                "browser": self.browser,
                "stealth": self.name == "playwright_stealth",
                "html_length": len(html_text),
                "politeness": self.polite_gate.latest_event_for(url),
            },
        )

    async def _prepare_context(self, context: Any) -> None:
        del context


class PlaywrightStealthCrawler(PlaywrightCrawler):
    name = "playwright_stealth"

    async def _prepare_context(self, context: Any) -> None:
        try:
            from playwright_stealth import Stealth
        except ImportError as exc:
            raise SearchAgentError(
                "playwright-stealth package is missing; install with `uv pip install -e '.[playwright]'`"
            ) from exc
        await Stealth().apply_stealth_async(context)


class SeleniumCrawler:
    name = "selenium"

    def __init__(
        self,
        *,
        timeout: int = 30,
        respect_robots: bool = True,
        polite_gate: PoliteRequestGate | None = None,
    ) -> None:
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.polite_gate = make_polite_gate(
            respect_robots=respect_robots,
            base_delay_seconds=5.0,
            polite_gate=polite_gate,
        )

    def crawl(self, url: str) -> CrawlResult:
        ensure_public_allowed(url, self.polite_gate)
        self.polite_gate.wait_before_request(url)
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:
            self.polite_gate.record_error(url, str(exc))
            return CrawlResult.build(
                url=url,
                crawler=self.name,
                error=f"selenium package is missing: {exc}",
            )

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1440,2200")
        options.add_argument(f"--user-agent={self.polite_gate.user_agent}")
        driver = None
        try:
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(self.timeout)
            driver.get(url)
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                if driver.execute_script("return document.readyState") == "complete":
                    break
                time.sleep(0.25)
            time.sleep(1.0)
            html_text = driver.page_source
            final_url = driver.current_url
            title = driver.title
        except Exception as exc:
            self.polite_gate.record_error(url, str(exc))
            return CrawlResult.build(url=url, crawler=self.name, error=str(exc))
        finally:
            if driver is not None:
                driver.quit()
        self.polite_gate.record_response(url, None, None)

        soup = BeautifulSoup(html_text, "html.parser")
        return CrawlResult.build(
            url=final_url,
            crawler=self.name,
            title=title or soup_title(soup, fallback=canonicalize_url(final_url)),
            text=soup_main_text(soup),
            html=html_text,
            raw_payload={"html_length": len(html_text), "politeness": self.polite_gate.latest_event_for(url)},
        )


class FirecrawlCrawler:
    name = "firecrawl"

    def __init__(
        self,
        *,
        timeout: int = 180,
        respect_robots: bool = True,
        polite_gate: PoliteRequestGate | None = None,
    ) -> None:
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.api_key = secret_for_env("FIRECRAWL_API_KEY")
        self.binary = resolve_binary("firecrawl")

    def crawl(self, url: str) -> CrawlResult:
        ensure_public_url_shape(url)
        if not self.api_key:
            raise MissingCredentialError("FIRECRAWL_API_KEY is missing")
        if not self.binary:
            raise SearchAgentError("firecrawl CLI is missing")
        env = os.environ.copy()
        env["FIRECRAWL_API_KEY"] = self.api_key
        with tempfile.TemporaryDirectory(prefix="web-search-agent-firecrawl-") as tmp:
            output_path = Path(tmp) / "scrape.md"
            cmd = [
                self.binary,
                "scrape",
                url,
                "--format",
                "markdown",
                "--only-main-content",
                "-o",
                str(output_path),
            ]
            try:
                result = subprocess.run(
                    cmd,
                    cwd=str(ROOT),
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=self.timeout,
                )
            except Exception as exc:
                record_api_usage(
                    "firecrawl",
                    "crawl",
                    ok=False,
                    metadata={"error_type": type(exc).__name__, "managed_service": "firecrawl"},
                )
                raise
            if result.returncode != 0:
                record_api_usage(
                    "firecrawl",
                    "crawl",
                    ok=False,
                    metadata={"returncode": result.returncode, "managed_service": "firecrawl"},
                )
                return CrawlResult.build(
                    url=url,
                    crawler=self.name,
                    error=result.stderr.strip() or result.stdout.strip() or f"returncode={result.returncode}",
                    raw_payload={
                        "command": ["firecrawl", "scrape", url, "--format", "markdown", "--only-main-content"],
                        "managed_service": "firecrawl",
                        "target_politeness": "delegated_to_firecrawl",
                    },
            )
            markdown = output_path.read_text(encoding="utf-8", errors="ignore") if output_path.exists() else ""

        record_api_usage(
            "firecrawl",
            "crawl",
            ok=True,
            result_count=1,
            metadata={"managed_service": "firecrawl"},
        )
        title = ""
        for line in markdown.splitlines():
            stripped = line.strip("# ").strip()
            if stripped:
                title = stripped
                break
        return CrawlResult.build(
            url=url,
            crawler=self.name,
            title=title or canonicalize_url(url),
            text=markdown,
            markdown=markdown,
            raw_payload={
                "format": "markdown",
                "markdown_length": len(markdown),
                "managed_service": "firecrawl",
                "target_politeness": "delegated_to_firecrawl",
            },
        )


class ApifyLinkedInCrawler:
    name = "apify_linkedin"
    api_base = "https://api.apify.com/v2"
    default_actor_id = "harvestapi/linkedin-profile-scraper"
    default_company_actor_id = "harvestapi/linkedin-company"
    allowed_path_prefixes = ("/in/", "/company/", "/school/", "/showcase/")
    blocked_path_tokens = (
        "/login",
        "/checkpoint",
        "/authwall",
        "/uas/login",
        "/feed",
        "/messaging",
        "/sales",
        "/jobs",
        "/talent",
    )
    sensitive_key_tokens = (
        "email",
        "phone",
        "mobile",
        "telephone",
        "contact",
        "address",
        "street",
        "postal",
        "zip",
        "birthday",
        "birthdate",
        "candm",
        "can_dm",
        "message",
    )

    def __init__(self, *, timeout: int | None = None, max_items: int | None = None) -> None:
        self.api_token = secret_for_env("APIFY_API_TOKEN")
        self.actor_id = secret_for_env("APIFY_LINKEDIN_ACTOR_ID") or self.default_actor_id
        self.company_actor_id = (
            secret_for_env("APIFY_LINKEDIN_COMPANY_ACTOR_ID") or self.default_company_actor_id
        )
        self.input_template = secret_for_env("APIFY_LINKEDIN_INPUT_TEMPLATE")
        self.company_input_template = secret_for_env("APIFY_LINKEDIN_COMPANY_INPUT_TEMPLATE")
        self.timeout = timeout or self._int_env("APIFY_LINKEDIN_TIMEOUT_SECONDS", 180, lower=30, upper=300)
        self.max_items = max_items or self._int_env("APIFY_LINKEDIN_MAX_ITEMS", 1, lower=1, upper=10)
        self.max_total_charge_usd = self._float_env("APIFY_LINKEDIN_MAX_TOTAL_CHARGE_USD", 0.5, lower=0.0, upper=10.0)

    @staticmethod
    def _int_env(name: str, default: int, *, lower: int, upper: int) -> int:
        try:
            value = int(os.getenv(name, str(default)).strip())
        except ValueError:
            value = default
        return max(lower, min(value, upper))

    @staticmethod
    def _float_env(name: str, default: float, *, lower: float, upper: float) -> float:
        try:
            value = float(os.getenv(name, str(default)).strip())
        except ValueError:
            value = default
        return max(lower, min(value, upper))

    @staticmethod
    def _actor_path_id(actor_id: str) -> str:
        normalized = actor_id.strip()
        if "/" in normalized and "~" not in normalized:
            return normalized.replace("/", "~")
        return normalized

    def _validate_linkedin_url(self, url: str) -> str:
        candidate = (url or "").strip()
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "/").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            raise RobotsDeniedError("apify_linkedin only accepts explicit http/https LinkedIn URLs")
        if host != "linkedin.com" and not host.endswith(".linkedin.com") and host != "linkedin.cn" and not host.endswith(".linkedin.cn"):
            raise RobotsDeniedError("apify_linkedin only accepts LinkedIn URLs")
        if any(token in path for token in self.blocked_path_tokens):
            raise RobotsDeniedError("refusing LinkedIn login/feed/jobs/sales/private URL")
        if not any(path.startswith(prefix) for prefix in self.allowed_path_prefixes):
            raise RobotsDeniedError("apify_linkedin only accepts public /in/, /company/, /school/, or /showcase/ URLs")
        return candidate

    def _replace_url_placeholders(self, value: Any, url: str) -> Any:
        if isinstance(value, str):
            return value.replace("{url}", url)
        if isinstance(value, list):
            return [self._replace_url_placeholders(item, url) for item in value]
        if isinstance(value, dict):
            return {key: self._replace_url_placeholders(item, url) for key, item in value.items()}
        return value

    def _actor_id_for_url(self, url: str) -> str:
        if urlparse(url).path.lower().startswith("/company/"):
            return getattr(self, "company_actor_id", self.default_company_actor_id)
        return getattr(self, "actor_id", self.default_actor_id)

    def _build_input(self, url: str, *, actor_id: str | None = None) -> dict[str, Any]:
        actor_id = actor_id or self._actor_id_for_url(url)
        is_company = urlparse(url).path.lower().startswith("/company/")
        input_template = (
            getattr(self, "company_input_template", None) or self.input_template
            if is_company
            else self.input_template
        )
        if input_template:
            try:
                parsed = json.loads(input_template)
            except json.JSONDecodeError as exc:
                raise SearchAgentError("Apify LinkedIn input template must be valid JSON") from exc
            if not isinstance(parsed, dict):
                raise SearchAgentError("Apify LinkedIn input template must be a JSON object")
            return self._replace_url_placeholders(parsed, url)
        if self._actor_path_id(actor_id) == self._actor_path_id(self.default_company_actor_id) and is_company:
            return {"companies": [url]}
        if "linkedin-profile-scraper" in actor_id and urlparse(url).path.lower().startswith("/in/"):
            return {
                "profileScraperMode": "Profile details no email ($4 per 1k)",
                "urls": [url],
            }
        return {
            "startUrls": [{"url": url}],
            "profileUrls": [url],
            "companyUrls": [url],
            "urls": [url],
            "url": url,
        }

    def _is_sensitive_key(self, key: str) -> bool:
        lowered = "".join(ch for ch in key.lower() if ch.isalnum() or ch == "_")
        return any(token in lowered for token in self.sensitive_key_tokens)

    def _sanitize_value(self, key: str, value: Any, *, depth: int = 0) -> Any:
        if self._is_sensitive_key(key):
            return None
        if isinstance(value, str):
            clean = compact_text(value)
            if "url" in key.lower() and clean.startswith(("http://", "https://")):
                return clean[:2000]
            flags = detect_pii(clean)
            if flags:
                return f"[redacted:{','.join(flags)}]"
            return clean[:2000]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, list):
            if depth >= 2:
                return f"[list:{len(value)}]"
            safe = [self._sanitize_value(key, item, depth=depth + 1) for item in value[:8]]
            return [item for item in safe if item not in (None, "", [], {})]
        if isinstance(value, dict):
            if depth >= 2:
                return f"[object:{len(value)}]"
            clean: dict[str, Any] = {}
            for item_key, item_value in value.items():
                safe_value = self._sanitize_value(str(item_key), item_value, depth=depth + 1)
                if safe_value not in (None, "", [], {}):
                    clean[str(item_key)] = safe_value
            return clean
        return str(value)[:500]

    def _sanitize_item(self, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            safe_value = self._sanitize_value("value", item)
            return {"value": safe_value} if safe_value not in (None, "", [], {}) else {}
        clean: dict[str, Any] = {}
        for key, value in item.items():
            safe_value = self._sanitize_value(str(key), value)
            if safe_value not in (None, "", [], {}):
                clean[str(key)] = safe_value
        return clean

    def _safe_items(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload["items"]
        elif isinstance(payload, dict):
            items = [payload]
        else:
            items = []
        return [item for item in (self._sanitize_item(raw) for raw in items[: self.max_items]) if item]

    @staticmethod
    def _first_string(item: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _title_from_items(self, items: list[dict[str, Any]], fallback: str) -> str:
        if not items:
            return fallback
        first = items[0]
        title = self._first_string(first, ("name", "fullName", "title"))
        if not title:
            title = " ".join(
                part for part in (
                    self._first_string(first, ("firstName",)),
                    self._first_string(first, ("lastName",)),
                ) if part
            )
        title = title or self._first_string(first, ("headline", "companyName"))
        handle = self._first_string(first, ("username", "publicIdentifier", "linkedinSlug"))
        if title and handle and handle not in title:
            return f"{title} ({handle})"
        return title or fallback

    def _text_from_items(self, items: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        preferred_keys = (
            "name",
            "fullName",
            "firstName",
            "lastName",
            "title",
            "headline",
            "occupation",
            "currentPosition",
            "companyName",
            "company",
            "industry",
            "location",
            "summary",
            "about",
            "description",
            "experience",
            "education",
            "skills",
            "certifications",
            "projects",
            "publications",
            "honorsAndAwards",
            "followers",
            "followersCount",
            "employees",
            "employeeCount",
            "url",
            "linkedinUrl",
            "profileUrl",
            "companyUrl",
            "website",
        )
        for index, item in enumerate(items, start=1):
            lines.append(f"item {index}")
            for key in preferred_keys:
                value = item.get(key)
                if value in (None, "", [], {}):
                    continue
                lines.append(f"{key}: {json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value}")
            extras = sorted(set(item) - set(preferred_keys))
            if extras:
                lines.append(f"other_fields: {', '.join(extras[:30])}")
        return "\n".join(lines)

    @staticmethod
    def _error_message(response: httpx.Response | None, fallback: str) -> str:
        if response is None:
            return fallback
        try:
            payload = response.json()
        except Exception:
            return compact_text(response.text)[:1200] or fallback
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("type")
                if message:
                    return str(message)
            message = payload.get("message")
            if message:
                return str(message)
        return compact_text(response.text)[:1200] or fallback

    def crawl(self, url: str) -> CrawlResult:
        target_url = self._validate_linkedin_url(url)
        if not self.api_token:
            raise MissingCredentialError("APIFY_API_TOKEN is missing")
        actor_id = self._actor_id_for_url(target_url)
        if not actor_id:
            raise MissingCredentialError("Apify LinkedIn Actor ID is missing")

        actor_path = self._actor_path_id(actor_id)
        endpoint = f"{self.api_base}/actors/{actor_path}/run-sync-get-dataset-items"
        params: dict[str, object] = {
            "format": "json",
            "clean": "true",
            "limit": self.max_items,
            "maxItems": self.max_items,
        }
        if self.max_total_charge_usd:
            params["maxTotalChargeUsd"] = self.max_total_charge_usd
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        response: httpx.Response | None = None
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
                response = client.post(
                    endpoint,
                    params=params,
                    json=self._build_input(target_url, actor_id=actor_id),
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            record_api_usage(
                "apify_linkedin",
                "crawl",
                ok=False,
                status_code=response.status_code if response is not None else None,
                response_headers=response.headers if response is not None else None,
                metadata={"error_type": type(exc).__name__, "managed_service": "apify"},
            )
            return CrawlResult.build(
                url=target_url,
                crawler=self.name,
                status_code=response.status_code if response is not None else None,
                error=f"Apify LinkedIn actor failed: {self._error_message(response, str(exc))}",
                raw_payload={
                    "managed_service": "apify",
                    "actor_id": actor_id,
                    "target_politeness": "delegated_to_apify_actor",
                },
            )

        items = self._safe_items(payload)
        record_api_usage(
            "apify_linkedin",
            "crawl",
            ok=True,
            status_code=response.status_code,
            response_headers=response.headers,
            result_count=len(items),
            metadata={"managed_service": "apify"},
        )
        text = self._text_from_items(items)
        title = self._title_from_items(items, canonicalize_url(target_url))
        return CrawlResult.build(
            url=target_url,
            crawler=self.name,
            title=title,
            text=text,
            status_code=response.status_code,
            markdown=text,
            raw_payload={
                "managed_service": "apify",
                "actor_id": actor_id,
                "item_count": len(items),
                "sample_keys": sorted(items[0].keys()) if items else [],
                "profile": items[0] if items else {},
                "sample_items": items[:3],
                "target_politeness": "delegated_to_apify_actor",
                "redaction": "contact-like fields and detected email/phone values removed",
            },
        )


class WatercrawlCrawler:
    name = "watercrawl"

    def __init__(
        self,
        *,
        client: WatercrawlApiClient | None = None,
        max_depth: int | None = None,
        page_limit: int | None = None,
        include_html: bool | None = None,
    ) -> None:
        self.client = client or WatercrawlApiClient()
        self.max_depth = max_depth if max_depth is not None else self._int_env("WATERCRAWL_CRAWL_MAX_DEPTH", 0, lower=0, upper=5)
        self.page_limit = page_limit if page_limit is not None else self._int_env("WATERCRAWL_CRAWL_PAGE_LIMIT", 1, lower=1, upper=100)
        self.include_html = include_html if include_html is not None else self._bool_env("WATERCRAWL_INCLUDE_HTML", True)

    @staticmethod
    def _int_env(name: str, default: int, *, lower: int, upper: int) -> int:
        try:
            value = int(os.getenv(name, str(default)).strip())
        except ValueError:
            value = default
        return max(lower, min(value, upper))

    @staticmethod
    def _bool_env(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() not in {"0", "false", "no", "off"}

    @staticmethod
    def _result_content(item: dict[str, Any]) -> dict[str, Any]:
        result = item.get("result")
        if isinstance(result, dict):
            return result
        return item

    @staticmethod
    def _metadata_title(metadata: Any, fallback: str) -> str:
        if not isinstance(metadata, dict):
            return fallback
        for key in ("title", "og:title", "twitter:title"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return fallback

    @staticmethod
    def _text_from_content(content: dict[str, Any]) -> str:
        markdown = content.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown
        html_text = content.get("html")
        if isinstance(html_text, str) and html_text.strip():
            return soup_main_text(BeautifulSoup(html_text, "html.parser"))
        extraction = content.get("extraction")
        if extraction not in (None, "", [], {}):
            return compact_text(json.dumps(extraction, ensure_ascii=False))
        return compact_text(json.dumps(content, ensure_ascii=False))

    def crawl(self, url: str) -> CrawlResult:
        ensure_public_url_shape(url)
        create_response: httpx.Response | None = None
        final_response: httpx.Response | None = None
        try:
            created, create_response = self.client.create_crawl(
                url,
                max_depth=self.max_depth,
                page_limit=self.page_limit,
                include_html=self.include_html,
            )
            request_uuid = str(created.get("uuid") or "")
            if not request_uuid:
                raise SearchAgentError("WaterCrawl crawl create response did not include uuid")
            finished, final_response = self.client.poll_request("/api/v1/core/crawl-requests", request_uuid)
            status = str(finished.get("status") or "").lower()
            if status != "finished":
                raise SearchAgentError(f"WaterCrawl crawl ended with status={status}")
            items, results_response = self.client.crawl_results(request_uuid)
            final_response = results_response
        except Exception as exc:
            response = final_response or create_response
            record_api_usage(
                "watercrawl",
                "crawl",
                ok=False,
                status_code=response.status_code if response is not None else None,
                response_headers=response.headers if response is not None else None,
                metadata={"managed_service": "watercrawl", "error_type": type(exc).__name__},
            )
            return CrawlResult.build(
                url=url,
                crawler=self.name,
                status_code=response.status_code if response is not None else None,
                error=f"WaterCrawl failed: {exc}",
                raw_payload={
                    "managed_service": "watercrawl",
                    "target_politeness": "delegated_to_watercrawl",
                },
            )

        record_api_usage(
            "watercrawl",
            "crawl",
            ok=True,
            status_code=final_response.status_code if final_response is not None else None,
            response_headers=final_response.headers if final_response is not None else None,
            result_count=len(items),
            metadata={"managed_service": "watercrawl", "item_count": len(items)},
        )
        if not items:
            return CrawlResult.build(
                url=url,
                crawler=self.name,
                error="WaterCrawl finished but returned no crawl results",
                raw_payload={"managed_service": "watercrawl", "item_count": 0},
            )
        primary = items[0]
        content = self._result_content(primary)
        final_url = str(primary.get("url") or url)
        metadata = content.get("metadata")
        text = self._text_from_content(content)
        html_text = content.get("html") if isinstance(content.get("html"), str) else None
        markdown = content.get("markdown") if isinstance(content.get("markdown"), str) else text
        return CrawlResult.build(
            url=final_url,
            crawler=self.name,
            title=self._metadata_title(metadata, canonicalize_url(final_url)),
            text=text,
            status_code=final_response.status_code if final_response is not None else None,
            markdown=markdown,
            html=html_text,
            raw_payload={
                "managed_service": "watercrawl",
                "request_uuid": str(primary.get("request") or ""),
                "item_count": len(items),
                "sample_urls": [str(item.get("url") or "") for item in items[:5]],
                "target_politeness": "delegated_to_watercrawl",
            },
        )


class StealthBrowserCrawler:
    name = "stealth_browser"

    def __init__(
        self,
        *,
        respect_robots: bool = True,
        screenshot_path: str | Path | None = None,
        polite_gate: PoliteRequestGate | None = None,
        parallel: bool = False,
        max_latency_seconds: float | None = None,
    ) -> None:
        self.respect_robots = respect_robots
        self.screenshot_path = Path(screenshot_path) if screenshot_path else None
        self.parallel = parallel
        self.max_latency_seconds = max_latency_seconds
        self.polite_gate = make_polite_gate(
            respect_robots=respect_robots,
            base_delay_seconds=5.0,
            polite_gate=polite_gate,
        )

    @staticmethod
    def _ensure_import_path() -> None:
        if STEALTH_BROWSER_REPO.exists():
            repo_path = str(STEALTH_BROWSER_REPO)
            if repo_path not in sys.path:
                sys.path.insert(0, repo_path)

    @staticmethod
    def _bool_env(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() not in {"0", "false", "no", "off"}

    @staticmethod
    def _float_env(name: str, default: float, *, lower: float, upper: float) -> float:
        try:
            value = float(os.getenv(name, str(default)).strip())
        except ValueError:
            value = default
        return max(lower, min(value, upper))

    def crawl(self, url: str) -> CrawlResult:
        return self.crawl_with_actions(url, [])

    def crawl_with_actions(self, url: str, actions: list[dict[str, Any]]) -> CrawlResult:
        ensure_public_url_shape(url)
        ensure_public_allowed(url, self.polite_gate)
        max_latency = (
            self.max_latency_seconds
            if self.max_latency_seconds is not None
            else self._float_env("STEALTH_BROWSER_MAX_LATENCY_SECONDS", 30.0, lower=5.0, upper=120.0)
        )
        runtime_timeout = self._float_env(
            "STEALTH_BROWSER_RUNTIME_TIMEOUT_SECONDS",
            max_latency + 15.0,
            lower=10.0,
            upper=180.0,
        )
        try:
            return _STEALTH_COORDINATOR.run(
                url,
                lambda: _STEALTH_RUNTIME.run(
                    lambda: self._crawl(url, actions=actions),
                    parallel=self.parallel,
                    timeout_seconds=runtime_timeout,
                ),
                parallel=self.parallel,
            )
        except Exception as exc:
            return CrawlResult.build(url=url, crawler=self.name, error=str(exc))

    async def _crawl(self, url: str, *, actions: list[dict[str, Any]] | None = None) -> CrawlResult:
        self._ensure_import_path()
        try:
            from stealth_browser.engines import Requirements, router
        except ImportError as exc:
            raise SearchAgentError(
                "stealth-browser package is missing; install `stealth-browser` or keep external/stealth-browser cloned"
            ) from exc

        requirements = Requirements(
            needs_js=self._bool_env("STEALTH_BROWSER_NEEDS_JS", True),
            needs_screenshot=self._bool_env("STEALTH_BROWSER_NEEDS_SCREENSHOT", bool(self.screenshot_path)),
            needs_dom=True,
            needs_interaction=bool(actions),
            vendor_hint=os.getenv("STEALTH_BROWSER_VENDOR_HINT") or None,
            max_latency_s=self.max_latency_seconds
            if self.max_latency_seconds is not None
            else self._float_env("STEALTH_BROWSER_MAX_LATENCY_SECONDS", 30.0, lower=5.0, upper=120.0),
            prefer_lightweight=self._bool_env("STEALTH_BROWSER_PREFER_LIGHTWEIGHT", False),
            require_tls_impersonation=self._bool_env("STEALTH_BROWSER_REQUIRE_TLS_IMPERSONATION", False),
            actions=actions or [],
        )
        snap, decision = await router.snapshot(url, requirements=requirements)
        screenshot = None
        if self.screenshot_path and snap.screenshot_base64:
            self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            self.screenshot_path.write_bytes(base64.b64decode(snap.screenshot_base64))
            screenshot = str(self.screenshot_path)

        html_text = self._html_from_snapshot(snap.elements)
        text = self._text_from_snapshot(snap.elements, html_text)
        image_urls = extract_image_page_links(html_text, base_url=snap.url)
        image_urls.extend(url for url in extract_snapshot_image_links(snap.elements, snap.url) if url not in image_urls)
        table_rows = self._table_rows_from_snapshot(snap.elements) if actions else []
        return CrawlResult.build(
            url=snap.url,
            crawler=self.name,
            title=snap.title or canonicalize_url(snap.url),
            text=text,
            html=html_text,
            screenshot_path=screenshot,
            image_urls=image_urls,
            raw_payload={
                "engine_name": snap.engine_name,
                "elapsed_s": snap.elapsed_s,
                "cost_cents": snap.cost_cents,
                "candidate_engines": decision.candidate_engines,
                "decision_reason": decision.reason,
                "escalation_path": decision.escalation_path,
                "element_count": len(snap.elements or []),
                "table_rows": table_rows,
                "notes": snap.notes,
            },
        )

    @staticmethod
    def _html_from_snapshot(elements: list[dict[str, Any]]) -> str | None:
        for item in elements or []:
            if item.get("tag") == "html":
                text = item.get("text")
                attrs = item.get("attrs")
                if isinstance(text, str) and isinstance(attrs, dict) and attrs.get("x-engine") == "curl_cffi":
                    return text
        return None

    @staticmethod
    def _text_from_snapshot(elements: list[dict[str, Any]], html_text: str | None) -> str:
        if html_text:
            return soup_main_text(BeautifulSoup(html_text, "html.parser"))
        lines: list[str] = []
        seen: set[str] = set()
        for item in elements or []:
            text = compact_text(str(item.get("text") or ""))
            if not text or text in seen:
                continue
            seen.add(text)
            lines.append(text)
            if len("\n".join(lines)) > 200_000:
                break
        return "\n".join(lines)

    @staticmethod
    def _table_rows_from_snapshot(elements: list[dict[str, Any]]) -> list[list[str]]:
        rows: dict[str, list[str]] = {}
        for item in elements or []:
            if item.get("tag") != "td":
                continue
            xpath = str(item.get("xpath") or "")
            text = compact_text(str(item.get("text") or ""))
            if not xpath or not text:
                continue
            rows.setdefault(xpath.rsplit("/", 1)[0], []).append(text)
        return list(rows.values())


def assess_crawl_quality(result: CrawlResult, *, min_text_chars: int = 700) -> dict[str, Any]:
    """Cheap capture-quality gate; this estimates extraction adequacy, not truth or credibility."""
    text = compact_text(result.markdown or result.text)
    words = re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)
    title_words = set(re.findall(r"[^\W_]+", (result.title or "").casefold(), flags=re.UNICODE))
    text_words = set(words)
    text_length = len(text)
    word_count = len(words)
    sentence_count = len(re.findall(r"[.!?。！？]+(?:\s|$)", text))
    unique_word_ratio = len(text_words) / word_count if word_count else 0.0
    visible_chars = sum(not char.isspace() for char in text)
    readable_char_ratio = sum(char.isalnum() for char in text) / visible_chars if visible_chars else 0.0
    title_overlap = len(title_words & text_words) / len(title_words) if title_words else 0.0

    article_marker = False
    main_marker = False
    paragraph_count = 0
    list_item_count = 0
    table_row_count = len(result.raw_payload.get("table_rows") or [])
    tag_count = 0
    link_density: float | None = None
    if result.html:
        soup = BeautifulSoup(result.html, "html.parser")
        article_marker = soup.find("article") is not None or soup.select_one('[itemtype*="Article"]') is not None
        og_type = soup.find("meta", attrs={"property": "og:type"})
        article_marker = article_marker or bool(og_type and "article" in str(og_type.get("content") or "").lower())
        main_marker = soup.find("main") is not None
        for tag in soup(["script", "style", "noscript", "svg", "canvas"]):
            tag.decompose()
        body = soup.body or soup
        tag_count = len(body.find_all(True))
        paragraph_count = sum(len(compact_text(tag.get_text(" "))) >= 40 for tag in body.find_all("p"))
        list_item_count = len(body.find_all("li"))
        table_row_count = max(table_row_count, len(body.find_all("tr")))
        body_text = compact_text(body.get_text(" "))
        link_text = compact_text(" ".join(tag.get_text(" ") for tag in body.find_all("a")))
        link_density = len(link_text) / max(len(body_text), 1)

    structured_items = max(list_item_count, table_row_count)
    if article_marker or (paragraph_count >= 3 and sentence_count >= 3 and (link_density or 0.0) < 0.5):
        profile = "article"
    elif table_row_count >= 2 or (list_item_count >= 5 and paragraph_count < 2):
        profile = "structured"
    else:
        profile = "general"

    content_type = str(result.raw_payload.get("content_type") or "").split(";", 1)[0].strip().lower()
    unsupported_static_type = bool(
        result.crawler == "static"
        and content_type
        and not content_type.startswith("text/")
        and content_type not in {"application/json", "application/xhtml+xml", "application/xml"}
    )
    blocked = crawl_result_has_block_signal(result)
    js_heavy = bool(result.raw_payload.get("js_heavy_hint"))
    repetitive = word_count >= 20 and unique_word_ratio < 0.35
    readable = readable_char_ratio >= 0.45
    low_article_link_density = link_density is None or link_density <= 0.65
    long_content = text_length >= max(1, min_text_chars) and (profile != "article" or low_article_link_density)
    compact_content = bool(
        text_length >= 120
        and word_count >= 12
        and readable
        and not repetitive
        and (link_density is None or link_density <= 0.85 or profile == "structured")
        and (bool(result.title) or paragraph_count or structured_items)
    )
    article_content = bool(
        profile == "article"
        and text_length >= 240
        and word_count >= 35
        and paragraph_count >= 2
        and low_article_link_density
    )
    structured_content = bool(profile == "structured" and text_length >= 100 and structured_items >= 2 and readable)
    usable = bool(
        not result.error
        and not blocked
        and text
        and not unsupported_static_type
        and not js_heavy
        and readable
        and not repetitive
        and (long_content or compact_content or article_content or structured_content)
    )

    target_chars = 500 if profile == "article" else 200 if profile == "structured" else 300
    length_signal = min(text_length / target_chars, 1.0)
    word_signal = min(word_count / (70 if profile == "article" else 30), 1.0)
    structure_signal = min(
        (paragraph_count / 3) if profile == "article" else (structured_items / 5) if profile == "structured" else ((paragraph_count + int(bool(result.title))) / 3),
        1.0,
    )
    sentence_signal = min(sentence_count / 4, 1.0)
    density_signal = 0.5 if link_density is None else max(0.0, 1.0 - link_density)
    score = (
        0.30 * length_signal
        + 0.15 * word_signal
        + 0.15 * structure_signal
        + 0.10 * sentence_signal
        + 0.10 * unique_word_ratio
        + 0.10 * readable_char_ratio
        + 0.05 * title_overlap
        + 0.05 * density_signal
    )
    if result.error or blocked or not text or unsupported_static_type:
        score = 0.0
    elif js_heavy:
        score = min(score, 0.3)

    reasons: list[str] = []
    if result.error:
        reasons.append("crawler_error")
    if blocked:
        reasons.append("block_detected")
    if not text:
        reasons.append("empty_text")
    if unsupported_static_type:
        reasons.append("unsupported_static_content_type")
    if js_heavy:
        reasons.append("js_heavy")
    if repetitive:
        reasons.append("high_repetition")
    if text and not readable:
        reasons.append("low_readable_character_ratio")
    if profile == "article" and not low_article_link_density:
        reasons.append("high_article_link_density")
    if not reasons:
        reasons.append("usable_capture" if usable else "insufficient_capture_signals")

    return {
        "method": "shallow_web_quality/v1",
        "profile": profile,
        "score": round(max(0.0, min(score, 1.0)), 3),
        "usable": usable,
        "blocked": blocked,
        "reasons": reasons,
        "features": {
            "text_length": text_length,
            "word_count": word_count,
            "sentence_count": sentence_count,
            "paragraph_count": paragraph_count,
            "structured_items": structured_items,
            "tag_count": tag_count,
            "link_density": round(link_density, 3) if link_density is not None else None,
            "unique_word_ratio": round(unique_word_ratio, 3),
            "readable_char_ratio": round(readable_char_ratio, 3),
            "title_overlap": round(title_overlap, 3),
            "content_type": content_type or None,
        },
    }


class AutoCrawler:
    name = "auto"

    def __init__(
        self,
        *,
        min_text_chars: int = 700,
        respect_robots: bool = True,
        allow_selenium_fallback: bool = False,
        polite_gate: PoliteRequestGate | None = None,
        parallel_stealth: bool = False,
        max_latency_seconds: float | None = None,
    ) -> None:
        self.min_text_chars = min_text_chars
        self.respect_robots = respect_robots
        self.allow_selenium_fallback = allow_selenium_fallback
        self.parallel_stealth = parallel_stealth
        self.max_latency_seconds = max_latency_seconds
        self.polite_gate = make_polite_gate(respect_robots=respect_robots, polite_gate=polite_gate)

    def crawl(self, url: str) -> CrawlResult:
        static = StaticSoupCrawler(
            timeout=self.max_latency_seconds or 20,
            respect_robots=self.respect_robots,
            polite_gate=self.polite_gate,
        ).crawl(url)
        static_quality = assess_crawl_quality(static, min_text_chars=self.min_text_chars)
        static.raw_payload["quality"] = static_quality
        if static_quality["usable"]:
            static.raw_payload["auto_decision"] = "static_primary"
            return static

        stealth = StealthBrowserCrawler(
            respect_robots=self.respect_robots,
            polite_gate=self.polite_gate,
            parallel=self.parallel_stealth,
            max_latency_seconds=self.max_latency_seconds,
        ).crawl(url)
        stealth_quality = assess_crawl_quality(stealth, min_text_chars=self.min_text_chars)
        stealth.raw_payload["quality"] = stealth_quality
        if stealth_quality["usable"]:
            stealth.raw_payload["auto_decision"] = "stealth_after_static"
            stealth.raw_payload["static_quality"] = static_quality
            return stealth
        if static_quality["blocked"] or stealth_quality["blocked"]:
            blocked = stealth if stealth_quality["blocked"] else static
            blocked.raw_payload["auto_decision"] = "blocked_no_managed_fallback"
            blocked.raw_payload["static_quality"] = static_quality
            blocked.raw_payload["stealth_quality"] = stealth_quality
            return blocked

        try:
            firecrawl = FirecrawlCrawler(
                timeout=int(self.max_latency_seconds) if self.max_latency_seconds else 180,
                respect_robots=self.respect_robots,
            ).crawl(url)
        except Exception as exc:
            firecrawl = CrawlResult.build(url=url, crawler="firecrawl", error=f"{type(exc).__name__}: {exc}")
        firecrawl_quality = assess_crawl_quality(firecrawl, min_text_chars=self.min_text_chars)
        firecrawl.raw_payload["quality"] = firecrawl_quality
        if firecrawl_quality["usable"]:
            firecrawl.raw_payload["auto_decision"] = "firecrawl_after_stealth"
            firecrawl.raw_payload["static_quality"] = static_quality
            firecrawl.raw_payload["stealth_quality"] = stealth_quality
            return firecrawl

        best, _quality = max(
            ((static, static_quality), (stealth, stealth_quality), (firecrawl, firecrawl_quality)),
            key=lambda item: float(item[1]["score"]),
        )
        best.raw_payload["auto_decision"] = "best_effort_after_firecrawl"
        best.raw_payload["static_quality"] = static_quality
        best.raw_payload["stealth_quality"] = stealth_quality
        best.raw_payload["firecrawl_quality"] = firecrawl_quality
        return best

    def crawl_with_actions(self, url: str, actions: list[dict[str, Any]]) -> CrawlResult:
        result = StealthBrowserCrawler(
            respect_robots=self.respect_robots,
            polite_gate=self.polite_gate,
            parallel=self.parallel_stealth,
            max_latency_seconds=self.max_latency_seconds,
        ).crawl_with_actions(url, actions)
        result.raw_payload["auto_decision"] = "stealth_interaction"
        return result


def create_crawler(
    name: str,
    *,
    respect_robots: bool = True,
    browser: str = "chromium",
    allow_selenium_fallback: bool = False,
    screenshot_path: str | Path | None = None,
    base_delay_seconds: float = 2.0,
    government_delay_seconds: float = 10.0,
    parallel_stealth: bool = False,
    max_latency_seconds: float | None = None,
) -> Crawler:
    normalized = name.strip().lower()
    polite_gate = make_polite_gate(
        respect_robots=respect_robots,
        base_delay_seconds=base_delay_seconds,
        government_delay_seconds=government_delay_seconds,
    )
    if normalized == "static":
        return ObservedCrawler(
            StaticSoupCrawler(
                timeout=max_latency_seconds or 20,
                respect_robots=respect_robots,
                polite_gate=polite_gate,
            )
        )
    if normalized == "playwright":
        return ObservedCrawler(
            PlaywrightCrawler(
                browser=browser,
                respect_robots=respect_robots,
                screenshot_path=screenshot_path,
                polite_gate=polite_gate,
            )
        )
    if normalized in {"playwright_stealth", "playwright-stealth"}:
        return ObservedCrawler(
            PlaywrightStealthCrawler(
                browser=browser,
                respect_robots=respect_robots,
                screenshot_path=screenshot_path,
                polite_gate=polite_gate,
            )
        )
    if normalized == "selenium":
        return ObservedCrawler(SeleniumCrawler(respect_robots=respect_robots, polite_gate=polite_gate))
    if normalized == "firecrawl":
        return ObservedCrawler(
            FirecrawlCrawler(
                timeout=int(max_latency_seconds) if max_latency_seconds else 180,
                respect_robots=respect_robots,
            )
        )
    if normalized == "apify_linkedin":
        return ObservedCrawler(ApifyLinkedInCrawler())
    if normalized == "watercrawl":
        return ObservedCrawler(WatercrawlCrawler())
    if normalized in {"stealth_browser", "stealth-browser"}:
        return ObservedCrawler(
            StealthBrowserCrawler(
                respect_robots=respect_robots,
                screenshot_path=screenshot_path,
                polite_gate=polite_gate,
                max_latency_seconds=max_latency_seconds,
            )
        )
    if normalized == "auto":
        return ObservedCrawler(
            AutoCrawler(
                respect_robots=respect_robots,
                allow_selenium_fallback=allow_selenium_fallback,
                polite_gate=polite_gate,
                parallel_stealth=parallel_stealth,
                max_latency_seconds=max_latency_seconds,
            )
        )
    raise ValueError(f"unknown crawler: {name}")
