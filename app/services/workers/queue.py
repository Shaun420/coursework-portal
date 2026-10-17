"""Bounded in-process job queue.

The bound is the point. A deadline spike can arrive with thousands of
submissions in seconds; an unbounded queue would grow memory until the box
died. Instead the queue has a hard capacity, `enqueue` fails fast with 503
+ `Retry-After`, and the API stays responsive while workers drain the backlog.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.core.errors import QueueFullError
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Job:
    kind: str
    payload: dict[str, Any]
    enqueued_at: float
    attempts: int = 0


@dataclass
class QueueStats:
    depth: int
    capacity: int
    enqueued: int
    completed: int
    failed: int
    dropped: int
    oldest_wait_seconds: float


class JobQueue:
    def __init__(self, capacity: int) -> None:
        self._q: asyncio.Queue[Job] = asyncio.Queue(maxsize=capacity)
        self.capacity = capacity
        self.enqueued = 0
        self.completed = 0
        self.failed = 0
        self.dropped = 0
        self._inflight: deque[float] = deque()

    @property
    def depth(self) -> int:
        return self._q.qsize()

    @property
    def full(self) -> bool:
        return self._q.full()

    def enqueue(self, kind: str, payload: dict[str, Any]) -> Job:
        job = Job(kind=kind, payload=payload, enqueued_at=time.monotonic())
        try:
            self._q.put_nowait(job)
        except asyncio.QueueFull as exc:
            self.dropped += 1
            raise QueueFullError(
                "The analysis queue is full; the submission is saved and will be "
                "analysed shortly. Retry in a moment.",
                depth=self.depth,
                capacity=self.capacity,
            ) from exc
        self.enqueued += 1
        return job

    async def get(self) -> Job:
        job = await self._q.get()
        self._inflight.append(job.enqueued_at)
        return job

    def task_done(self, *, failed: bool = False) -> None:
        if self._inflight:
            self._inflight.popleft()
        if failed:
            self.failed += 1
        else:
            self.completed += 1
        self._q.task_done()

    def requeue(self, job: Job) -> bool:
        job.attempts += 1
        if job.attempts > 2:
            self.dropped += 1
            return False
        try:
            self._q.put_nowait(job)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        return True

    def stats(self) -> QueueStats:
        oldest = (time.monotonic() - self._inflight[0]) if self._inflight else 0.0
        return QueueStats(
            depth=self.depth,
            capacity=self.capacity,
            enqueued=self.enqueued,
            completed=self.completed,
            failed=self.failed,
            dropped=self.dropped,
            oldest_wait_seconds=round(oldest, 3),
        )
