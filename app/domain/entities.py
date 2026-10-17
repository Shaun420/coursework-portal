"""SQLAlchemy 2.0 declarative models.

Deliberately plain tables with explicit FKs and covering indexes: the read
patterns of this app (dashboard by user, roster by course, submission by
assignment+student) are all index-served, which is what lets one machine hold
10k concurrent readers.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime. SQLite hands back naive values."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes that survive a SQLite round-trip.

    SQLite has no datetime type and drops the offset, so a naive value comes
    back and every comparison against `datetime.now(timezone.utc)` raises.
    Normalising on both sides of the boundary removes that whole class of bug.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        value = as_utc(value)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):  # noqa: ANN001
        return as_utc(value)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    google_sub: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    picture_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="student", index=True)
    roll_number: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    # Drive linkage
    drive_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    drive_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    drive_root_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    enrolments: Mapped[list[Enrolment]] = relationship(back_populates="user", lazy="selectin")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(200))
    academic_year: Mapped[str] = mapped_column(String(16), default="2025-26")
    semester: Mapped[int] = mapped_column(Integer, default=1)
    instructor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    teacher_folder_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("code", "academic_year", "semester", name="uq_course_term"),)

    instructor: Mapped[User] = relationship()
    enrolments: Mapped[list[Enrolment]] = relationship(back_populates="course", lazy="selectin")


class Enrolment(Base):
    __tablename__ = "enrolments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="student")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "course_id", name="uq_enrolment"),)

    user: Mapped[User] = relationship(back_populates="enrolments")
    course: Mapped[Course] = relationship(back_populates="enrolments")


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    code: Mapped[str] = mapped_column(String(16), default="A1")
    title: Mapped[str] = mapped_column(String(200))
    brief: Mapped[str] = mapped_column(Text, default="")
    max_marks: Mapped[int] = mapped_column(Integer, default=100)
    opens_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    allow_late: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_resubmit: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("course_id", "code", name="uq_assignment_code"),
        Index("ix_assignment_due", "course_id", "due_at"),
    )

    course: Mapped[Course] = relationship()
    submissions: Mapped[list[Submission]] = relationship(back_populates="assignment")


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[str] = mapped_column(String(24), default="submitted", index=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)

    # Local staging copy (source of truth for analysis; Drive is the archive).
    blob_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Drive references
    student_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    teacher_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    teacher_folder_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archived_name: Mapped[str | None] = mapped_column(String(400), nullable=True)

    # Analysis results
    analysis_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(24), nullable=True)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    analysis_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    marks_awarded: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    analysed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        Index("ix_submission_lookup", "assignment_id", "student_id", "attempt"),
    )

    assignment: Mapped[Assignment] = relationship(back_populates="submissions")
    student: Mapped[User] = relationship()


class DraftRevision(Base):
    """Unsubmitted work autosaved onto the *student's own* Drive."""

    __tablename__ = "draft_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(16), default="autosave")

    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    drive_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    drive_path: Mapped[str | None] = mapped_column(String(600), nullable=True)
    saved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("assignment_id", "student_id", "revision", name="uq_draft_rev"),
        Index("ix_draft_lookup", "assignment_id", "student_id"),
    )


class ReferenceDocument(Base):
    """Instructor-supplied material that submissions are also compared against."""

    __tablename__ = "reference_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body_text: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(255), default="upload")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DocumentFingerprint(Base):
    """MinHash signature + verbatim block hashes for one submission.

    Kept in its own table so the integrity report stays small enough to send
    to a browser, and so building the LSH index for a course is a single
    indexed SELECT instead of re-reading and re-parsing every uploaded file.
    """

    __tablename__ = "document_fingerprints"

    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id"), primary_key=True
    )
    signature: Mapped[bytes] = mapped_column(LargeBinary)
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    shingle_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
