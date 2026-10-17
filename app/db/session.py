"""Async engine + session factory.

SQLite is configured for high-concurrency *reads* (WAL + a shared read cache)
and serialised writes, which is the pattern that lets one node serve thousands
of concurrent students. Point `PORTAL_DATABASE_URL` at
`postgresql+asyncpg://...` for multi-node and the rest of the code is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_NULL_GATE = contextlib.nullcontext()  # Postgres and friends need no write gate

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def build_engine(url: str | None = None) -> AsyncEngine:
    url = url or settings.portal_database_url
    kwargs: dict = {"echo": settings.db_echo, "future": True}
    if _is_sqlite(url):
        # A real pool, not the NullPool aiosqlite picks by default: with WAL
        # every pooled connection is a concurrent reader, which is what lets a
        # single node serve thousands of simultaneous dashboard loads.
        kwargs.update(
            {
                "connect_args": {"timeout": 30, "check_same_thread": False},
                "poolclass": AsyncAdaptedQueuePool,
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
            }
        )
    else:
        kwargs.update(
            {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_pre_ping": True,
                "pool_recycle": 1800,
            }
        )
    engine = create_async_engine(url, **kwargs)

    if _is_sqlite(url):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            # WAL: readers never block the writer — essential for C10k reads.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA cache_size=-32000")  # ~32 MB page cache
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, autoflush=False, class_=AsyncSession
        )
    return _sessionmaker


def configure_database(engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Test hook: install an engine/sessionmaker built elsewhere."""
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = sessionmaker


async def init_models() -> None:
    from app.domain.entities import Base

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional unit of work: commit on success, rollback on error."""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


_AFTER_COMMIT = "portal.after_commit"
_HOOKED = "portal.after_commit_hooked"


def on_commit(session: AsyncSession, callback, *args) -> None:
    """Register work to run only if this session's transaction commits.

    Used for enqueueing background jobs. Enqueueing inside the transaction lets
    a worker pick the job up before the row is visible to it, which shows up as
    a mysterious "submission does not exist" under load. Hooked onto the
    session's own `after_commit` event so it fires no matter who commits.
    """
    session.info.setdefault(_AFTER_COMMIT, []).append((callback, args))
    if not session.info.get(_HOOKED):
        session.info[_HOOKED] = True

        @event.listens_for(session.sync_session, "after_commit")
        def _fire(sync_session) -> None:  # noqa: ANN001
            for fn, fn_args in sync_session.info.pop(_AFTER_COMMIT, []):
                fn(*fn_args)

        @event.listens_for(session.sync_session, "after_rollback")
        def _discard(sync_session) -> None:  # noqa: ANN001
            sync_session.info.pop(_AFTER_COMMIT, None)


def _is_sqlite_url() -> bool:
    return _is_sqlite(settings.portal_database_url)


# SQLite allows exactly one writer at a time, so queuing writers here costs
# nothing in throughput and keeps them from each holding a pooled connection
# while they wait on the database lock. Without this, a write burst consumes
# the whole pool and even read-only requests start failing.
_write_gate: asyncio.Semaphore | None = None


def write_gate() -> asyncio.Semaphore:
    global _write_gate
    if _write_gate is None:
        _write_gate = asyncio.Semaphore(1)
    return _write_gate


async def get_db_write() -> AsyncIterator[AsyncSession]:
    """Dependency for mutating endpoints: opens a write transaction up front.

    SQLite's busy handler is *not* invoked when a deferred (read) transaction
    is upgraded to a write while another writer holds the lock — it fails
    immediately with "database is locked", ignoring `busy_timeout`. Issuing
    `BEGIN IMMEDIATE` takes the write lock at the start of the transaction, so
    concurrent writers queue politely for up to `busy_timeout` instead of
    erroring. Readers are unaffected: in WAL mode the write lock never blocks
    them, which is why reads keep using the deferred `get_db`.
    """
    sm = get_sessionmaker()
    gate = write_gate() if _is_sqlite_url() else _NULL_GATE
    async with gate:
        async with sm() as session:
            try:
                if _is_sqlite_url():
                    await session.execute(text("BEGIN IMMEDIATE"))
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction per request.

    Commits when the endpoint returns normally and rolls back on any exception,
    so a handler cannot half-write (e.g. create a course but not its enrolment)
    and cannot forget to commit.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def healthcheck() -> str:
    async with get_sessionmaker()() as session:
        await session.execute(text("SELECT 1"))
    return "ok"
