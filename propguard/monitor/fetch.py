"""Polite HTTP fetcher: conditional requests, robots.txt, per-host rate limiting, no bypassing.

If a page is behind a CAPTCHA / anti-bot challenge / login, the fetch is recorded as BLOCKED and
never retried with evasion techniques.
"""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

BLOCK_MARKERS = ("captcha", "cf-chl", "just a moment...", "attention required", "access denied",
                 "enable javascript and cookies", "px-captcha", "are you a robot")


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: str  # OK | NOT_MODIFIED | BLOCKED | ERROR | DISALLOWED
    http_status: int | None = None
    body: str = ""
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None


class Fetcher:
    def __init__(self, user_agent: str, min_delay_per_host_s: float = 10.0, timeout_s: float = 20.0,
                 client: httpx.Client | None = None, respect_robots: bool = True) -> None:
        self.ua = user_agent
        self.min_delay = min_delay_per_host_s
        self.client = client or httpx.Client(timeout=timeout_s, follow_redirects=True,
                                             headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.5"})
        self.respect_robots = respect_robots
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _polite_wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.min_delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last[host] = time.monotonic()

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt", headers={"User-Agent": self.ua})
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                self._robots[base] = rp
            except httpx.HTTPError:
                self._robots[base] = None  # robots unreachable -> treat as allowed, fetch normally
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.ua, url)

    def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        if not self._allowed(url):
            return FetchResult(url, "DISALLOWED", error="disallowed by robots.txt")
        self._polite_wait(urlsplit(url).netloc)
        headers = {"User-Agent": self.ua}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            r = self.client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return FetchResult(url, "ERROR", error=type(exc).__name__ + ": " + str(exc)[:200])
        if r.status_code == 304:
            return FetchResult(url, "NOT_MODIFIED", 304, etag=etag, last_modified=last_modified)
        text = r.text if r.status_code < 500 else ""
        low = text[:20000].lower()
        challenged = len(text) < 30000 and any(m in low for m in BLOCK_MARKERS)
        if r.status_code in (401, 403, 429) or challenged:
            return FetchResult(url, "BLOCKED", r.status_code,
                               error=f"access blocked/challenged (HTTP {r.status_code}); not bypassed")
        if r.status_code >= 400:
            return FetchResult(url, "ERROR", r.status_code, error=f"HTTP {r.status_code}")
        return FetchResult(url, "OK", r.status_code, text, r.headers.get("etag"), r.headers.get("last-modified"))
