"""Queue backpressure and worker dispatch."""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import QueueFullError
from app.services.analysis import decide_verdict
from app.services.workers.pool import WorkerPool
from app.services.workers.queue import JobQueue


def test_queue_rejects_when_full():
    q = JobQueue(capacity=2)
    q.enqueue("analyse_submission", {"submission_id": 1})
    q.enqueue("analyse_submission", {"submission_id": 2})
    with pytest.raises(QueueFullError) as exc:
        q.enqueue("analyse_submission", {"submission_id": 3})
    assert exc.value.status_code == 503
    assert q.dropped == 1
    assert q.stats().depth == 2


@pytest.mark.asyncio
async def test_pool_runs_registered_jobs_to_completion():
    q = JobQueue(capacity=100)
    pool = WorkerPool(q, workers=4, cpu_threads=2)
    done: list[int] = []

    async def handler(payload: dict) -> None:
        await asyncio.sleep(0.01)
        done.append(payload["n"])

    pool.register("count", handler)
    await pool.start()
    for n in range(50):
        q.enqueue("count", {"n": n})
    await pool.drain(timeout=20)
    await pool.stop()

    assert sorted(done) == list(range(50))
    assert q.stats().completed == 50
    assert q.stats().failed == 0


@pytest.mark.asyncio
async def test_failing_job_is_retried_then_dropped():
    q = JobQueue(capacity=10)
    pool = WorkerPool(q, workers=1, cpu_threads=1)
    calls = {"n": 0}

    async def handler(_payload: dict) -> None:
        calls["n"] += 1
        raise RuntimeError("boom")

    pool.register("bad", handler)
    await pool.start()
    q.enqueue("bad", {})
    await asyncio.sleep(0.4)
    await pool.stop()

    # initial attempt + two retries, then it is dropped rather than looping forever
    assert calls["n"] == 3
    assert q.stats().failed >= 1


@pytest.mark.asyncio
async def test_run_blocking_keeps_the_loop_responsive():
    q = JobQueue(capacity=4)
    pool = WorkerPool(q, workers=1, cpu_threads=2)

    def heavy(n: int) -> int:
        total = 0
        for i in range(n):
            total += i * i
        return total

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while ticks < 20:
            await asyncio.sleep(0)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    result = await pool.run_blocking(heavy, 200_000)
    await tick_task
    await pool.stop()

    assert result == sum(i * i for i in range(200_000))
    assert ticks == 20  # the loop was never blocked


def test_threshold_is_inclusive_at_fifty_percent():
    """The policy is "up to 50 %" — exactly 0.50 must be flagged."""
    assert decide_verdict(0.50, 0.10)[1] is True
    assert decide_verdict(0.50, 0.10)[0].value == "similarity_flag"
    assert decide_verdict(0.4999, 0.10)[1] is False
    assert decide_verdict(0.10, 0.50)[0].value == "ai_flag"
    assert decide_verdict(0.90, 0.90)[0].value == "both_flag"
    assert decide_verdict(0.10, 0.10)[0].value == "clean"
    assert decide_verdict(None, None) == (
        decide_verdict(None, None)[0], False
    )
