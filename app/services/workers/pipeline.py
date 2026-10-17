"""The analysis pipeline: queue + worker pool + job handler wiring.

Lives on ``app.state`` so routers and workers share one instance, and so the
queue depth can be reported by ``/api/system/health``.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.workers.pool import WorkerPool
from app.services.workers.queue import Job, JobQueue

log = get_logger(__name__)


class AnalysisPipeline:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.queue = JobQueue(capacity=self.settings.analysis_queue_capacity)
        self.pool = WorkerPool(
            self.queue,
            workers=self.settings.analysis_workers,
            cpu_threads=self.settings.analysis_cpu_threads,
        )
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.pool.register("analyse_submission", self._handle)
        await self.pool.start()
        self.started = True

    async def stop(self) -> None:
        if not self.started:
            return
        await self.pool.stop()
        self.started = False

    def enqueue(self, submission_id: int) -> Job:
        return self.queue.enqueue("analyse_submission", {"submission_id": submission_id})

    async def drain(self, timeout: float = 60.0) -> None:
        """Block until every queued job has finished — used by tests/seeder."""
        await self.pool.drain(timeout=timeout)

    async def _handle(self, payload: dict) -> None:
        from app.services.analysis import analyse_submission

        # Workers own their sessions: a request-scoped session would be closed
        # long before the job runs.
        try:
            await analyse_submission(payload["submission_id"])
        except LookupError as exc:
            # The row is gone (rolled back or deleted). Retrying cannot help.
            log.warning("skipping analysis job: %s", exc)


_pipeline: AnalysisPipeline | None = None


def get_pipeline() -> AnalysisPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = AnalysisPipeline()
    return _pipeline


def set_pipeline(pipeline: AnalysisPipeline | None) -> None:
    global _pipeline
    _pipeline = pipeline
