"""Request counters and latency percentiles.

A fixed-size ring buffer of recent latencies is enough to publish p50/p95/p99
for a single node without pulling in Prometheus, and costs a constant ~80 KB.
"""

from __future__ import annotations

import bisect
import threading
import time
from collections import deque


class RequestStats:
    def __init__(self, window: int = 5000) -> None:
        self.total = 0
        self.errors = 0
        self.in_flight = 0
        self._latencies: deque[float] = deque(maxlen=window)
        self._lock = threading.Lock()
        self.slowest: list[tuple[float, str]] = []

    def start(self) -> float:
        with self._lock:
            self.in_flight += 1
        return time.perf_counter()

    def finish(self, started: float, path: str, status_code: int) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        with self._lock:
            self.in_flight -= 1
            self.total += 1
            if status_code >= 500:
                self.errors += 1
            self._latencies.append(elapsed_ms)
            self.slowest.append((elapsed_ms, f"{status_code} {path}"))
            self.slowest.sort(key=lambda t: t[0], reverse=True)
            del self.slowest[10:]

    def percentile(self, values, p: float) -> float:  # noqa: ANN001
        with self._lock:
            data = sorted(values)
        if not data:
            return 0.0
        idx = min(len(data) - 1, max(0, bisect.bisect_left(data, 0) + int(len(data) * p / 100)))
        return round(data[idx], 2)

    def latency_samples(self) -> list[float]:
        with self._lock:
            return list(self._latencies)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total": self.total,
                "errors": self.errors,
                "in_flight": self.in_flight,
                "samples": len(self._latencies),
                "slowest": [{"ms": round(ms, 1), "path": path} for ms, path in self.slowest[:5]],
            }


request_stats = RequestStats()
