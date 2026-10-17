"""Draft autosave and submission endpoints."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel

from app.api.deps import (
    DB,
    CurrentTeacher,
    CurrentUser,
    apply_rate_limit,
)
from app.core.config import settings
from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.core.storage import read_blob
from app.db.repositories import SubmissionRepository, UserRepository
from app.domain.enums import DraftKind
from app.domain.schemas import (
    DraftSaveOut,
    GradeIn,
    ReportOut,
    SubmissionOut,
)
from app.services import submissions as svc
from app.services.analysis import public_report
from app.services.drive.service import get_drive_sync

assignments_router = APIRouter(prefix="/api/assignments", tags=["submissions"])
submissions_router = APIRouter(prefix="/api/submissions", tags=["submissions"])


# ── Drafts (unsubmitted work → student's own Drive) ───────────────────────────


class DraftTextIn(BaseModel):
    text: str
    filename: str = "draft.txt"
    snapshot: bool = False


@assignments_router.post("/{assignment_id}/draft")
async def save_text_draft(
    assignment_id: int, payload: DraftTextIn, request: Request, user: CurrentUser
) -> DraftSaveOut:
    """Autosave from the in-browser editor. Debounced client-side, rate limited here."""
    await apply_rate_limit(request, limit=settings.draft_rate_limit_per_minute)
    if len(payload.text.encode()) > settings.max_upload_bytes:
        raise ValidationError("Draft is larger than the allowed upload size.")
    row = await svc.save_draft(
        student=user,
        assignment_id=assignment_id,
        filename=payload.filename,
        data=payload.text.encode(),
        kind=DraftKind.AUTOSAVE,
        snapshot=payload.snapshot,
    )
    return DraftSaveOut.model_validate(row)


@assignments_router.post("/{assignment_id}/draft/upload")
async def upload_draft(
    assignment_id: int,
    request: Request,
    user: CurrentUser,
    file: UploadFile = File(...),
    snapshot: bool = Form(False),
) -> DraftSaveOut:
    await apply_rate_limit(request, limit=settings.draft_rate_limit_per_minute)
    data = await file.read()
    row = await svc.save_draft(
        student=user,
        assignment_id=assignment_id,
        filename=file.filename or "upload.txt",
        data=data,
        kind=DraftKind.MANUAL,
        snapshot=snapshot,
    )
    return DraftSaveOut.model_validate(row)


@assignments_router.get("/{assignment_id}/draft")
async def get_draft(assignment_id: int, user: CurrentUser) -> dict:
    row = await svc.latest_draft(user, assignment_id)
    if row is None:
        return {"draft": None}
    return {
        "draft": DraftSaveOut.model_validate(row).model_dump(mode="json"),
        "history": [
            DraftSaveOut.model_validate(r).model_dump(mode="json")
            for r in await _draft_history(user, assignment_id)
        ],
    }


async def _draft_history(user, assignment_id: int):  # noqa: ANN001
    from app.db.repositories import DraftRepository
    from app.db.session import session_scope

    async with session_scope() as db:
        return await DraftRepository(db).history(assignment_id, user.id)


@assignments_router.get("/{assignment_id}/draft/content")
async def draft_content(assignment_id: int, user: CurrentUser) -> dict:
    """Fetch the current draft back from the student's Drive."""
    row = await svc.latest_draft(user, assignment_id)
    if row is None:
        raise NotFoundError("No draft saved for this assignment yet.")
    data = await svc.download_draft(user, row)
    return {
        "filename": row.filename,
        "revision": row.revision,
        "drive_path": row.drive_path,
        "text": data.decode("utf-8", errors="replace")[:2_000_000],
    }


# ── Submission ────────────────────────────────────────────────────────────────


@assignments_router.post("/{assignment_id}/submit")
async def submit_file(
    assignment_id: int,
    request: Request,
    user: CurrentUser,
    file: UploadFile = File(...),
) -> SubmissionOut:
    await apply_rate_limit(request)
    data = await file.read()
    sub = await svc.submit(
        student=user,
        assignment_id=assignment_id,
        filename=file.filename or "submission.txt",
        data=data,
        source="upload",
    )
    return SubmissionOut.model_validate(sub)


@assignments_router.post("/{assignment_id}/submit-draft")
async def submit_draft(
    assignment_id: int, request: Request, user: CurrentUser
) -> SubmissionOut:
    """Submit whatever the student last autosaved to their Drive."""
    await apply_rate_limit(request)
    row = await svc.latest_draft(user, assignment_id)
    if row is None:
        raise NotFoundError("There is no saved draft to submit.")
    data = await svc.download_draft(user, row)
    if not data:
        raise ValidationError("The saved draft is empty.")
    sub = await svc.submit(
        student=user,
        assignment_id=assignment_id,
        filename=row.filename,
        data=data,
        source="draft",
    )
    # Pin the exact revision handed in as a versioned copy on the student's own
    # Drive, so a later autosave cannot overwrite the record of what was sent.
    await svc.pin_draft_snapshot(user, row, data)
    return SubmissionOut.model_validate(sub)


