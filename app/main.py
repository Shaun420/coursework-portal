"""Application entry point.

`create_app()` is a factory so tests can build isolated instances with their
own database, provider and pipeline.
"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders

from app.api import assignments, auth, courses, drive, submissions, system
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.metrics import request_stats
from app.db.session import init_models
from app.services.drive import close_drive_provider
from app.services.workers.pipeline import get_pipeline
from app.web.router import router as web_router

log = get_logger(__name__)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_EXEMPT_PREFIXES = ("/auth/", "/api/system/health", "/docs", "/openapi.json", "/redoc")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings.ensure_dirs()
    await init_models()
    pipeline = get_pipeline()
    await pipeline.start()
    app.state.pipeline = pipeline
    log.info(
        "portal ready — env=%s drive=%s workers=%d queue=%d thresholds(sim=%.2f, ai=%.2f)",
        settings.portal_env, settings.drive_provider,
        settings.analysis_workers, settings.analysis_queue_capacity,
        settings.similarity_threshold, settings.ai_detection_threshold,
    )
    try:
        yield
    finally:
        await pipeline.stop()
        await close_drive_provider()
        log.info("portal shut down cleanly")


# ── Middleware ────────────────────────────────────────────────────────────────
#
# These are written as raw ASGI middleware rather than Starlette's
# BaseHTTPMiddleware. BaseHTTPMiddleware runs the downstream app inside an
# anyio task group and streams the response through a memory channel, which
# costs real CPU per request and — measured on this codebase — roughly halves
# throughput at 100 concurrent connections. Raw ASGI has no such wrapper.


class MetricsMiddleware:
    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = request_stats.start()
        holder = {"status": 500}

        async def send_wrapper(message):  # noqa: ANN001, ANN202
            if message["type"] == "http.response.start":
                holder["status"] = message["status"]
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "same-origin")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_stats.finish(started, scope.get("path", ""), holder["status"])


class CsrfMiddleware:
    """Double-submit CSRF: the header must match the (JS-readable) cookie."""

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if (
            scope["method"] in UNSAFE_METHODS
            and path.startswith("/api/")
            and not path.startswith(CSRF_EXEMPT_PREFIXES)
        ):
            headers = Headers(scope=scope)
            cookie_value = SimpleCookie(headers.get("cookie", "")).get(settings.csrf_cookie)
            cookie = cookie_value.value if cookie_value else ""
            token = headers.get("x-csrf-token", "")
            if not cookie or not token or not hmac.compare_digest(cookie, token):
                response = JSONResponse(
                    status_code=403,
                    content={
                        "error": "csrf_failed",
                        "message": "Missing or mismatched CSRF token. Reload the page and retry.",
                    },
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Coursework Portal",
        version="1.0.0",
        description=(
            "College assignments portal: Google Drive sync for drafts and "
            "submissions, similarity indexing, AI detection, 50% thresholds."
        ),
        lifespan=lifespan,
        docs_url="/docs",
    )

    register_exception_handlers(app)

    # add_middleware wraps outermost-last, so this order means:
    # GZip (outermost) -> Csrf -> Metrics+security headers -> routes.
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    app.include_router(auth.router)
    app.include_router(courses.router)
    app.include_router(assignments.router)
    app.include_router(submissions.assignments_router)
    app.include_router(submissions.submissions_router)
    app.include_router(drive.router)
    app.include_router(system.router)
    app.include_router(web_router)

    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "web" / "static"),
        name="static",
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=302)

    return app


app = create_app()
