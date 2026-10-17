"""Enumerations shared across the domain."""

from __future__ import annotations

import enum


class Role(enum.StrEnum):
    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"


class SubmissionStatus(enum.StrEnum):
    DRAFT = "draft"            # exists only as an autosaved file on the student's Drive
    SUBMITTED = "submitted"    # archived to the teacher's Drive, awaiting analysis
    ANALYSING = "analysing"
    ANALYSED = "analysed"
    ANALYSIS_FAILED = "analysis_failed"
    LATE = "late"
    RETURNED = "returned"


class AnalysisStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"       # e.g. document too short to score


class Verdict(enum.StrEnum):
    """Human-readable outcome derived from the two scores and the threshold."""

    CLEAN = "clean"
    SIMILARITY_FLAG = "similarity_flag"
    AI_FLAG = "ai_flag"
    BOTH_FLAG = "both_flag"


class DraftKind(enum.StrEnum):
    MANUAL = "manual"
    AUTOSAVE = "autosave"
