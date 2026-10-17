"""Worker pool.

Two pools, because the work is two different shapes:

* **I/O pool** — `analysis_workers` asyncio tasks draining the queue. They
  spend their time waiting on Drive and the database, so they are cheap and
  can outnumber CPUs.
* **CPU pool** — a bounded thread executor for text extraction, shingling and
  MinHashing. Those release the GIL only partially, so the executor is sized
  to the core count; keeping them off the event loop is what stops one huge
  PDF from stalling every other request.

Jobs are idempotent (keyed on submission id) so a restart just re-runs them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from app.core.logging import get_logger
from app.services.workers.queue import Job, JobQueue

log = get_logger(__name__)

Handler = Callable[[dict], Awaitable[None]]


class WorkerPool:
    def __init__(self, queue: JobQueue, workers: int, cpu_threads: int) -> None:
        self.queue = queue
        self.worker_count = workers
        self._handlers: dict[str, Handler] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, cpu_threads), thread_name_prefix="analysis-cpu"
        )
        self._loop: asyncio.AbstractEventLoop | None = None

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    async def run_blocking(self, fn: Callable, *args, **kwargs):
        """Run CPU-bound work off the event loop."""
        loop = asyncio.get_running_loop()
        if kwargs:
            from functools import partial

            fn = partial(fn, **kwargs)
        return await loop.run_in_executor(self.executor, fn, *args)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping.clear()
        for i in range(self.worker_count):
            self._tasks.append(asyncio.create_task(self._worker(i), name=f"analysis-worker-{i}"))
        log.info("analysis pool started: %d workers, %d cpu threads", self.worker_count,
                 self.executor._max_workers)

    async def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.queue._q.join(), timeout=timeout)

    async def _worker(self, index: int) -> None:
        while not self._stopping.is_set():
            try:
                job = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._dispatch(job)
                self.queue.task_done()
            except asyncio.CancelledError:
                self.queue.task_done(failed=True)
                return
            except Exception as exc:
                log.exception("worker %d job %s failed: %s", index, job.kind, exc)
                self.queue.task_done(failed=True)
                if not self.queue.requeue(job):
                    log.error("job %s dropped after %d attempts", job.payload, job.attempts)

    async def _dispatch(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            log.error("no handler registered for job kind %r", job.kind)
            return
        await handler(job.payload)

    async def drain(self, timeout: float = 60.0) -> None:
        """Wait until the queue is empty — used by tests and by the seeder."""
        await asyncio.wait_for(self.queue._q.join(), timeout=timeout)
