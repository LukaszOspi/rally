"""Abuse limits for the public, unauthenticated surface.

The map and list are deliberately open, and saved searches are created without
any login. That means POST /api/watches is reachable by anyone on the internet,
so it needs its own ceilings: this box is shared with several other apps and a
filled disk or exhausted memory would take all of them down, not just this one.
"""

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Bodies larger than this are refused before they are parsed or stored.
MAX_BODY_BYTES = 64 * 1024

# Per-IP ceilings on state-changing calls.
WRITE_LIMITS = ((10, 60), (40, 3600))  # (requests, seconds)

# Absolute ceiling on stored searches, as a last line of defence.
MAX_TOTAL_WATCHES = 5_000

# Bounds applied to a single saved search's criteria.
MAX_IDS_PER_FIELD = 50
MAX_CODES_PER_FIELD = 25
MAX_CRITERIA_BYTES = 4 * 1024


class RateLimiter:
    """Sliding-window counter keyed by client IP.

    In-process and therefore per-container, which is fine: there is exactly one
    api container and the limits are about abuse, not precise accounting.
    """

    def __init__(self, limits=WRITE_LIMITS):
        self.limits = limits
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()
        self._last_gc = time.monotonic()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        longest = max(window for _, window in self.limits)
        with self.lock:
            if now - self._last_gc > 300:
                self._gc(now, longest)
            hits = self.hits[key]
            while hits and now - hits[0] > longest:
                hits.popleft()
            for allowed, window in self.limits:
                recent = sum(1 for t in hits if now - t <= window)
                if recent >= allowed:
                    return False, int(window - (now - hits[0])) + 1
            hits.append(now)
            return True, 0

    def _gc(self, now: float, longest: float) -> None:
        """Drop idle keys so a spray of unique IPs cannot grow this forever."""
        stale = [k for k, v in self.hits.items() if not v or now - v[-1] > longest]
        for k in stale:
            del self.hits[k]
        self._last_gc = now


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    """Real client IP.

    Only Caddy can reach this container, and Caddy appends the true peer to
    X-Forwarded-For, so the last entry is authoritative. Reading the first
    entry instead would let a client spoof its own identity and dodge limits.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before FastAPI parses or stores them.

    Returns a response rather than raising: an HTTPException raised inside
    BaseHTTPMiddleware is not translated by the exception handlers, so it
    surfaces as a 500 and a reset connection instead of a clean 413.
    """

    async def dispatch(self, request: Request, call_next):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, 400)
            if declared > MAX_BODY_BYTES:
                return JSONResponse(
                    {"detail": f"Request body exceeds {MAX_BODY_BYTES} bytes"}, 413
                )
        elif request.method in ("POST", "PATCH", "PUT"):
            # No Content-Length means a streamed body; refuse rather than read
            # an unbounded amount into memory.
            if request.headers.get("transfer-encoding", "").lower() == "chunked":
                return JSONResponse({"detail": "Chunked bodies not accepted"}, 411)
        return await call_next(request)


def enforce_write_limit(request: Request) -> None:
    ok, retry_after = limiter.check(client_ip(request))
    if not ok:
        raise HTTPException(
            429,
            "Too many saved searches created from this address. "
            f"Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
