"""Assignment endpoints and the per-user dashboard feed."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api.deps import DB, DBW, CurrentTeacher, CurrentUser
from app.core.errors import ForbiddenError, NotFoundError
from app.db.repositories import (
    AssignmentRepository,
    AuditRepository,
    CourseRepository,
    SubmissionRepository,
)
from app.domain.enums import Role
from app.domain.schemas import (
    AssignmentCreate,
    AssignmentListItem,
    AssignmentOut,
    SubmissionWithStudent,
)

router = APIRouter(prefix="/api/assignments", tags=["assignments"])


@router.get("")
async def dashboard(user: CurrentUser, db: DB) -> list[AssignmentListItem]:
    """One call that powers the dashboard for both roles."""
    repo = AssignmentRepository(db)

    if user.role in (Role.TEACHER.value, Role.ADMIN.value):
        rows = await repo.dashboard_for_teacher(user.id)
        out: list[AssignmentListItem] = []
        for assignment, course, total, flagged, students in rows:
            item = AssignmentListItem.model_validate(assignment)
            item.course_code = course.code
            item.course_title = course.title
            item.submission_count = int(total)
            item.flagged_count = int(flagged)
            item.student_count = int(students)
            out.append(item)
        return out

    rows = await repo.dashboard_for_student(user.id)
    out = []
    for assignment, course, mine in rows:
        item = AssignmentListItem.model_validate(assignment)
        item.course_code = course.code
        item.course_title = course.title
        item.my_status = mine.status if mine else None
        item.my_similarity = mine.similarity_score if mine else None
        item.my_ai_score = mine.ai_score if mine else None
        out.append(item)
    return out


@router.post("", status_code=201)
async def create_assignment(
    payload: AssignmentCreate, teacher: CurrentTeacher, db: DBW
) -> AssignmentOut:
    course = await CourseRepository(db).get(payload.course_id)
    if course is None:
        raise NotFoundError("Course not found.")
    if course.instructor_id != teacher.id and teacher.role != "admin":
        raise ForbiddenError("You do not teach that course.")
    if payload.due_at.tzinfo is None:
        raise ForbiddenError("due_at must include a timezone offset, e.g. 2026-02-01T23:59:00Z")

    repo = AssignmentRepository(db)
    assignment = await repo.create(
        course_id=course.id,
        code=payload.code.upper(),
        title=payload.title,
        brief=payload.brief,
        max_marks=payload.max_marks,
        opens_at=payload.opens_at or datetime.now(UTC),
        due_at=payload.due_at,
        allow_late=payload.allow_late,
        allow_resubmit=payload.allow_resubmit,
        created_by=teacher.id,
    )
    await AuditRepository(db).log(
        teacher.id, "assignment.created", "assignment", assignment.id,
        course=course.code, code=assignment.code,
    )
    return AssignmentOut.model_validate(assignment)


@router.get("/{assignment_id}")
async def get_assignment(assignment_id: int, user: CurrentUser, db: DB) -> AssignmentOut:
    assignment = await AssignmentRepository(db).get(assignment_id)
    if assignment is None:
        raise NotFoundError("Assignment not found.")
    course = await CourseRepository(db).get(assignment.course_id)
    if not await CourseRepository(db).is_member(user.id, course.id):
        raise ForbiddenError("You are not enrolled in this course.")
    return AssignmentOut.model_validate(assignment)


@router.get("/{assignment_id}/submissions")
async def list_submissions(
    assignment_id: int, teacher: CurrentTeacher, db: DB
) -> list[SubmissionWithStudent]:
    assignment = await AssignmentRepository(db).get(assignment_id)
    if assignment is None:
        raise NotFoundError("Assignment not found.")
    course = await CourseRepository(db).get(assignment.course_id)
    if course.instructor_id != teacher.id and teacher.role != "admin":
        raise ForbiddenError("Only the course instructor can view submissions.")

    rows = await SubmissionRepository(db).with_students(assignment_id)
    out: list[SubmissionWithStudent] = []
    for s, student in rows:
        item = SubmissionWithStudent.model_validate(s)
        item.student_name = student.name if student else None
        item.student_email = student.email if student else None
        item.roll_number = student.roll_number if student else None
        out.append(item)
    return out


@router.get("/{assignment_id}/summary")
async def assignment_summary(assignment_id: int, teacher: CurrentTeacher, db: DB) -> dict:
    assignment = await AssignmentRepository(db).get(assignment_id)
    if assignment is None:
        raise NotFoundError("Assignment not found.")
    course = await CourseRepository(db).get(assignment.course_id)
    if course.instructor_id != teacher.id and teacher.role != "admin":
        raise ForbiddenError("Only the course instructor can view the summary.")
    summary = await SubmissionRepository(db).summary(assignment_id)
    summary["assignment"] = AssignmentOut.model_validate(assignment).model_dump(mode="json")
    summary["course"] = {"code": course.code, "title": course.title}
    return summary
