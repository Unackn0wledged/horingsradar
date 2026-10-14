"""Polite HTTP fetching for regjeringen.no.

- Identifying User-Agent from config.
- At least REQUEST_INTERVAL_SECONDS between requests (one clock per Fetcher).
- Disk cache in DATA_DIR/http_cache. HTML is cached by age (the site sends no validators);
  files with ETag/Last-Modified are revalidated with conditional GET.
- robots.txt is honoured, including `*` and `$` patterns.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from horingsradar.config import Settings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, message: str = ""):
        super().__init__(f"{status or 'error'} for {url} {message}".strip())
        self.url = url
        self.status = status


class RobotsDisallowed(FetchError):
    def __init__(self, url: str):
        super().__init__(url, None, "disallowed by robots.txt")


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content: bytes
    content_type: str | None
    etag: str | None
    last_modified: str | None
    from_cache: bool

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# robots.txt


@dataclass
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


class RobotsRules:
    """Google-style robots.txt matching: longest pattern wins, Allow wins ties."""

    def __init__(self, text: str, agent: str = "*"):
        self.rules: list[_Rule] = []
        groups: list[tuple[list[str], list[_Rule]]] = []
        agents: list[str] = []
        rules: list[_Rule] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if not last_was_agent and (agents or rules):
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
                last_was_agent = True
            elif field in ("allow", "disallow"):
                last_was_agent = False
                if value:
                    rules.append(_Rule(field == "allow", value, self._compile(value)))
            else:
                last_was_agent = False
        if agents or rules:
            groups.append((agents, rules))
        token = agent.lower().split("/")[0]
        specific = [r for a, r in groups if any(x != "*" and x in token for x in a)]
        default = [r for a, r in groups if "*" in a]
        for group in specific or default:
            self.rules.extend(group)

    @staticmethod
    def _compile(pattern: str) -> re.Pattern[str]:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = ".*".join(re.escape(part) for part in body.split("*"))
        return re.compile("^" + regex + ("$" if anchored else ""), re.IGNORECASE)

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        best: _Rule | None = None
        for rule in self.rules:
            if not rule.regex.match(target):
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow)
            ):
                best = rule
        return best is None or best.allow


# ---------------------------------------------------------------------------
# Fetcher


class Fetcher:
    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.cache_dir = settings.http_cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(
            timeout=settings.http_timeout_seconds, follow_redirects=True
        )
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.interval = settings.request_interval_seconds
        self._last_request: float | None = None
        self._robots: dict[str, RobotsRules] = {}
        self.request_count = 0

    def close(self) -> None:
        self.client.close()

    # -- cache -------------------------------------------------------------

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        folder = self.cache_dir / key[:2]
        return folder / f"{key}.body", folder / f"{key}.json"

    def _read_cache(self, url: str) -> tuple[dict, bytes] | None:
        body_path, meta_path = self._paths(url)
        if not (body_path.is_file() and meta_path.is_file()):
            return None
        return json.loads(meta_path.read_text(encoding="utf-8")), body_path.read_bytes()

    def _write_cache(self, url: str, meta: dict, body: bytes | None) -> None:
        body_path, meta_path = self._paths(url)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        if body is not None:
            tmp = body_path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(body_path)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _result(url: str, meta: dict, body: bytes, from_cache: bool) -> FetchResult:
        return FetchResult(
            url=url,
            final_url=meta.get("final_url", url),
            status=meta.get("status", 200),
            content=body,
            content_type=meta.get("content_type"),
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
            from_cache=from_cache,
        )

    # -- network -----------------------------------------------------------

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self._last_request + self.interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last_request = self.clock()

    def _request(self, url: str, headers: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": self.settings.user_agent, **headers}
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            self.request_count += 1
            try:
                response = self.client.get(url, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                log.warning("Request failed (%s/%s) %s: %s", attempt, MAX_ATTEMPTS, url, exc)
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                last_error = FetchError(url, response.status_code)
                log.warning("HTTP %s (%s/%s) %s", response.status_code, attempt, MAX_ATTEMPTS, url)
            if attempt < MAX_ATTEMPTS:
                self.sleep(self.interval * attempt * 2)
        if isinstance(last_error, FetchError):
            raise last_error
        raise FetchError(url, None, str(last_error))

    def robots(self, url: str) -> RobotsRules:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            robots_url = origin + "/robots.txt"
            cached = self._read_cache(robots_url)
            if cached and self.wall_clock() - cached[0].get("fetched_at", 0) < 86400:
                text = cached[1].decode("utf-8", errors="replace")
            else:
                response = self._request(robots_url, {})
                if response.status_code == 200:
                    text = response.text
                elif 400 <= response.status_code < 500:
                    text = ""  # no robots.txt: everything allowed
                else:
                    raise FetchError(robots_url, response.status_code, "robots.txt unavailable")
                meta = {"url": robots_url, "status": 200, "fetched_at": self.wall_clock()}
                self._write_cache(robots_url, meta, text.encode("utf-8"))
            self._robots[origin] = RobotsRules(text, self.settings.user_agent)
        return self._robots[origin]

    def get(
        self, url: str, *, max_age: float | None = None, conditional: bool = True
    ) -> FetchResult:
        """Fetch `url`.

        max_age: serve from cache without any request if the copy is younger than this many
                 seconds. None means always go to the network (conditionally if possible).
        """
        if not self.robots(url).allowed(url):
            raise RobotsDisallowed(url)
        cached = self._read_cache(url)
        if cached and max_age is not None:
            meta, body = cached
            if self.wall_clock() - meta.get("fetched_at", 0) < max_age:
                return self._result(url, meta, body, from_cache=True)

        headers: dict[str, str] = {}
        if cached and conditional:
            meta = cached[0]
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        response = self._request(url, headers)
        if response.status_code == 304 and cached:
            meta, body = cached
            meta["fetched_at"] = self.wall_clock()
            self._write_cache(url, meta, None)
            return self._result(url, meta, body, from_cache=True)
        if response.status_code != 200:
            raise FetchError(url, response.status_code)
        meta = {
            "url": url,
            "final_url": str(response.url),
            "status": response.status_code,
            "fetched_at": self.wall_clock(),
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "content_type": response.headers.get("content-type"),
        }
        body = response.content
        self._write_cache(url, meta, body)
        return self._result(url, meta, body, from_cache=False)
