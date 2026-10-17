"""Submission and draft workflow.

The state machine, in words:

    (nothing) --autosave--> DRAFT (lives on the student's Drive)
    DRAFT     --submit----> SUBMITTED  (copied to the teacher's Drive)
    SUBMITTED --worker----> ANALYSING --> ANALYSED | ANALYSIS_FAILED
    SUBMITTED --past due + allow_late--> LATE
    ANALYSED  --teacher grades--------> RETURNED

Resubmission increments `attempt` and keeps the previous row, so the history of
what a student handed in, and when, is never lost.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import settings
from app.core.errors import (
    ConflictError,
    DeadlinePassedError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.storage import blob_path, sha256_of, validate_upload
from app.db.repositories import (
    AssignmentRepository,
    AuditRepository,
    CourseRepository,
    DraftRepository,
    SubmissionRepository,
    UserRepository,
)
from app.db.session import on_commit, session_scope
from app.domain.entities import Assignment, Course, DraftRevision, Submission, User, as_utc
from app.domain.enums import AnalysisStatus, DraftKind, SubmissionStatus
from app.services.drive.service import get_drive_sync
from app.services.similarity.extractor import extract
from app.services.workers.pipeline import get_pipeline

log = get_logger(__name__)


async def _context(db, assignment_id: int) -> tuple[Assignment, Course]:  # noqa: ANN001
    assignment = await AssignmentRepository(db).get(assignment_id)
    if assignment is None:
        raise NotFoundError(f"Assignment {assignment_id} does not exist.")
    course = await CourseRepository(db).get(assignment.course_id)
    if course is None:
        raise NotFoundError("The course for this assignment is missing.")
    return assignment, course


async def require_enrolled_student(db, user: User, course: Course) -> None:  # noqa: ANN001
    courses = CourseRepository(db)
    if not await courses.is_member(user.id, course.id, role="student"):
        raise ForbiddenError("You are not enrolled in this course.", course_id=course.id)


# ── Drafts: unsubmitted work on the student's own Drive ───────────────────────


async def save_draft(
    *,
    student: User,
    assignment_id: int,
    filename: str,
    data: bytes,
    kind: DraftKind = DraftKind.AUTOSAVE,
    snapshot: bool = False,
) -> DraftRevision:
    """Autosave unsubmitted work onto the student's own Drive.

    The Drive call happens *between* two short database transactions rather
    than inside one. Holding a pooled connection (and, on SQLite, the write
    lock) across a network round-trip is what exhausts the pool under load.
    """
    validate_upload(filename, data)

    # ── read phase ──────────────────────────────────────────────────────────
    async with session_scope() as db:
        assignment, course = await _context(db, assignment_id)
        await require_enrolled_student(db, student, course)
        previous = await DraftRepository(db).latest(assignment_id, student.id)

    digest = sha256_of(data)
    # Autosave fires on every keystroke pause; identical bytes are a no-op so
    # 10k students typing at once do not mean 10k Drive writes. An explicit
    # "pin revision" is a deliberate action and is always honoured.
    if previous and previous.sha256 == digest and not snapshot:
        return previous

    revision = (previous.revision + 1) if previous else 1
    extracted = extract(filename, data)

    # ── Drive phase (no database connection held) ───────────────────────────
    result = await get_drive_sync().save_draft(
        student=student,
        course=course,
        assignment=assignment,
        filename=filename,
        data=data,
        revision=revision,
        live_file_id=previous.drive_file_id if previous else None,
        snapshot=snapshot,
        body_text=extracted.text[:200_000] if extracted.ok else None,
    )

    # ── write phase ─────────────────────────────────────────────────────────
    async with session_scope() as db:
        row = await DraftRepository(db).add(
            assignment_id=assignment_id,
            student_id=student.id,
            revision=revision,
            kind=kind.value,
            filename=filename,
            size_bytes=len(data),
            sha256=digest,
            body_text=(extracted.text[:200_000] if extracted.ok else None),
            drive_file_id=result.drive_file_id,
            drive_path=result.drive_path,
        )
        await AuditRepository(db).log(
            student.id, "draft.saved", "assignment", assignment_id,
            revision=revision, bytes=len(data), drive=result.drive_path,
        )
        return row


async def latest_draft(student: User, assignment_id: int) -> DraftRevision | None:
    async with session_scope() as db:
        assignment, course = await _context(db, assignment_id)
        await require_enrolled_student(db, student, course)
        return await DraftRepository(db).latest(assignment_id, student.id)


async def download_draft(student: User, draft: DraftRevision) -> bytes:
    if not draft.drive_file_id:
        raise NotFoundError("That draft has no file on Drive.")
    drive = get_drive_sync()
    return await drive.provider.download(drive.student_account(student), draft.drive_file_id)


# ── Submission ────────────────────────────────────────────────────────────────


async def pin_draft_snapshot(student: User, draft: DraftRevision, data: bytes) -> str | None:
    """Write an immutable, versioned copy of a draft to the student's Drive.

    Autosave keeps overwriting a single live file, so on its own a student has
    no record of what an earlier revision said. This pins the exact revision
    that was handed in as `..._draft_r03.txt`, which later edits cannot alter.

    Best effort: a failed bookkeeping write must never block a submission, so
    this logs and returns None rather than raising.
    """
    async with session_scope() as db:
        assignment, course = await _context(db, draft.assignment_id)
        await require_enrolled_student(db, student, course)

    try:
        result = await get_drive_sync().save_draft(
            student=student,
            course=course,
            assignment=assignment,
            filename=draft.filename,
            data=data,
            revision=draft.revision,
            live_file_id=draft.drive_file_id,
            snapshot=True,
        )
    except Exception:
        log.warning(
            "could not pin draft snapshot assignment=%s revision=%s",
            draft.assignment_id, draft.revision, exc_info=True,
        )
        return None
    log.info(
        "pinned draft snapshot assignment=%s revision=%s -> %s",
        draft.assignment_id, draft.revision, result.drive_path,
    )
    return result.drive_path


async def submit(
    *,
    student: User,
    assignment_id: int,
    filename: str,
    data: bytes,
    source: str = "upload",
) -> Submission:
    """Record the submission, archive it to the teacher's Drive, queue analysis.

    Ordering matters: the row is committed *before* the Drive upload, so a
    Drive outage can never lose a student's work — the staged blob and the row
    remain, and archiving can be retried. No database connection is held across
    the Drive call.
    """
    validate_upload(filename, data)
    digest = sha256_of(data)

    # ── transaction 1: validate and create the row ──────────────────────────
    async with session_scope() as db:
        assignment, course = await _context(db, assignment_id)
        await require_enrolled_student(db, student, course)

        now = datetime.now(UTC)
        if now < as_utc(assignment.opens_at):
            raise ConflictError("This assignment has not opened yet.")

        subs = SubmissionRepository(db)
        previous = await subs.latest_for(assignment_id, student.id)

        late = now > as_utc(assignment.due_at)
        if late and not assignment.allow_late:
            raise DeadlinePassedError(
                "The deadline has passed and late submissions are closed.",
                due_at=assignment.due_at.isoformat(),
            )

        if previous is not None:
            if not assignment.allow_resubmit:
                raise ConflictError("Resubmission is disabled for this assignment.")
            if previous.sha256 == digest:
                raise ConflictError(
                    "That file is identical to your current submission.",
                    submission_id=previous.id,
                )
            attempt = previous.attempt + 1
        else:
            attempt = 1

        submission = await subs.add(
            assignment_id=assignment_id,
            student_id=student.id,
            attempt=attempt,
            status=(SubmissionStatus.LATE.value if late else SubmissionStatus.SUBMITTED.value),
            filename=filename,
            mime_type=_mime(filename),
            size_bytes=len(data),
            sha256=digest,
            submitted_at=now,
            analysis_status=AnalysisStatus.PENDING.value,
        )
        # Stage locally first: this is the durable copy the analyser reads.
        path = blob_path(assignment_id, submission.id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        submission.blob_path = str(path)
        instructor = await UserRepository(db).get(course.instructor_id)
        await db.flush()

    # ── Drive phase (no database connection held) ───────────────────────────
    archive = await get_drive_sync().archive_submission(
        course=course,
        assignment=assignment,
        instructor=instructor,
        student=student,
        attempt=attempt,
        filename=filename,
        data=data,
        submitted_at=now,
    )

    # ── transaction 2: record where it landed, then queue analysis ──────────
    async with session_scope() as db:
        submission = await SubmissionRepository(db).get(submission.id)
        submission.teacher_file_id = archive.teacher_file_id
        submission.teacher_folder_id = archive.teacher_folder_id
        submission.archived_name = archive.archived_name
        await AuditRepository(db).log(
            student.id, "submission.created", "submission", submission.id,
            assignment_id=assignment_id, attempt=attempt, late=late,
            bytes=len(data), sha256=digest, source=source,
            teacher_file=archive.teacher_file_id, drive_path=archive.archived_path,
        )
        on_commit(db, _enqueue_analysis, submission.id)

    log.info(
        "submission %s (assignment=%s student=%s attempt=%s late=%s) -> %s",
        submission.id, assignment_id, student.id, attempt, late, archive.archived_path,
    )
    return submission


def _enqueue_analysis(submission_id: int) -> None:
    """Runs after the row commits, so the worker can always see it."""
    try:
        get_pipeline().enqueue(submission_id)
    except Exception as exc:  # queue full — the submission is safe and can be retried
        log.warning("could not enqueue analysis for %s: %s", submission_id, exc)


async def reanalyse(submission_id: int, actor: User) -> Submission:
    async with session_scope() as db:
        sub = await SubmissionRepository(db).get(submission_id)
        if sub is None:
            raise NotFoundError("Submission not found.")
        on_commit(db, get_pipeline().enqueue, sub.id)
        await AuditRepository(db).log(actor.id, "submission.reanalyse", "submission", submission_id)
        return sub


async def grade(
    *, actor: User, submission_id: int, marks: float, feedback: str
) -> Submission:
    async with session_scope() as db:
        sub = await SubmissionRepository(db).get(submission_id)
        if sub is None:
            raise NotFoundError("Submission not found.")
        assignment = await AssignmentRepository(db).get(sub.assignment_id)
        course = await CourseRepository(db).get(assignment.course_id)
        if course.instructor_id != actor.id and actor.role != "admin":
            raise ForbiddenError("Only the course instructor can grade this submission.")
        if marks > assignment.max_marks:
            raise ValidationError(
                f"Marks cannot exceed {assignment.max_marks} for this assignment.",
                max_marks=assignment.max_marks,
            )
        sub.marks_awarded = marks
        sub.feedback = feedback
        sub.status = SubmissionStatus.RETURNED.value
        await AuditRepository(db).log(
            actor.id, "submission.graded", "submission", submission_id, marks=marks
        )
        return sub


def _mime(filename: str) -> str:
    from app.services.drive.service import mime_for

    return mime_for(filename)


def upload_limit_bytes() -> int:
    return settings.max_upload_bytes
