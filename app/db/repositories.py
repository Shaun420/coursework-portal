"""Data-access layer.

Every query the app runs lives in a repository function. Services never build
SQL, which keeps the ORM swappable and makes the query surface auditable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, and_, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import (
    Assignment,
    AuditEvent,
    Course,
    DraftRevision,
    Enrolment,
    ReferenceDocument,
    Submission,
    User,
)

# ── Users ─────────────────────────────────────────────────────────────────────


class UserRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, user_id: int) -> User | None:
        return await self.db.get(User, user_id)

    async def by_google_sub(self, sub: str) -> User | None:
        result = await self.db.execute(select(User).where(User.google_sub == sub))
        return result.scalar_one_or_none()

    async def by_email(self, email: str) -> User | None:
        return (await self.db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    async def create(self, **fields) -> User:
        user = User(**fields)
        self.db.add(user)
        await self.db.flush()
        return user

    async def courses_for(self, user_id: int, role: str | None = None) -> Sequence[Course]:
        stmt = (
            select(Course)
            .join(Enrolment, Enrolment.course_id == Course.id)
            .where(Enrolment.user_id == user_id)
        )
        if role:
            stmt = stmt.where(Enrolment.role == role)
        return (await self.db.execute(stmt.order_by(Course.code))).scalars().all()

    async def touch(self, user_id: int) -> None:
        await self.db.execute(
            update(User).where(User.id == user_id).values(last_seen_at=datetime.now(UTC))
        )


# ── Courses / enrolment ───────────────────────────────────────────────────────


class CourseRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, course_id: int) -> Course | None:
        return await self.db.get(Course, course_id)

    async def create(self, **fields) -> Course:
        course = Course(**fields)
        self.db.add(course)
        await self.db.flush()
        return course

    async def enrol(self, user_id: int, course_id: int, role: str = "student") -> Enrolment:
        existing = (
            await self.db.execute(
                select(Enrolment).where(
                    Enrolment.user_id == user_id, Enrolment.course_id == course_id
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        row = Enrolment(user_id=user_id, course_id=course_id, role=role)
        self.db.add(row)
        await self.db.flush()
        return row

    async def is_member(self, user_id: int, course_id: int, role: str | None = None) -> bool:
        stmt = select(Enrolment.id).where(
            Enrolment.user_id == user_id, Enrolment.course_id == course_id
        )
        if role:
            stmt = stmt.where(Enrolment.role == role)
        return (await self.db.execute(stmt.limit(1))).first() is not None

    async def student_count(self, course_id: int) -> int:
        stmt = (
            select(func.count(Enrolment.id))
            .where(Enrolment.course_id == course_id, Enrolment.role == "student")
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def students(self, course_id: int) -> Sequence[User]:
        stmt = (
            select(User)
            .join(Enrolment, Enrolment.user_id == User.id)
            .where(Enrolment.course_id == course_id, Enrolment.role == "student")
            .order_by(User.roll_number, User.name)
        )
        return (await self.db.execute(stmt)).scalars().all()


# ── Assignments ───────────────────────────────────────────────────────────────


class AssignmentRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, assignment_id: int) -> Assignment | None:
        stmt = select(Assignment).where(Assignment.id == assignment_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def create(self, **fields) -> Assignment:
        a = Assignment(**fields)
        self.db.add(a)
        await self.db.flush()
        return a

    async def for_course(self, course_id: int) -> Sequence[Assignment]:
        stmt = (
            select(Assignment)
            .where(Assignment.course_id == course_id)
            .order_by(Assignment.due_at)
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def for_student(self, user_id: int) -> Sequence[tuple[Assignment, Course]]:
        stmt = (
            select(Assignment, Course)
            .join(Course, Course.id == Assignment.course_id)
            .join(Enrolment, Enrolment.course_id == Course.id)
            .where(Enrolment.user_id == user_id, Enrolment.role == "student")
            .order_by(Assignment.due_at.desc())
        )
        return (await self.db.execute(stmt)).all()

    async def for_teacher(self, user_id: int) -> Sequence[tuple[Assignment, Course]]:
        stmt = (
            select(Assignment, Course)
            .join(Course, Course.id == Assignment.course_id)
            .where(Course.instructor_id == user_id)
            .order_by(Assignment.due_at.desc())
        )
        return (await self.db.execute(stmt)).all()

    async def dashboard_for_student(
        self, user_id: int
    ) -> Sequence[tuple[Assignment, Course, Submission | None]]:
        """Whole student dashboard in ONE query.

        The obvious implementation loops over assignments and issues a
        `latest_for` per row; at 10k concurrent students that N+1 turns a
        6 ms page into a connection-pool stampede.
        """
        latest = (
            select(
                Submission.assignment_id.label("assignment_id"),
                func.max(Submission.attempt).label("attempt"),
            )
            .where(Submission.student_id == user_id)
            .group_by(Submission.assignment_id)
            .subquery()
        )
        stmt = (
            select(Assignment, Course, Submission)
            .join(Course, Course.id == Assignment.course_id)
            .join(Enrolment, Enrolment.course_id == Course.id)
            .outerjoin(latest, latest.c.assignment_id == Assignment.id)
            .outerjoin(
                Submission,
                and_(
                    Submission.assignment_id == Assignment.id,
                    Submission.student_id == user_id,
                    Submission.attempt == latest.c.attempt,
                ),
            )
            .where(Enrolment.user_id == user_id, Enrolment.role == "student")
            .order_by(Assignment.due_at.desc())
        )
        return (await self.db.execute(stmt)).all()

    async def dashboard_for_teacher(self, user_id: int) -> Sequence[tuple]:
        """Assignments with submission/flag counts and roster size, one query."""
        stats = (
            select(
                Submission.assignment_id.label("assignment_id"),
                func.count(Submission.id).label("total"),
                func.coalesce(
                    func.sum(case((Submission.flagged.is_(True), 1), else_=0)), 0
                ).label("flagged"),
            )
            .where(Submission.status != "draft")
            .group_by(Submission.assignment_id)
            .subquery()
        )
        roster = (
            select(
                Enrolment.course_id.label("course_id"),
                func.count(Enrolment.id).label("students"),
            )
            .where(Enrolment.role == "student")
            .group_by(Enrolment.course_id)
            .subquery()
        )
        stmt = (
            select(
                Assignment, Course,
                func.coalesce(stats.c.total, 0),
                func.coalesce(stats.c.flagged, 0),
                func.coalesce(roster.c.students, 0),
            )
            .join(Course, Course.id == Assignment.course_id)
            .outerjoin(stats, stats.c.assignment_id == Assignment.id)
            .outerjoin(roster, roster.c.course_id == Course.id)
            .where(Course.instructor_id == user_id)
            .order_by(Assignment.due_at.desc())
        )
        return (await self.db.execute(stmt)).all()


# ── Submissions ───────────────────────────────────────────────────────────────


class SubmissionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, submission_id: int) -> Submission | None:
        return await self.db.get(Submission, submission_id)

    async def latest_for(
        self, assignment_id: int, student_id: int
    ) -> Submission | None:
        stmt = (
            select(Submission)
            .where(Submission.assignment_id == assignment_id, Submission.student_id == student_id)
            .order_by(Submission.attempt.desc(), Submission.id.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def with_students(self, assignment_id: int) -> Sequence[tuple[Submission, User | None]]:
        """Submissions joined to their student — avoids an N+1 on the roster."""
        stmt = (
            select(Submission, User)
            .outerjoin(User, User.id == Submission.student_id)
            .where(Submission.assignment_id == assignment_id, Submission.status != "draft")
            .order_by(User.roll_number, Submission.attempt)
        )
        return (await self.db.execute(stmt)).all()

    async def all_for_assignment(
        self, assignment_id: int, *, exclude_id: int | None = None, submitted_only: bool = True
    ) -> Sequence[Submission]:
        stmt = select(Submission).where(Submission.assignment_id == assignment_id)
        if submitted_only:
            stmt = stmt.where(Submission.status != "draft")
        if exclude_id is not None:
            stmt = stmt.where(Submission.id != exclude_id)
        return (await self.db.execute(stmt.order_by(Submission.id))).scalars().all()

    async def peer_corpora(
        self, course_id: int, *, exclude_assignment: int | None = None
    ) -> Sequence[Submission]:
        """Cross-assignment corpus inside the same course."""
        stmt = (
            select(Submission)
            .join(Assignment, Assignment.id == Submission.assignment_id)
            .where(Assignment.course_id == course_id, Submission.status != "draft")
        )
        if exclude_assignment is not None:
            stmt = stmt.where(Assignment.id != exclude_assignment)
        return (await self.db.execute(stmt.order_by(Submission.id))).scalars().all()

    async def add(self, **fields) -> Submission:
        s = Submission(**fields)
        self.db.add(s)
        await self.db.flush()
        return s

    async def set_analysis(self, submission_id: int, **fields) -> None:
        await self.db.execute(
            update(Submission).where(Submission.id == submission_id).values(**fields)
        )

    async def summary(self, assignment_id: int) -> dict:
        rows = (
            await self.db.execute(
                select(Submission).where(
                    Submission.assignment_id == assignment_id, Submission.status != "draft"
                )
            )
        ).scalars().all()
        analysed = [r for r in rows if r.analysis_status == "done"]
        sims = [r.similarity_score for r in analysed if r.similarity_score is not None]
        ais = [r.ai_score for r in analysed if r.ai_score is not None]
        return {
            "total": len(rows),
            "analysed": len(analysed),
            "pending": len([r for r in rows if r.analysis_status in ("pending", "running")]),
            "flagged": len([r for r in rows if r.flagged]),
            "mean_similarity": round(sum(sims) / len(sims), 4) if sims else None,
            "mean_ai_score": round(sum(ais) / len(ais), 4) if ais else None,
        }


# ── Drafts ────────────────────────────────────────────────────────────────────


class DraftRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def latest(self, assignment_id: int, student_id: int) -> DraftRevision | None:
        stmt = (
            select(DraftRevision)
            .where(
                DraftRevision.assignment_id == assignment_id,
                DraftRevision.student_id == student_id,
            )
            .order_by(DraftRevision.revision.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def history(
        self, assignment_id: int, student_id: int, limit: int = 25
    ) -> Sequence[DraftRevision]:
        stmt = (
            select(DraftRevision)
            .where(
                DraftRevision.assignment_id == assignment_id,
                DraftRevision.student_id == student_id,
            )
            .order_by(DraftRevision.revision.desc())
            .limit(limit)
        )
        return (await self.db.execute(stmt)).scalars().all()

    async def add(self, **fields) -> DraftRevision:
        d = DraftRevision(**fields)
        self.db.add(d)
        await self.db.flush()
        return d


class ReferenceRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def for_course(self, course_id: int) -> Sequence[ReferenceDocument]:
        stmt = select(ReferenceDocument).where(ReferenceDocument.course_id == course_id)
        return (await self.db.execute(stmt)).scalars().all()

    async def add(self, **fields) -> ReferenceDocument:
        r = ReferenceDocument(**fields)
        self.db.add(r)
        await self.db.flush()
        return r


class AuditRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def log(
        self,
        actor_id: int | None,
        action: str,
        subject_type: str | None = None,
        subject_id: int | None = None,
        **detail,
    ) -> None:
        self.db.add(
            AuditEvent(
                actor_id=actor_id,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                detail=detail or None,
            )
        )
        await self.db.flush()

    async def recent(self, limit: int = 100) -> Sequence[AuditEvent]:
        stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
        return (await self.db.execute(stmt)).scalars().all()


def as_select(stmt: Select):  # pragma: no cover - tiny helper
    return stmt
