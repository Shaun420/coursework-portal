"""Operational endpoints: health, queue, and live capacity metrics."""

from __future__ import annotations

import time

import psutil
from fastapi import APIRouter

from app.core.config import settings
from app.db import healthcheck
from app.db.session import get_engine
from app.domain.schemas import HealthOut, MetricsOut
from app.services.workers.pipeline import get_pipeline

router = APIRouter(prefix="/api/system", tags=["system"])

_STARTED = time.monotonic()


@router.get("/health")
async def health() -> HealthOut:
    db = await healthcheck()
    pipeline = get_pipeline()
    return HealthOut(
        status="ok" if db == "ok" else "degraded",
        env=settings.portal_env,
        database=db,
        drive_provider=settings.drive_provider,
        queue_depth=pipeline.queue.depth,
        queue_capacity=pipeline.queue.capacity,
        uptime_seconds=round(time.monotonic() - _STARTED, 1),
    )


@router.get("/queue")
async def queue() -> dict:
    stats = get_pipeline().queue.stats()
    return {
        "depth": stats.depth,
        "capacity": stats.capacity,
        "enqueued": stats.enqueued,
        "completed": stats.completed,
        "failed": stats.failed,
        "dropped": stats.dropped,
        "oldest_wait_seconds": stats.oldest_wait_seconds,
        "workers": get_pipeline().pool.worker_count,
        "cpu_threads": settings.analysis_cpu_threads,
    }


@router.get("/metrics")
async def metrics() -> MetricsOut:
    from app.core.metrics import request_stats

    engine = get_engine()
    pool = engine.pool
    checked_out = getattr(pool, "checkedout", lambda: 0)()

    proc = psutil.Process()
    with proc.oneshot():
        cpu = proc.cpu_percent(interval=None)
        rss = proc.memory_info().rss / (1024 * 1024)
        threads = proc.num_threads()

    lat = request_stats.latency_samples()
    return MetricsOut(
        requests_total=request_stats.total,
        requests_in_flight=request_stats.in_flight,
        errors_total=request_stats.errors,
        latency_p50_ms=request_stats.percentile(lat, 50),
        latency_p95_ms=request_stats.percentile(lat, 95),
        latency_p99_ms=request_stats.percentile(lat, 99),
        cpu_percent=round(cpu, 1),
        rss_mb=round(rss, 1),
        threads=threads,
        db_pool_checked_out=checked_out,
        queue_depth=get_pipeline().queue.depth,
        analysis_completed=get_pipeline().queue.completed,
        analysis_failed=get_pipeline().queue.failed,
    )


@router.get("/config")
async def config() -> dict:
    """Non-secret runtime configuration, for the ops page."""
    return {
        "env": settings.portal_env,
        "drive_provider": settings.drive_provider,
        "google_configured": settings.google_configured,
        "scopes": settings.scope_list,
        "thresholds": {
            "similarity": settings.similarity_threshold,
            "ai_detection": settings.ai_detection_threshold,
            "min_words_for_reliable_ai_score": settings.ai_detection_min_words,
        },
        "similarity": {
            "shingle_size": settings.shingle_size,
            "minhash_perms": settings.minhash_perms,
            "lsh_bands": settings.lsh_bands,
            "max_comparisons": settings.similarity_max_comparisons,
        },
        "pipeline": {
            "workers": settings.analysis_workers,
            "queue_capacity": settings.analysis_queue_capacity,
            "cpu_threads": settings.analysis_cpu_threads,
        },
        "limits": {
            "max_upload_bytes": settings.max_upload_bytes,
            "allowed_extensions": list(settings.allowed_extensions),
            "rate_limit_per_minute": settings.rate_limit_per_minute,
        },
        "database": {
            "url": _safe_url(settings.portal_database_url),
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
        },
    }


def _safe_url(url: str) -> str:
    if "@" in url:
        head, _, tail = url.rpartition("@")
        scheme = head.split("://", 1)[0]
        return f"{scheme}://***:***@{tail}"
    return url
