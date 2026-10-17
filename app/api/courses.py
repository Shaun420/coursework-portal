"""Course, enrolment and reference-material endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import DB, DBW, CurrentTeacher, CurrentUser
from app.core.config import settings
from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.storage import sha256_of
from app.db.repositories import (
    AuditRepository,
    CourseRepository,
    ReferenceRepository,
    UserRepository,
)
from app.domain.schemas import CourseCreate, CourseOut
from app.services.drive.paths import AssignmentContext, submission_folder_path
from app.services.drive.service import get_drive_sync

log = get_logger(__name__)
router = APIRouter(prefix="/api/courses", tags=["courses"])

SELF_ENROL_LIMIT = 500


class ReferenceIn(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    text: str = Field(
        min_length=50,
        description="Instructor material submissions are compared against",
    )


async def _require_instructor(db, course_id: int, actor) -> object:  # noqa: ANN001, ANN202
    repo = CourseRepository(db)
    course = await repo.get(course_id)
    if course is None:
        raise NotFoundError("Course not found.")
    if course.instructor_id != actor.id and actor.role != "admin":
        raise ForbiddenError("Only the course instructor can do that.")
    return course


@router.get("")
async def list_my_courses(user: CurrentUser, db: DB) -> list[CourseOut]:
    courses = await UserRepository(db).courses_for(user.id)
    return [CourseOut.model_validate(c) for c in courses]


async def _may_create_course(db, user) -> bool:  # noqa: ANN001
    """Admin, an existing instructor, or staff on the institution's own domain.

    Roles in this app are derived from data rather than claimed at sign-up, so
    the first course needs a bootstrap path. It is limited to the institution's
    email domain and can be switched off with ALLOW_OPEN_COURSE_CREATION=false,
    after which only admins can create courses.
    """
    from app.domain.enums import Role

    if user.role in (Role.ADMIN.value, Role.TEACHER.value):
        return True
    if not settings.allow_open_course_creation:
        return False
    domain = user.email.rsplit("@", 1)[-1].lower()
    return domain == settings.institution_email_domain.lower()


@router.post("", status_code=201)
async def create_course(payload: CourseCreate, user: CurrentUser, db: DBW) -> CourseOut:
    if not await _may_create_course(db, user):
        raise ForbiddenError(
            "Only staff may create courses. Ask an administrator to promote your account."
        )
    teacher = user
    repo = CourseRepository(db)
    course = await repo.create(
        code=payload.code.upper(),
        title=payload.title,
        academic_year=payload.academic_year,
        semester=payload.semester,
        instructor_id=teacher.id,
    )
    await repo.enrol(teacher.id, course.id, role="teacher")
    # Creating a course is what makes someone a teacher. The user object came
    # from a detached auth lookup, so the promotion has to be written
    # explicitly rather than relying on ORM change tracking.
    from sqlalchemy import update

    from app.domain.entities import User
    from app.domain.enums import Role

    if teacher.role == Role.STUDENT.value:
        await db.execute(
            update(User).where(User.id == teacher.id).values(role=Role.TEACHER.value)
        )
        teacher.role = Role.TEACHER.value
    await AuditRepository(db).log(
        teacher.id, "course.created", "course", course.id, code=course.code
    )
    await _prewarm_drive_folders(teacher, course)
    return CourseOut.model_validate(course)


async def _prewarm_drive_folders(teacher, course) -> None:  # noqa: ANN001
    """Create the course folder on the instructor's Drive up front.

    Purely cosmetic — every submission creates its own folders — but it means a
    teacher linking their account sees the structure immediately.
    """
    if not teacher.drive_linked:
        return
    drive = get_drive_sync()
    ctx = drive.course_context(course)
    try:
        from app.core.config import settings

        await drive._resolve_path(
            drive.student_account(teacher),
            submission_folder_path(ctx, AssignmentContext(code="", title=""))[:-2],
            root_id=settings.teacher_drive_root_id or "root",
        )
    except Exception as exc:
        log.info("drive prewarm skipped for course %s: %s", course.code, exc)


@router.post("/{course_id}/enrol")
async def self_enrol(course_id: int, user: CurrentUser, db: DBW) -> dict:
    repo = CourseRepository(db)
    course = await repo.get(course_id)
    if course is None:
        raise NotFoundError("Course not found.")
    if await repo.student_count(course_id) >= SELF_ENROL_LIMIT:
        raise ForbiddenError("This course is full; ask the instructor to enrol you.")
    await repo.enrol(user.id, course_id, role="student")
    return {"ok": True, "course_id": course_id}


@router.post("/{course_id}/enrol/{email}")
async def enrol_by_email(course_id: int, email: str, teacher: CurrentTeacher, db: DBW) -> dict:
    await _require_instructor(db, course_id, teacher)
    target = await UserRepository(db).by_email(email.lower())
    if target is None:
        raise NotFoundError("No account with that email has signed in yet.")
    await CourseRepository(db).enrol(target.id, course_id, role="student")
    await AuditRepository(db).log(teacher.id, "course.enrolled", "course", course_id, email=email)
    return {"ok": True, "user_id": target.id}


@router.get("/{course_id}/roster")
async def roster(course_id: int, teacher: CurrentTeacher, db: DB) -> list[dict]:
    await _require_instructor(db, course_id, teacher)
    students = await CourseRepository(db).students(course_id)
    return [
        {
            "id": s.id,
            "name": s.name,
            "email": s.email,
            "roll_number": s.roll_number,
            "drive_linked": s.drive_linked,
        }
        for s in students
    ]


@router.post("/{course_id}/reference", status_code=201)
async def add_reference(
    course_id: int, payload: ReferenceIn, teacher: CurrentTeacher, db: DBW
) -> dict:
    await _require_instructor(db, course_id, teacher)
    doc = await ReferenceRepository(db).add(
        course_id=course_id,
        title=payload.title,
        body_text=payload.text,
        sha256=sha256_of(payload.text.encode()),
    )
    await AuditRepository(db).log(
        teacher.id, "reference.added", "course", course_id, title=payload.title
    )
    return {"id": doc.id, "title": doc.title}


@router.get("/{course_id}/reference")
async def list_references(course_id: int, teacher: CurrentTeacher, db: DB) -> list[dict]:
    await _require_instructor(db, course_id, teacher)
    docs = await ReferenceRepository(db).for_course(course_id)
    return [
        {"id": d.id, "title": d.title, "chars": len(d.body_text), "source": d.source}
        for d in docs
    ]


@router.post("/{course_id}/validate-code")
async def validate_code(course_id: int, user: CurrentUser, db: DB) -> dict:
    """Cheap probe used by the UI to check enrolment before showing a form."""
    course = await CourseRepository(db).get(course_id)
    if course is None:
        raise NotFoundError("Course not found.")
    is_member = await CourseRepository(db).is_member(user.id, course_id)
    if not is_member:
        raise ValidationError("You are not enrolled in this course.")
    return {"ok": True}