@assignments_router.get("/{assignment_id}/my-submission")
async def my_submission(assignment_id: int, user: CurrentUser, db: DB) -> dict:
    sub = await SubmissionRepository(db).latest_for(assignment_id, user.id)
    if sub is None:
        return {"submission": None}
    return {"submission": SubmissionOut.model_validate(sub).model_dump(mode="json")}


# ── Reports, grading, re-analysis ─────────────────────────────────────────────


async def _load(db, submission_id: int):  # noqa: ANN001, ANN202
    sub = await SubmissionRepository(db).get(submission_id)
    if sub is None:
        raise NotFoundError("Submission not found.")
    return sub


@submissions_router.get("/{submission_id}")
async def get_submission(submission_id: int, user: CurrentUser, db: DB) -> SubmissionOut:
    sub = await _load(db, submission_id)
    _authorise_view(sub, user)
    return SubmissionOut.model_validate(sub)


@submissions_router.get("/{submission_id}/report")
async def get_report(submission_id: int, user: CurrentUser, db: DB) -> ReportOut:
    from app.db.repositories import AssignmentRepository

    sub = await _load(db, submission_id)
    _authorise_view(sub, user)
    assignment = await AssignmentRepository(db).get(sub.assignment_id)
    student = await UserRepository(db).get(sub.student_id)
    return ReportOut(
        submission_id=sub.id,
        assignment_title=assignment.title,
        student_name=student.name if student else "unknown",
        roll_number=student.roll_number if student else None,
        submitted_at=sub.submitted_at,
        status=sub.status,
        analysis_status=sub.analysis_status,
        similarity_score=sub.similarity_score,
        similarity_threshold=settings.similarity_threshold,
        ai_score=sub.ai_score,
        ai_threshold=settings.ai_detection_threshold,
        verdict=sub.verdict,
        flagged=sub.flagged,
        word_count=sub.word_count,
        report=public_report(sub.report),
    )


@submissions_router.get("/{submission_id}/download")
async def download_submission(submission_id: int, user: CurrentTeacher, db: DB):
    """Return the staged bytes so the instructor can read the work in-browser."""
    from fastapi.responses import Response

    from app.db.repositories import AssignmentRepository, CourseRepository

    sub = await _load(db, submission_id)
    assignment = await AssignmentRepository(db).get(sub.assignment_id)
    course = await CourseRepository(db).get(assignment.course_id)
    if course.instructor_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the course instructor can download submissions.")
    data = read_blob(sub.blob_path)
    if not data:
        raise NotFoundError("The staged copy of this submission is no longer on this server.")
    return Response(
        content=data,
        media_type=sub.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{sub.filename}"'},
    )


@submissions_router.post("/{submission_id}/grade")
async def grade_submission(
    submission_id: int, payload: GradeIn, teacher: CurrentTeacher
) -> SubmissionOut:
    sub = await svc.grade(
        actor=teacher, submission_id=submission_id,
        marks=payload.marks, feedback=payload.feedback,
    )
    return SubmissionOut.model_validate(sub)


@submissions_router.post("/{submission_id}/reanalyse")
async def reanalyse(submission_id: int, teacher: CurrentTeacher) -> dict:
    sub = await svc.reanalyse(submission_id, teacher)
    return {"ok": True, "submission_id": sub.id, "analysis_status": sub.analysis_status}


def _authorise_view(sub, user) -> None:  # noqa: ANN001
    if user.role in ("teacher", "admin"):
        return
    if sub.student_id != user.id:
        raise ForbiddenError("You can only view your own submissions.")


@assignments_router.get("/{assignment_id}/drive-location")
async def drive_location(assignment_id: int, user: CurrentTeacher, db: DB) -> dict:
    """Show the instructor exactly where their Drive folders will be."""
    from app.db.repositories import AssignmentRepository, CourseRepository
    from app.services.drive.paths import report_folder_path, submission_folder_path

    assignment = await AssignmentRepository(db).get(assignment_id)
    if assignment is None:
        raise NotFoundError("Assignment not found.")
    course = await CourseRepository(db).get(assignment.course_id)
    if course.instructor_id != user.id and user.role != "admin":
        raise ForbiddenError("Only the course instructor can do that.")

    drive = get_drive_sync()
    ctx = drive.course_context(course)
    actx = drive.assignment_context(assignment)
    return {
        "submissions_folder": "/".join(submission_folder_path(ctx, actx)),
        "reports_folder": "/".join(report_folder_path(ctx, actx)),
        "filename_pattern": "<roll>_<StudentName>_<code>_v<attempt>.<ext>",
        "report_pattern": "<roll>_<StudentName>_<code>_integrity_report.json",
        "roster_file": f"{actx.code.upper()}_roster_summary.csv",
    }
