"""Pydantic DTOs — the contract between the API and the browser."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Users ─────────────────────────────────────────────────────────────────────


class UserOut(ORMModel):
    id: int
    email: str
    name: str
    role: str
    roll_number: str | None = None
    picture_url: str | None = None
    drive_linked: bool
    drive_email: str | None = None


# ── Courses / assignments ─────────────────────────────────────────────────────


class CourseCreate(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    title: str = Field(min_length=2, max_length=200)
    academic_year: str = "2025-26"
    semester: int = Field(default=1, ge=1, le=8)


class CourseOut(ORMModel):
    id: int
    code: str
    title: str
    academic_year: str
    semester: int
    instructor_id: int
    teacher_folder_id: str | None = None


class AssignmentCreate(BaseModel):
    course_id: int
    code: str = Field(default="A1", min_length=1, max_length=16)
    title: str = Field(min_length=2, max_length=200)
    brief: str = ""
    max_marks: int = Field(default=100, ge=1, le=1000)
    opens_at: datetime | None = None
    due_at: datetime
    allow_late: bool = True
    allow_resubmit: bool = True


class AssignmentOut(ORMModel):
    id: int
    course_id: int
    code: str
    title: str
    brief: str
    max_marks: int
    opens_at: datetime
    due_at: datetime
    allow_late: bool
    allow_resubmit: bool


class AssignmentListItem(AssignmentOut):
    course_code: str | None = None
    course_title: str | None = None
    submission_count: int = 0
    student_count: int = 0
    my_status: str | None = None
    my_similarity: float | None = None
    my_ai_score: float | None = None
    flagged_count: int = 0


# ── Drafts (unsubmitted work → student's own Drive) ───────────────────────────


class DraftSaveOut(ORMModel):
    id: int
    revision: int
    kind: str
    filename: str
    size_bytes: int
    drive_file_id: str | None = None
    drive_path: str | None = None
    saved_at: datetime


# ── Submissions ───────────────────────────────────────────────────────────────


class SubmissionOut(ORMModel):
    id: int
    assignment_id: int
    student_id: int
    attempt: int
    status: str
    filename: str
    size_bytes: int
    similarity_score: float | None = None
    ai_score: float | None = None
    verdict: str | None = None
    flagged: bool = False
    word_count: int = 0
    analysis_status: str
    teacher_file_id: str | None = None
    archived_name: str | None = None
    submitted_at: datetime | None = None
    analysed_at: datetime | None = None
    marks_awarded: float | None = None
    feedback: str | None = None


class SubmissionWithStudent(SubmissionOut):
    student_name: str | None = None
    student_email: str | None = None
    roll_number: str | None = None


class ReportOut(BaseModel):
    submission_id: int
    assignment_title: str
    student_name: str
    roll_number: str | None
    submitted_at: datetime | None
    status: str
    analysis_status: str
    similarity_score: float | None
    similarity_threshold: float
    ai_score: float | None
    ai_threshold: float
    verdict: str | None
    flagged: bool
    word_count: int
    report: dict | None = None


class GradeIn(BaseModel):
    marks: float = Field(ge=0, le=1000)
    feedback: str = ""


class AnalysisSummary(BaseModel):
    total: int
    analysed: int
    pending: int
    flagged: int
    mean_similarity: float | None = None
    mean_ai_score: float | None = None


# ── System ────────────────────────────────────────────────────────────────────


class HealthOut(BaseModel):
    status: str
    env: str
    database: str
    drive_provider: str
    queue_depth: int
    queue_capacity: int
    uptime_seconds: float


class MetricsOut(BaseModel):
    requests_total: int
    requests_in_flight: int
    errors_total: int
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    cpu_percent: float
    rss_mb: float
    threads: int
    db_pool_checked_out: int
    queue_depth: int
    analysis_completed: int
    analysis_failed: int
