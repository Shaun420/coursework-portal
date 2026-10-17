"""Folder and filename policy.

This module is the single source of truth for how files are laid out on
Google Drive, so a reviewer opening a teacher's Drive sees a predictable tree
and a filename that identifies the work without opening it.

Teacher (institutional) Drive
─────────────────────────────
    Submissions/
      2025-26 Sem 3/
        CS301 - Distributed Systems/
          A1 - Consensus Essay/
            submissions/
              21CS042_MeeraIyer_A1_v1.pdf
            reports/
              21CS042_MeeraIyer_A1_integrity_report.json
              A1_roster_summary.csv

Student (personal) Drive — unsubmitted work
───────────────────────────────────────────
    College Coursework/
      2025-26 Sem 3/
        CS301 - Distributed Systems/
          A1 - Consensus Essay/
            Drafts/
              A1_ConsensusEssay_draft_r03.docx
            Submitted/
              A1_ConsensusEssay_submitted_2026-01-12_v1.pdf
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_NAME_LEN = 150
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_DASHES = re.compile(r"\s*[-_\u2013\u2014]+\s*")
_SPACES = re.compile(r"\s+")


def slugify(text: str, max_len: int = MAX_NAME_LEN, sep: str = "") -> str:
    """Filesystem/Drive-safe token: ascii, no separators, no whitespace runs."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _ILLEGAL.sub("", text)
    text = _DASHES.sub("_", text)
    text = _SPACES.sub("_", text).strip("._-")
    if not text:
        text = "untitled"
    return text[:max_len].rstrip("._-") or "untitled"


def display_name(text: str, max_len: int = MAX_NAME_LEN) -> str:
    """Readable folder name: keeps spaces, strips only Drive-illegal chars."""
    text = _ILLEGAL.sub("", text or "").strip()
    text = _SPACES.sub(" ", text).strip(" .")
    return (text[:max_len].rstrip(" .") or "untitled")


def split_ext(filename: str) -> tuple[str, str]:
    stem, dot, ext = filename.rpartition(".")
    if not dot or not stem:
        return filename, ""
    return stem, "." + ext.lower()


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


# ── Teacher side ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CourseContext:
    academic_year: str
    semester: int
    course_code: str
    course_title: str

    @property
    def term_folder(self) -> str:
        return display_name(f"{self.academic_year} Sem {self.semester}")

    @property
    def course_folder(self) -> str:
        return display_name(f"{slugify(self.course_code)} - {self.course_title}")


@dataclass(frozen=True)
class AssignmentContext:
    code: str
    title: str

    @property
    def folder(self) -> str:
        return display_name(f"{slugify(self.code)} - {self.title}")


def submission_folder_path(ctx: CourseContext, asn: AssignmentContext) -> list[str]:
    return ["Submissions", ctx.term_folder, ctx.course_folder, asn.folder, "submissions"]


def report_folder_path(ctx: CourseContext, asn: AssignmentContext) -> list[str]:
    return ["Submissions", ctx.term_folder, ctx.course_folder, asn.folder, "reports"]


def submission_filename(
    roll_number: str | None,
    student_name: str,
    asn: AssignmentContext,
    attempt: int,
    original_filename: str,
) -> str:
    """`21CS042_MeeraIyer_A1_v1.pdf` — sortable by roll, unambiguous at a glance."""
    _, ext = split_ext(original_filename)
    roll = slugify(roll_number or "NOROLL", 24)
    name = slugify(student_name, 60)
    code = slugify(asn.code, 8)
    return f"{roll}_{name}_{code}_v{max(1, attempt)}{ext}"


def report_filename(
    roll_number: str | None, student_name: str, asn: AssignmentContext
) -> str:
    roll = slugify(roll_number or "NOROLL", 24)
    name = slugify(student_name, 60)
    return f"{roll}_{name}_{slugify(asn.code, 8)}_integrity_report.json"


def roster_summary_filename(asn: AssignmentContext) -> str:
    return f"{slugify(asn.code, 8)}_roster_summary.csv"


# ── Student side ──────────────────────────────────────────────────────────────

STUDENT_ROOT = "College Coursework"


def student_assignment_path(ctx: CourseContext, asn: AssignmentContext) -> list[str]:
    return [STUDENT_ROOT, ctx.term_folder, ctx.course_folder, asn.folder]


def draft_folder_path(ctx: CourseContext, asn: AssignmentContext) -> list[str]:
    return student_assignment_path(ctx, asn) + ["Drafts"]


def student_submitted_path(ctx: CourseContext, asn: AssignmentContext) -> list[str]:
    return student_assignment_path(ctx, asn) + ["Submitted"]


def draft_filename(
    asn: AssignmentContext, revision: int, original_filename: str
) -> str:
    """`A1_ConsensusEssay_draft_r03.docx` — a pinned snapshot of one revision."""
    _, ext = split_ext(original_filename)
    title = slugify(asn.title, 40)
    return f"{slugify(asn.code, 8)}_{title}_draft_r{revision:02d}{ext}"


def live_draft_filename(asn: AssignmentContext, original_filename: str) -> str:
    """`A1_ConsensusEssay_draft.docx` — the single in-place autosave file.

    One live file per assignment keeps the student's Drive uncluttered while
    Drive's own version history retains every autosave.
    """
    _, ext = split_ext(original_filename)
    title = slugify(asn.title, 40)
    return f"{slugify(asn.code, 8)}_{title}_draft{ext}"


def student_copy_filename(
    asn: AssignmentContext, submitted_on, attempt: int, original_filename: str
) -> str:
    _, ext = split_ext(original_filename)
    day = submitted_on.strftime("%Y-%m-%d") if submitted_on else "unsaved"
    return f"{slugify(asn.code, 8)}_{slugify(asn.title, 40)}_submitted_{day}_v{attempt}{ext}"


def portal_path(prefix: list[str], filename: str) -> str:
    return _join(*prefix, filename)
