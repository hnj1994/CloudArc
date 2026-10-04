"""Client IP resolution and in-process rate limiting.

The app runs as a single instance (DuckDB), so limits are kept in memory: a restart resets them,
which is acceptable for brute-force and cost protection. Limits are per client IP.
"""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict, deque

from starlette.requests import Request


def client_ip(request: Request) -> str | None:
    """The caller's IP. Behind App Service the TCP peer is the platform front end (a private or
    link-local address) and the real client is the *last* X-Forwarded-For entry, the one the front end
    appended; earlier entries are client-controlled and ignored."""
    peer = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for")
    if not xff or not _is_internal(peer):
        return peer
    last = xff.split(",")[-1].strip()
    if last.startswith("["):  # [IPv6]:port
        last = last[1:].split("]", 1)[0]
    elif last.count(":") == 1:  # IPv4:port
        last = last.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(last))
    except ValueError:
        return peer


def _is_internal(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in {"testclient", "localhost"}
    return ip.is_private or ip.is_link_local or ip.is_loopback


class SlidingWindow:
    """At most ``limit`` events per ``window`` seconds per key."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, q: deque, now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` may proceed (0 = allowed now). Does not record an event."""
        with self._lock:
            q = self._events.get(key)
            if not q:
                return 0
            now = self.clock()
            self._trim(q, now)
            return 0 if len(q) < self.limit else max(1, int(q[0] + self.window - now) + 1)

    def hit(self, key: str) -> None:
        with self._lock:
            q = self._events[key]
            now = self.clock()
            self._trim(q, now)
            q.append(now)
            if len(self._events) > 50_000:  # bound memory under a spray of distinct IPs
                for k in [k for k, v in self._events.items() if not v][:10_000]:
                    del self._events[k]


class Limits:
    def __init__(self):
        # Wrong or expired tokens: blocks credential guessing.
        self.auth_failures = SlidingWindow(limit=20, window=300)
        # Credential validation calls cloud APIs (AWS bills Cost Explorer per request).
        self.cloud_checks = SlidingWindow(limit=15, window=600)
        # Everything else: generous, only stops runaway clients.
        self.api = SlidingWindow(limit=1200, window=60)


CLOUD_CHECK_SUFFIXES = ("/connect/aws", "/connect/gcp", "/connect/aws/validate", "/connect/gcp/validate",
                        "/onboarding/validate", "/onboarding/complete")
