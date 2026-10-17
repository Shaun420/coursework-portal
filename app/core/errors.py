"""Domain exception hierarchy and the FastAPI handlers that map it to HTTP."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError, TimeoutError


class AppError(Exception):
    """Base class for every error this application raises on purpose."""

    status_code = 400
    code = "app_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class UploadTooLargeError(AppError):
    status_code = 413
    code = "upload_too_large"


class UnsupportedFileTypeError(AppError):
    status_code = 415
    code = "unsupported_file_type"


class GoogleNotConfiguredError(AppError):
    status_code = 503
    code = "google_not_configured"


class DriveError(AppError):
    status_code = 502
    code = "drive_error"


class QueueFullError(AppError):
    status_code = 503
    code = "analysis_queue_full"


class DeadlinePassedError(AppError):
    status_code = 409
    code = "deadline_passed"


class NotLinkedError(AppError):
    """Raised when a route needs a Google account that is not linked yet."""

    status_code = 409
    code = "google_account_not_linked"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(TimeoutError)
    async def _pool_timeout(_: Request, exc: TimeoutError) -> JSONResponse:
        # SQLAlchemy raises sqlalchemy.exc.TimeoutError (a built-in subclass)
        # when the connection pool is exhausted. The server is overloaded, not
        # broken, so say so and tell the client when to come back.
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "2"},
            content={
                "error": "overloaded",
                "message": "The server is saturated. The request was not processed; retry shortly.",
            },
        )

    @app.exception_handler(OperationalError)
    async def _operational(_: Request, exc: OperationalError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "2"},
            content={
                "error": "database_busy",
                "message": "The database is busy with another write. Retry shortly.",
                "details": {"detail": str(exc.orig)[:200]},
            },
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        # A unique-constraint violation is the client's problem (duplicate
        # course code, double enrolment), not a server fault.
        return JSONResponse(
            status_code=409,
            content={
                "error": "conflict",
                "message": "That record already exists.",
                "details": {"constraint": str(exc.orig)[:200]},
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("portal.errors").exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "The server hit an unexpected problem. It has been logged.",
            },
        )
