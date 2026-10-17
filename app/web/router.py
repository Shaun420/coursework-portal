"""Server-rendered pages.

The UI is intentionally plain: dense tables, monospace numbers, no framework,
no build step. Everything a student or teacher needs is on one screen, and the
whole front end is a few hundred lines of vanilla JS.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from app.core.config import settings
from app.db.repositories import AssignmentRepository, UserRepository
from app.db.session import get_sessionmaker
from app.services.auth import read_session

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _ctx(request: Request, **extra) -> dict:
    token = request.cookies.get(settings.session_cookie)
    data = read_session(token)
    return {
        "request": request,
        "session": data,
        "portal_name": settings.portal_name,
        "env": settings.portal_env,
        "thresholds": {
            "similarity": settings.similarity_threshold,
            "ai": settings.ai_detection_threshold,
        },
        **extra,
    }


def _user_id(request: Request) -> int | None:
    data = read_session(request.cookies.get(settings.session_cookie))
    return int(data["user_id"]) if data else None


@router.get("/login")
async def login_page(request: Request) -> Response:
    if _user_id(request) is not None:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        _ctx(
            request,
            google_configured=settings.google_configured,
            dev_signin=settings.portal_env != "production",
            drive_provider=settings.drive_provider,
        ),
    )


@router.get("/dashboard")
async def dashboard_page(request: Request) -> Response:
    if _user_id(request) is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request))


@router.get("/assignment/{assignment_id}")
async def assignment_page(request: Request, assignment_id: int) -> Response:
    if _user_id(request) is None:
        return RedirectResponse("/login", status_code=302)
    async with get_sessionmaker()() as db:
        assignment = await AssignmentRepository(db).get(assignment_id)
    if assignment is None:
        return templates.TemplateResponse(
            request, "error.html", _ctx(request, message="Assignment not found."), status_code=404
        )
    return templates.TemplateResponse(
        request, "assignment.html", _ctx(request, assignment_id=assignment_id)
    )


@router.get("/assignment/{assignment_id}/review")
async def review_page(request: Request, assignment_id: int) -> Response:
    if _user_id(request) is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        request, "review.html", _ctx(request, assignment_id=assignment_id)
    )


@router.get("/drive")
async def drive_page(request: Request) -> Response:
    if _user_id(request) is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        request, "drive.html", _ctx(request, drive_provider=settings.drive_provider)
    )


@router.get("/system")
async def system_page(request: Request) -> Response:
    if _user_id(request) is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        request, "system.html", _ctx(request, config={
            "analysis_workers": settings.analysis_workers,
            "queue_capacity": settings.analysis_queue_capacity,
            "cpu_threads": settings.analysis_cpu_threads,
            "drive_provider": settings.drive_provider,
            "max_upload_mb": round(settings.max_upload_bytes / 1_048_576, 1),
        })
    )


@router.get("/courses")
async def courses_page(request: Request) -> Response:
    uid = _user_id(request)
    if uid is None:
        return RedirectResponse("/login", status_code=302)
    async with get_sessionmaker()() as db:
        courses = await UserRepository(db).courses_for(uid)
    return templates.TemplateResponse(
        request,
        "courses.html",
        _ctx(request, courses=[{"id": c.id, "code": c.code, "title": c.title} for c in courses]),
    )
