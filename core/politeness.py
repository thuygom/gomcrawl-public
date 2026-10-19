from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urlparse, urlunparse

import httpx


DEFAULT_USER_AGENT = os.getenv("WEB_SEARCH_AGENT_USER_AGENT", "web-search-agent/0.1 public-evidence-crawler")


@dataclass(frozen=True, slots=True)
class PolitenessPolicy:
    user_agent: str = DEFAULT_USER_AGENT
    base_delay_seconds: float = 2.0
    government_delay_seconds: float = 10.0
    max_delay_seconds: float = 300.0
    jitter_min: float = 0.5
    jitter_max: float = 1.5
    cooldown_seconds: float = 300.0
    respect_robots: bool = True
    robots_timeout_seconds: float = 10.0


@dataclass(slots=True)
class RobotsInfo:
    allowed: bool = True
    crawl_delay_seconds: float | None = None
    error: str | None = None


@dataclass(slots=True)
class HostState:
    next_allowed_at: float = 0.0
    last_delay_seconds: float = 0.0
    consecutive_limited: int = 0
    robots: RobotsInfo | None = None
    events: list[dict[str, object]] = field(default_factory=list)


class PoliteRequestGate:
    def __init__(
        self,
        policy: PolitenessPolicy | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.policy = policy or PolitenessPolicy()
        self._sleeper = sleeper
        self._clock = clock
        self._jitter = jitter
        self._hosts: dict[str, HostState] = {}

    @property
    def user_agent(self) -> str:
        return self.policy.user_agent

    def wait_before_request(self, url: str) -> dict[str, object]:
        state = self._state_for(url)
        wait_seconds = max(0.0, state.next_allowed_at - self._clock())
        event = {
            "host": host_for(url),
            "wait_seconds": round(wait_seconds, 3),
            "reason": "host_delay" if wait_seconds > 0 else "ready",
        }
        if wait_seconds > 0:
            self._sleeper(wait_seconds)
        state.events.append(event)
        return event

    def assert_allowed(self, url: str) -> RobotsInfo:
        lowered = url.lower()
        if any(token in lowered for token in ("captcha", "login", "signin", "account/login")):
            return RobotsInfo(allowed=False, error="refusing likely login/captcha URL")
        if not self.policy.respect_robots:
            return RobotsInfo(allowed=True)

        state = self._state_for(url)
        if state.robots is None:
            state.robots = fetch_robots_info(
                url,
                user_agent=self.policy.user_agent,
                timeout=self.policy.robots_timeout_seconds,
            )
        return state.robots

    def record_response(self, url: str, status_code: int | None, headers: dict[str, str] | httpx.Headers | None = None) -> None:
        state = self._state_for(url)
        delay = self.delay_for(url, state.robots)
        headers_dict = normalize_headers(headers)
        retry_after = retry_after_seconds(headers_dict.get("retry-after"))
        limited = status_code in {403, 429, 503}
        if retry_after is not None:
            delay = max(delay, retry_after)
        if limited:
            state.consecutive_limited += 1
            delay = max(delay, min(self.policy.cooldown_seconds, 60.0 * state.consecutive_limited))
        else:
            state.consecutive_limited = 0

        delay = min(delay, self.policy.max_delay_seconds)
        state.last_delay_seconds = delay
        state.next_allowed_at = self._clock() + delay
        state.events.append(
            {
                "host": host_for(url),
                "status_code": status_code,
                "delay_seconds": round(delay, 3),
                "retry_after_seconds": retry_after,
                "consecutive_limited": state.consecutive_limited,
            }
        )

    def record_error(self, url: str, error: str) -> None:
        state = self._state_for(url)
        delay = min(max(self.delay_for(url, state.robots), 30.0), self.policy.max_delay_seconds)
        state.next_allowed_at = self._clock() + delay
        state.last_delay_seconds = delay
        state.events.append({"host": host_for(url), "error": error, "delay_seconds": round(delay, 3)})

    def delay_for(self, url: str, robots: RobotsInfo | None = None) -> float:
        base = self.policy.base_delay_seconds
        if is_government_host(host_for(url)):
            base = max(base, self.policy.government_delay_seconds)
        if robots and robots.crawl_delay_seconds is not None:
            base = max(base, robots.crawl_delay_seconds)
        jittered = base * self._jitter(self.policy.jitter_min, self.policy.jitter_max)
        return min(max(0.0, jittered), self.policy.max_delay_seconds)

    def latest_event_for(self, url: str) -> dict[str, object]:
        events = self._state_for(url).events
        return dict(events[-1]) if events else {}

    def host_report(self) -> dict[str, dict[str, object]]:
        return {
            host: {
                "next_allowed_at": state.next_allowed_at,
                "last_delay_seconds": state.last_delay_seconds,
                "consecutive_limited": state.consecutive_limited,
                "robots_allowed": state.robots.allowed if state.robots else None,
                "robots_crawl_delay_seconds": state.robots.crawl_delay_seconds if state.robots else None,
                "events": list(state.events[-5:]),
            }
            for host, state in self._hosts.items()
        }

    def _state_for(self, url: str) -> HostState:
        host = host_for(url)
        if host not in self._hosts:
            self._hosts[host] = HostState()
        return self._hosts[host]


def host_for(url: str) -> str:
    return urlparse(url).netloc.lower()


def robots_url_for(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme or "https", parsed.netloc, "/robots.txt", "", "", ""))


def fetch_robots_info(url: str, *, user_agent: str = DEFAULT_USER_AGENT, timeout: float = 10.0) -> RobotsInfo:
    import urllib.robotparser

    if not host_for(url):
        return RobotsInfo(allowed=False, error="missing URL host")

    robot_url = robots_url_for(url)
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robot_url)
    try:
        response = httpx.get(robot_url, headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
        if response.status_code >= 400:
            return RobotsInfo(allowed=True, error=f"robots.txt HTTP {response.status_code}")
        parser.parse(response.text.splitlines())
        delay = parser.crawl_delay(user_agent)
        if delay is None:
            delay = parser.crawl_delay("*")
        return RobotsInfo(
            allowed=parser.can_fetch(user_agent, url),
            crawl_delay_seconds=float(delay) if delay is not None else None,
        )
    except Exception as exc:
        return RobotsInfo(allowed=True, error=str(exc))


def is_government_host(host: str) -> bool:
    return host.endswith(".gov") or host.endswith(".gov.kr") or host.endswith(".go.kr")


def normalize_headers(headers: dict[str, str] | httpx.Headers | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in dict(headers).items()}


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.isdigit():
        return float(stripped)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
