"""Analysis service — the academic-integrity pipeline.

Flow for one submission:

1. read the staged blob, extract text (off the event loop),
2. fingerprint it (MinHash signature + verbatim block hashes),
3. build the comparison corpus: peers on the same assignment, peers elsewhere
   in the course, and any instructor reference material,
4. score similarity and run AI detection,
5. apply the 50 % thresholds → verdict + flag,
6. persist the report and publish it to the teacher's Drive with a refreshed
   roster CSV.

Steps 1–4 are CPU work and run in the pool's thread executor, so a 40-page PDF
never blocks the request-handling loop.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.db.repositories import (
    AssignmentRepository,
    AuditRepository,
    CourseRepository,
    ReferenceRepository,
    SubmissionRepository,
    UserRepository,
)
from app.db.session import session_scope
from app.domain.entities import DocumentFingerprint, Submission
from app.domain.enums import AnalysisStatus, Verdict
from app.services.detection.base import get_detector
from app.services.drive.service import get_drive_sync
from app.services.similarity.engine import CorpusDocument, Fingerprint, SimilarityEngine
from app.services.similarity.extractor import extract
from app.services.similarity.minhash import pack_signature, unpack_signature
from app.services.similarity.normalize import tokenize
from app.services.workers.pipeline import get_pipeline

log = get_logger(__name__)

FINGERPRINT_CACHE_SIZE = 8000


class _LRU(OrderedDict):
    """Tiny bounded LRU — fingerprints are ~5 KB, so this is a real memory bound."""

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self.maxsize = maxsize

    def get_or_none(self, key):
        if key not in self:
            return None
        self.move_to_end(key)
        return self[key]

    def put(self, key, value) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.maxsize:
            self.popitem(last=False)


_fp_cache: _LRU = _LRU(FINGERPRINT_CACHE_SIZE)
_ref_cache: _LRU = _LRU(512)


def clear_caches() -> None:
    _fp_cache.clear()
    _ref_cache.clear()


@dataclass(slots=True)
class AnalysisOutcome:
    submission_id: int
    similarity_score: float | None
    ai_score: float | None
    verdict: str
    flagged: bool
    analysis_status: str
    report: dict
    drive_report_path: str | None = None
    error: str | None = None


def decide_verdict(
    similarity: float | None, ai_score: float | None
) -> tuple[Verdict, bool]:
    """Apply the configured thresholds (default 50 %) to the two scores."""
    sim_flag = similarity is not None and similarity >= settings.similarity_threshold
    ai_flag = ai_score is not None and ai_score >= settings.ai_detection_threshold
    if sim_flag and ai_flag:
        return Verdict.BOTH_FLAG, True
    if sim_flag:
        return Verdict.SIMILARITY_FLAG, True
    if ai_flag:
        return Verdict.AI_FLAG, True
    return Verdict.CLEAN, False


def _read_blob(path: str | None) -> bytes:
    if not path:
        return b""
    p = Path(path)
    if not p.exists():
        log.warning("staged blob missing: %s", path)
        return b""
    return p.read_bytes()


def _fingerprint_from_row(row: DocumentFingerprint) -> Fingerprint:
    return Fingerprint(
        normalized="",
        signature=list(unpack_signature(_b64(row.signature)) or []),
        blocks=set(row.blocks or []),
        word_count=row.word_count,
        shingle_count=row.shingle_count,
    )


def _b64(raw: bytes) -> str | None:
    import base64

    return base64.b64encode(raw).decode() if raw else None


def _pack(signature: list[int]) -> bytes:
    import base64

    return base64.b64decode(pack_signature(signature).encode())


class AnalysisRunner:
    def __init__(self, db, pool=None) -> None:  # noqa: ANN001 - AsyncSession at runtime
        self.db = db
        self.pool = pool or get_pipeline().pool
        self.engine = SimilarityEngine()
        self.subs = SubmissionRepository(db)
        self.assignments = AssignmentRepository(db)
        self.courses = CourseRepository(db)
        self.users = UserRepository(db)
        self.refs = ReferenceRepository(db)
        self.audit = AuditRepository(db)

    # ── fingerprint resolution ──────────────────────────────────────────────

    async def _fingerprint_for(self, sub: Submission) -> Fingerprint | None:
        cached = _fp_cache.get_or_none(sub.id)
        if cached is not None:
            return cached

        row = await self.db.get(DocumentFingerprint, sub.id)
        if row is not None:
            fp = _fingerprint_from_row(row)
            _fp_cache.put(sub.id, fp)
            return fp

        if not sub.blob_path:
            return None
        data = await self.pool.run_blocking(_read_blob, sub.blob_path)
        if not data:
            return None
        extracted = await self.pool.run_blocking(extract, sub.filename, data)
        if not extracted.ok:
            return None
        fp = await self.pool.run_blocking(self.engine.fingerprint, extracted.text)
        _fp_cache.put(sub.id, fp)
        return fp

    async def _reference_documents(self, course_id: int) -> list[CorpusDocument]:
        docs: list[CorpusDocument] = []
        for ref in await self.refs.for_course(course_id):
            cached = _ref_cache.get_or_none(ref.sha256)
            if cached is None:
                fp = await self.pool.run_blocking(self.engine.fingerprint, ref.body_text)
                cached = CorpusDocument(
                    doc_id=f"ref:{ref.id}",
                    label=f"reference: {ref.title}",
                    signature=fp.signature,
                    blocks=fp.blocks,
                    word_count=fp.word_count,
                    kind="reference",
                )
                _ref_cache.put(ref.sha256, cached)
            docs.append(cached)
        return docs

    async def _corpus(self, submission: Submission, assignment) -> list[CorpusDocument]:
        peers: list[Submission] = list(
            await self.subs.all_for_assignment(assignment.id, exclude_id=submission.id)
        )
        peers.extend(
            await self.subs.peer_corpora(assignment.course_id, exclude_assignment=assignment.id)
        )

        seen: set[int] = set()
        corpus: list[CorpusDocument] = []
        for peer in peers:
            if peer.id in seen:
                continue
            seen.add(peer.id)
            fp = await self._fingerprint_for(peer)
            if fp is None:
                continue
            student = await self.users.get(peer.student_id)
            label = (
                f"{student.roll_number or 'n/a'} {student.name}"
                if student
                else f"student#{peer.student_id}"
            )
            corpus.append(
                CorpusDocument(
                    doc_id=f"sub:{peer.id}",
                    label=label,
                    signature=fp.signature,
                    blocks=fp.blocks,
                    word_count=fp.word_count,
                    kind="peer",
                )
            )
        corpus.extend(await self._reference_documents(assignment.course_id))
        return corpus

    # ── main entry point ────────────────────────────────────────────────────

    async def run(self, submission_id: int) -> AnalysisOutcome:
        sub = await self.subs.get(submission_id)
        if sub is None:
            raise LookupError(f"submission {submission_id} does not exist")

        assignment = await self.assignments.get(sub.assignment_id)
        course = await self.courses.get(assignment.course_id)
        student = await self.users.get(sub.student_id)
        instructor = await self.users.get(course.instructor_id)

        await self.subs.set_analysis(submission_id, analysis_status=AnalysisStatus.RUNNING.value)
        await self.db.flush()

        data = await self.pool.run_blocking(_read_blob, sub.blob_path)
        extracted = await self.pool.run_blocking(extract, sub.filename, data) if data else None
        sha256 = hashlib.sha256(data).hexdigest() if data else sub.sha256

        report: dict = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "submission_id": submission_id,
            "assignment": {
                "id": assignment.id, "code": assignment.code, "title": assignment.title,
            },
            "course": {"code": course.code, "title": course.title},
            "student": {
                "roll_number": student.roll_number,
                "name": student.name,
                "email": student.email,
            },
            "document": {
                "filename": sub.filename,
                "bytes": len(data),
                "sha256": sha256,
                "extraction_method": extracted.method if extracted else None,
                "extraction_error": extracted.error if extracted else None,
            },
            "thresholds": {
                "similarity": settings.similarity_threshold,
                "ai_detection": settings.ai_detection_threshold,
            },
        }

        similarity_score: float | None = None
        ai_score: float | None = None
        error: str | None = None

        if extracted is None or not extracted.ok:
            error = (
                (extracted.error if extracted else "no text could be extracted")
                or "empty document"
            )
            report["similarity"] = None
            report["ai_detection"] = None
            report["note"] = (
                "No analysable text was found in this file, so no integrity score "
                "was produced. A reviewer should open it manually."
            )
            analysis_status = AnalysisStatus.SKIPPED.value
            word_count = 0
            verdict_value: str | None = None
            flagged = False
        else:
            fp = await self.pool.run_blocking(self.engine.fingerprint, extracted.text)
            corpus = await self._corpus(sub, assignment)

            sim = await self.pool.run_blocking(self.engine.compare, fp, corpus)
            detection = await get_detector().score(extracted.text)

            similarity_score = sim.similarity_index
            ai_score = detection["ai_score"]
            verdict, flagged = decide_verdict(similarity_score, ai_score)
            verdict_value = verdict.value
            word_count = fp.word_count
            analysis_status = AnalysisStatus.DONE.value

            report["similarity"] = sim.as_report()
            report["ai_detection"] = detection
            report["verdict"] = verdict_value
            report["flagged"] = flagged
            report["provenance"] = {
                "peer_documents_compared": sim.comparisons,
                "corpus_size": len(corpus),
                "reference_documents": len([c for c in corpus if c.kind == "reference"]),
                "min_words_for_reliable_ai_score": settings.ai_detection_min_words,
            }

            await self.db.merge(
                DocumentFingerprint(
                    submission_id=submission_id,
                    signature=_pack(fp.signature),
                    blocks=sorted(fp.blocks),
                    word_count=fp.word_count,
                    shingle_count=fp.shingle_count,
                )
            )
            _fp_cache.put(submission_id, fp)

        report["analysis_status"] = analysis_status
        if error:
            report["error"] = error

        await self.subs.set_analysis(
            submission_id,
            analysis_status=analysis_status,
            similarity_score=similarity_score,
            ai_score=ai_score,
            verdict=verdict_value,
            flagged=flagged,
            word_count=word_count,
            report=report,
            analysis_error=error,
            analysed_at=datetime.now(UTC),
        )
        await self.audit.log(
            student.id if student else None,
            "submission.analysed",
            "submission",
            submission_id,
            similarity=similarity_score,
            ai=ai_score,
            flagged=flagged,
        )
        await self.db.flush()

        drive_path = await self._publish_to_drive(
            sub=sub, course=course, assignment=assignment,
            instructor=instructor, student=student, report=report,
        )

        log.info(
            "analysed submission=%s similarity=%s ai=%s verdict=%s flagged=%s",
            submission_id,
            f"{similarity_score:.3f}" if similarity_score is not None else "n/a",
            f"{ai_score:.3f}" if ai_score is not None else "n/a",
            verdict_value, flagged,
        )
        return AnalysisOutcome(
            submission_id=submission_id,
            similarity_score=similarity_score,
            ai_score=ai_score,
            verdict=verdict_value or "skipped",
            flagged=flagged,
            analysis_status=analysis_status,
            report=report,
            drive_report_path=drive_path,
            error=error,
        )

    async def _publish_to_drive(
        self, *, sub, course, assignment, instructor, student, report  # noqa: ANN001
    ) -> str | None:
        """Push the report next to the submission on the teacher's Drive."""
        drive = get_drive_sync()
        try:
            rows = await self._roster_rows(assignment.id)
            result = await drive.publish_integrity_report(
                course=course,
                assignment=assignment,
                instructor=instructor,
                student=student,
                report=report,
                roster_rows=rows,
            )
            return result.report_path
        except Exception as exc:  # the DB record already exists — never lose it
            log.warning("could not publish integrity report to Drive: %s", exc)
            return None

    async def _roster_rows(self, assignment_id: int) -> list[dict]:
        out = []
        for s, student in await self.subs.with_students(assignment_id):
            out.append(
                {
                    "roll_number": student.roll_number if student else "",
                    "student": student.name if student else "",
                    "email": student.email if student else "",
                    "attempt": s.attempt,
                    "submitted_at": (
                        s.submitted_at.isoformat(timespec="seconds") if s.submitted_at else ""
                    ),
                    "filename": s.archived_name or s.filename,
                    "similarity_score": (
                        round(s.similarity_score, 4) if s.similarity_score is not None else ""
                    ),
                    "ai_score": round(s.ai_score, 4) if s.ai_score is not None else "",
                    "flagged": "yes" if s.flagged else "no",
                    "verdict": s.verdict or "",
                    "teacher_file": s.teacher_file_id or "",
                    "marks": s.marks_awarded if s.marks_awarded is not None else "",
                }
            )
        return out


async def analyse_submission(submission_id: int) -> AnalysisOutcome:
    """Entry point used by the worker pool; owns its own DB session."""
    async with session_scope() as db:
        return await AnalysisRunner(db).run(submission_id)


def public_report(report: dict | None) -> dict | None:
    """Strip internals before a report goes to a browser or to Drive."""
    if not report:
        return None
    slim = json.loads(json.dumps(report, default=str))
    slim.pop("fingerprint", None)
    return slim


def token_count(text: str) -> int:
    return len(tokenize(text))
