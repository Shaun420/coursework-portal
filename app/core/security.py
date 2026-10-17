"""Session signing, CSRF tokens and a small sliding-window rate limiter.

The rate limiter is deliberately in-process: on a single node it is exact, and
`scripts/loadtest.py` exercises it. Behind a load balancer you would swap
`RateLimiter` for the Redis implementation sketched at the bottom of the file.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner


class SessionSigner:
    """Signed (not encrypted) cookie payloads: `user_id.role.nonce`."""

    def __init__(self, secret: str, salt: str = "cp.session.v1") -> None:
        self._signer = TimestampSigner(secret, salt=salt)

    def issue(self, user_id: int, role: str, extra: dict[str, Any] | None = None) -> str:
        payload = f"{user_id}:{role}"
        if extra:
            payload += ":" + ",".join(f"{k}={v}" for k, v in sorted(extra.items()))
        return self._signer.sign(payload.encode()).decode()

    def read(self, token: str, max_age: int) -> dict[str, str] | None:
        try:
            raw = self._signer.unsign(token, max_age=max_age).decode()
        except (BadSignature, SignatureExpired):
            return None
        parts = raw.split(":")
        if len(parts) < 2:
            return None
        out = {"user_id": parts[0], "role": parts[1]}
        for kv in parts[2:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                out[k] = v
        return out


def csrf_token(secret: str, session_id: str) -> str:
    """Deterministic double-submit token bound to the session cookie value."""
    return hmac.new(secret.encode(), f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()


def new_state() -> str:
    return secrets.token_urlsafe(32)


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ── Rate limiting ─────────────────────────────────────────────────────────────


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: float
    limit: int


class RateLimiter:
    """Fixed-window-free sliding window over an in-memory deque per key."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_prune = 0.0

    def check(self, key: str, limit: int, window: float = 60.0) -> RateLimitResult:
        now = time.monotonic()
        if now - self._last_prune > 300:
            self._prune(now, window)
        bucket = self._hits[key]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return RateLimitResult(False, 0, window - (now - bucket[0]), limit)
        bucket.append(now)
        return RateLimitResult(True, limit - len(bucket), 0.0, limit)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)

    def _prune(self, now: float, window: float) -> None:
        self._last_prune = now
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > window]:
            self._hits.pop(key, None)


# A Redis-backed drop-in replacement would implement the same `check` contract
# with:  ZADD key now member; ZREMRANGEBYSCORE key 0 now-window; ZCARD key.
