"""DriveSyncService — the only place that touches Google Drive.

Two responsibilities, matching the two Drive accounts in the system:

* **Student Drive** — unsubmitted work is autosaved into the student's own
  account, one live draft file per assignment, updated in place so Drive's own
  revision history carries the trail.
* **Teacher Drive** — every submission is archived into a deterministic folder
  tree with a self-describing filename, alongside a JSON integrity report and a
  regenerated roster CSV.

Folder ids are memoised per (account, path) because resolving a path is
O(depth) API calls, and 10k students submitting near a deadline would
otherwise hammer the same lookups.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.core.errors import NotLinkedError
from app.core.logging import get_logger
from app.domain.entities import Assignment, Course, User
from app.services.drive import get_drive_provider
from app.services.drive.base import DriveAccount
from app.services.drive.paths import (
    AssignmentContext,
    CourseContext,
    draft_filename,
    draft_folder_path,
    live_draft_filename,
    portal_path,
    report_filename,
    report_folder_path,
    roster_summary_filename,
    student_copy_filename,
    student_submitted_path,
    submission_filename,
    submission_folder_path,
)

log = get_logger(__name__)

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".tex": "application/x-tex",
    ".html": "text/html",
    ".json": "application/json",
    ".csv": "text/csv",
}


def mime_for(filename: str, fallback: str = "application/octet-stream") -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return MIME_BY_EXT.get(ext, fallback)


@dataclass(slots=True)
class DraftSyncResult:
    drive_file_id: str | None
    drive_path: str
    web_view_link: str | None
    provider: str


@dataclass(slots=True)
class ArchiveResult:
    teacher_account_email: str
    teacher_file_id: str | None
    teacher_folder_id: str | None
    archived_name: str
    archived_path: str
    student_copy_id: str | None
    web_view_link: str | None
    degraded: bool
    notes: list[str]


@dataclass(slots=True)
class ReportPublishResult:
    report_file_id: str | None
    report_path: str
    summary_file_id: str | None
    summary_path: str
    degraded: bool


class DriveSyncService:
    def __init__(self, provider=None) -> None:
        self.provider = provider or get_drive_provider()
        self._folder_memo: dict[tuple[str, str, str], str] = {}

    # ── accounts ────────────────────────────────────────────────────────────

    @staticmethod
    def student_account(user: User) -> DriveAccount:
        if not user.drive_linked:
            raise NotLinkedError(
                "This portal account is not linked to a Google account yet.",
                user_id=user.id,
            )
        return DriveAccount(
            user_id=user.id,
            email=user.drive_email or user.email,
            refresh_token=decrypt_secret(user.refresh_token),
            expires_at=user.token_expires_at,
        )

    @staticmethod
    def institution_account() -> DriveAccount:
        return DriveAccount(
            user_id=0,
            email=settings.institution_email,
            refresh_token=settings.institution_refresh_token or "institution-mock-token",
        )

    @classmethod
    def archive_account(cls, instructor: User | None) -> tuple[DriveAccount, bool]:
        """Where submissions land: the instructor's own Drive if linked, else
        the institution Drive. Returns ``(account, degraded)``."""
        if instructor is not None and instructor.drive_linked:
            return cls.student_account(instructor), False
        return cls.institution_account(), True

    # ── paths ───────────────────────────────────────────────────────────────

    @staticmethod
    def course_context(course: Course) -> CourseContext:
        return CourseContext(
            academic_year=course.academic_year,
            semester=course.semester,
            course_code=course.code,
            course_title=course.title,
        )

    @staticmethod
    def assignment_context(assignment: Assignment) -> AssignmentContext:
        return AssignmentContext(code=assignment.code, title=assignment.title)

    async def _resolve_path(
        self, account: DriveAccount, parts: list[str], root_id: str = "root"
    ) -> str:
        """Create-or-find each path segment, returning the deepest folder id."""
        cache_key = (account.email, root_id, "/".join(parts))
        if cache_key in self._folder_memo:
            return self._folder_memo[cache_key]

        current = root_id
        for part in parts:
            current = await self.provider.ensure_folder(account, current, part)
        self._folder_memo[cache_key] = current
        return current

    def clear_folder_cache(self) -> None:
        self._folder_memo.clear()

    # ── Student Drive: unsubmitted work ─────────────────────────────────────

    async def save_draft(
        self,
        *,
        student: User,
        course: Course,
        assignment: Assignment,
        filename: str,
        data: bytes,
        revision: int,
        live_file_id: str | None,
        snapshot: bool = False,
        body_text: str | None = None,
    ) -> DraftSyncResult:
        """Autosave (or explicitly snapshot) unsubmitted work on the student's Drive."""
        account = self.student_account(student)
        ctx = self.course_context(course)
        actx = self.assignment_context(assignment)

        folder_id = await self._resolve_path(account, draft_folder_path(ctx, actx))
        mime = mime_for(filename)

        if snapshot:
            name = draft_filename(actx, revision, filename)
            created = await self.provider.upload(
                account,
                folder_id,
                name,
                data,
                mime,
                description=(
                    f"Draft r{revision} — {ctx.course_code} {actx.code} — not submitted"
                ),
            )
        elif live_file_id:
            created = await self.provider.update(
                account, live_file_id, data, mime, new_name=live_draft_filename(actx, filename)
            )
        else:
            created = await self.provider.upload(
                account,
                folder_id,
                live_draft_filename(actx, filename),
                data,
                mime,
                description=f"Live draft — {ctx.course_code} {actx.code} — not submitted",
            )

        path = portal_path(draft_folder_path(ctx, actx), created.name)
        log.info(
            "draft r%s saved for user=%s assignment=%s -> %s (%s bytes)",
            revision, student.id, assignment.id, path, len(data),
        )
        return DraftSyncResult(
            drive_file_id=created.id,
            drive_path=path,
            web_view_link=created.web_view_link,
            provider=self.provider.name,
        )

    async def ensure_student_folders(
        self, student: User, course: Course, assignment: Assignment
    ) -> str:
        account = self.student_account(student)
        ctx = self.course_context(course)
        actx = self.assignment_context(assignment)
        return await self._resolve_path(account, student_submitted_path(ctx, actx))

    # ── Teacher Drive: submitted work ───────────────────────────────────────

    async def archive_submission(
        self,
        *,
        course: Course,
        assignment: Assignment,
        instructor: User | None,
        student: User,
        attempt: int,
        filename: str,
        data: bytes,
        submitted_at: datetime | None = None,
    ) -> ArchiveResult:
        """Upload the submitted file into the teacher's organised folder tree.

        This happens synchronously on submit — before analysis — so a submitted
        assignment is on the teacher's Drive even if the analysis queue is
        backed up or the box restarts.
        """
        account, degraded = self.archive_account(instructor)
        ctx = self.course_context(course)
        actx = self.assignment_context(assignment)
        notes: list[str] = []

        root = settings.teacher_drive_root_id or "root"
        sub_folder = await self._resolve_path(
            account, submission_folder_path(ctx, actx), root_id=root
        )

        archived_name = submission_filename(
            student.roll_number, student.name, actx, attempt, filename
        )
        description = (
            f"{ctx.course_code} {actx.code} | {student.roll_number or 'n/a'} "
            f"{student.name} | attempt {attempt} | submitted "
            f"{(submitted_at or datetime.now(UTC)).isoformat(timespec='seconds')}"
        )
        created = await self.provider.upload(
            account, sub_folder, archived_name, data, mime_for(filename), description=description
        )
        if degraded:
            notes.append(
                f"instructor Drive not linked — archived to institutional Drive "
                f"({settings.institution_email})"
            )

        # Give the student a personal copy of what was submitted, for their records.
        student_copy_id: str | None = None
        try:
            student_account = self.student_account(student)
            student_folder = await self._resolve_path(
                student_account, student_submitted_path(ctx, actx)
            )
            copy_name = student_copy_filename(actx, submitted_at, attempt, filename)
            student_copy = await self.provider.upload(
                student_account,
                student_folder,
                copy_name,
                data,
                mime_for(filename),
                description="Your submitted copy (record of submission)",
            )
            student_copy_id = student_copy.id
        except NotLinkedError:
            notes.append("student Drive not linked — no personal copy created")
        except Exception as exc:  # never fail a submission over a courtesy copy
            log.warning("student courtesy copy failed: %s", exc)
            notes.append(f"student copy failed: {exc}")

        archived_path = portal_path(submission_folder_path(ctx, actx), archived_name)
        log.info(
            "archived submission assignment=%s student=%s attempt=%s -> %s "
            "(teacher=%s, degraded=%s)",
            assignment.id, student.id, attempt, archived_path, account.email, degraded,
        )
        return ArchiveResult(
            teacher_account_email=account.email,
            teacher_file_id=created.id,
            teacher_folder_id=sub_folder,
            archived_name=archived_name,
            archived_path=archived_path,
            student_copy_id=student_copy_id,
            web_view_link=created.web_view_link,
            degraded=degraded,
            notes=notes,
        )

    async def publish_integrity_report(
        self,
        *,
        course: Course,
        assignment: Assignment,
        instructor: User | None,
        student: User,
        report: dict,
        roster_rows: list[dict],
    ) -> ReportPublishResult:
        """Write the JSON integrity report and refresh the roster CSV.

        Called once analysis finishes, so the teacher's Drive holds the scores
        next to the work they describe.
        """
        account, degraded = self.archive_account(instructor)
        ctx = self.course_context(course)
        actx = self.assignment_context(assignment)
        root = settings.teacher_drive_root_id or "root"

        rep_folder = await self._resolve_path(
            account, report_folder_path(ctx, actx), root_id=root
        )

        report_name = report_filename(student.roll_number, student.name, actx)
        report_payload = json.dumps(report, indent=2, sort_keys=True, default=str).encode()
        existing_report = await self.provider.find_child(account, rep_folder, report_name)
        if existing_report:
            report_file = await self.provider.update(
                account, existing_report.id, report_payload, "application/json",
                new_name=report_name,
            )
        else:
            report_file = await self.provider.upload(
                account, rep_folder, report_name, report_payload, "application/json",
                description=(
                    f"Similarity + AI detection report — "
                    f"{student.roll_number or 'n/a'} {student.name}"
                ),
            )

        summary_name = roster_summary_filename(actx)
        csv_bytes = _roster_csv(roster_rows)
        existing_summary = await self.provider.find_child(account, rep_folder, summary_name)
        if existing_summary:
            summary_file = await self.provider.update(
                account, existing_summary.id, csv_bytes, "text/csv", new_name=summary_name
            )
        else:
            summary_file = await self.provider.upload(
                account, rep_folder, summary_name, csv_bytes, "text/csv",
                description=f"Auto-generated roster summary for {ctx.course_code} {actx.code}",
            )

        log.info(
            "published integrity report assignment=%s student=%s -> %s",
            assignment.id, student.id, portal_path(report_folder_path(ctx, actx), report_name),
        )
        return ReportPublishResult(
            report_file_id=report_file.id,
            report_path=portal_path(report_folder_path(ctx, actx), report_name),
            summary_file_id=summary_file.id,
            summary_path=portal_path(report_folder_path(ctx, actx), summary_name),
            degraded=degraded,
        )

    # ── Inspection helpers (used by the /drive tree pages and tests) ────────

    async def tree(
        self,
        account: DriveAccount,
        root_id: str = "root",
        depth: int = 6,
        _prefix: str = "",
    ) -> list[dict]:
        if depth <= 0:
            return []
        out: list[dict] = []
        for f in await self.provider.list_children(account, root_id):
            out.append(
                {
                    "name": f.name,
                    "path": f"{_prefix}/{f.name}",
                    "type": "folder" if f.is_folder else "file",
                    "size": f.size,
                    "id": f.id,
                    "link": f.web_view_link,
                }
            )
            if f.is_folder:
                out.extend(
                    await self.tree(account, f.id, depth - 1, f"{_prefix}/{f.name}")
                )
        return out


def _roster_csv(rows: list[dict]) -> bytes:
    """Deterministic CSV so repeated regenerations diff cleanly."""
    headers = [
        "roll_number", "student", "email", "attempt", "submitted_at",
        "filename", "similarity_score", "ai_score", "flagged", "verdict",
        "teacher_file", "marks",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in sorted(rows, key=lambda r: str(r.get("roll_number") or "zzz")):
        writer.writerow({h: row.get(h, "") for h in headers})
    return buf.getvalue().encode()


_drive_sync: DriveSyncService | None = None


def get_drive_sync() -> DriveSyncService:
    global _drive_sync
    if _drive_sync is None:
        _drive_sync = DriveSyncService()
    return _drive_sync
