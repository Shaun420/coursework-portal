"""Drive inspection endpoints.

These exist so a teacher (or a reviewer) can see, without leaving the portal,
exactly what the folder tree and filenames on Drive will look like — and so
tests can assert against the real provider output.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import DB, CurrentTeacher, CurrentUser
from app.core.config import settings
from app.core.errors import ForbiddenError, NotFoundError
from app.db.repositories import AssignmentRepository, UserRepository
from app.services.drive.paths import (
    AssignmentContext,
    CourseContext,
    draft_folder_path,
    report_folder_path,
    submission_folder_path,
)
from app.services.drive.service import get_drive_sync

router = APIRouter(prefix="/api/drive", tags=["drive"])


@router.get("/status")
async def status(user: CurrentUser, db: DB) -> dict:
    courses = await UserRepository(db).courses_for(user.id)
    taught = [c for c in courses if c.instructor_id == user.id]
    return {
        "provider": settings.drive_provider,
        "google_configured": settings.google_configured,
        "account_linked": user.drive_linked,
        "account_email": user.drive_email,
        "teacher_root_id": settings.teacher_drive_root_id,
        "courses": [
            {"code": c.code, "title": c.title, "teacher_folder_id": c.teacher_folder_id}
            for c in taught
        ],
        "thresholds": {
            "similarity": settings.similarity_threshold,
            "ai_detection": settings.ai_detection_threshold,
        },
    }


@router.get("/layout")
async def layout(user: CurrentUser, db: DB, course_id: int | None = None) -> dict:
    """The naming policy rendered against real course data."""
    courses = await UserRepository(db).courses_for(user.id)
    if not courses:
        raise NotFoundError("You have no courses yet.")
    course = courses[0]
    if course_id:
        found = next((c for c in courses if c.id == course_id), None)
        if found is None:
            raise NotFoundError("Course not found in your list.")
        course = found

    assignments = await AssignmentRepository(db).for_course(course.id)
    drive = get_drive_sync()
    ctx: CourseContext = drive.course_context(course)

    if assignments:
        actx = drive.assignment_context(assignments[0])
    else:
        actx = AssignmentContext(code="A1", title="First Assignment")

    return {
        "course": {"code": course.code, "title": course.title,
                   "term": f"{course.academic_year} Sem {course.semester}"},
        "teacher_drive": {
            "submissions": "/".join(submission_folder_path(ctx, actx)),
            "reports": "/".join(report_folder_path(ctx, actx)),
            "filename": "<roll>_<StudentName>_<code>_v<attempt>.<ext>",
        },
        "student_drive": {
            "drafts": "/".join(draft_folder_path(ctx, actx)),
            "filename": "<code>_<Title>_draft.<ext>",
        },
    }


@router.get("/tree")
async def teacher_tree(user: CurrentTeacher, db: DB, course_id: int | None = None) -> dict:
    """Walk the institutional/instructor Drive folder tree."""
    drive = get_drive_sync()
    account, degraded = drive.archive_account(user)
    root = settings.teacher_drive_root_id or "root"
    entries = await drive.tree(account, root_id=root)
    return {
        "account": account.email,
        "degraded": degraded,
        "provider": drive.provider.name,
        "root": root,
        "entries": entries,
    }


@router.get("/tree/mine")
async def my_tree(user: CurrentUser, db: DB) -> dict:
    """Walk the student's own Drive tree (drafts live here)."""
    if not user.drive_linked:
        raise ForbiddenError("Your Google account is not linked yet.")
    drive = get_drive_sync()
    account = drive.student_account(user)
    entries = await drive.tree(account, root_id="root")
    return {
        "account": account.email,
        "provider": drive.provider.name,
        "entries": entries,
    }
