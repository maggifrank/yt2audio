"""Client IP (X-Forwarded-For only from trusted proxies), rate limiting and Origin checks."""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from starlette.requests import Request

from .config import Config

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _ip(value: str):
    try:
        return ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None


def _trusted(cfg: Config, ip) -> bool:
    return ip is not None and any(ip in net for net in cfg.trusted_proxies)


def client_ip(cfg: Config, request: Request) -> str:
    peer = request.client.host if request.client else ""
    ip = _ip(peer)
    if not _trusted(cfg, ip):
        return peer or "unknown"
    # walk X-Forwarded-For from the right, skipping our own trusted proxies
    hops = [h for h in ",".join(request.headers.getlist("x-forwarded-for")).split(",") if h.strip()]
    for hop in reversed(hops):
        hip = _ip(hop)
        if hip is None:
            break
        if not _trusted(cfg, hip):
            return str(hip)
    return peer


class RateLimiter:
    """Sliding window: at most `count` events per `window` seconds per key."""

    def __init__(self, count: int, window: int):
        self.count, self.window = count, window
        self.events: dict[str, deque] = {}
        self.lock = threading.Lock()

    def hit(self, key: str) -> int:
        """Record an event; returns 0 if allowed, else seconds until the next one is allowed."""
        now = time.monotonic()
        with self.lock:
            q = self.events.setdefault(key, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.count:
                return int(q[0] + self.window - now) + 1
            q.append(now)
            if len(self.events) > 10000:  # drop idle keys
                for k in [k for k, v in self.events.items() if not v]:
                    del self.events[k]
            return 0


def origin_ok(cfg: Config, request: Request) -> bool:
    """State-changing requests must come from this site (or YTL_ALLOWED_ORIGINS)."""
    if request.method in SAFE_METHODS:
        return True
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        ref = request.headers.get("referer")
        if not ref:
            return False
        p = urlsplit(ref)
        origin = f"{p.scheme}://{p.netloc}"
    origin = origin.rstrip("/").lower()
    if origin in cfg.allowed_origins:
        return True
    host = (request.headers.get("host") or "").lower()
    return bool(host) and urlsplit(origin).netloc == host
