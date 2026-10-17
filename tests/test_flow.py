"""End-to-end: draft → submit → analyse → report on the teacher's Drive."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.domain.entities import DocumentFingerprint
from app.services.drive.service import get_drive_sync
from app.services.workers.pipeline import get_pipeline

from .conftest import HUMAN_ESSAY, HUMAN_VARIANT, UNRELATED_ESSAY

TEACHER = "prof.rao@college.edu"


class Sessions:
    """Two independent browsers sharing one ASGI app."""

    def __init__(self, app):
        self.app = app
        self._stack: list[httpx.AsyncClient] = []

    async def open(self, email: str, name: str, roll: str = ""):
        from .conftest import ApiClient

        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self._stack.append(http)
        client = ApiClient(http)
        await client.login(email, name, roll)
        return client

    async def aclose(self) -> None:
        for http in self._stack:
            await http.aclose()
        self._stack.clear()


async def _user_by_email(email: str):
    from app.db.repositories import UserRepository
    from app.db.session import session_scope

    async with session_scope() as db:
        return await UserRepository(db).by_email(email)


@pytest.fixture
async def sessions(app, _bootstrap):
    box = Sessions(app)
    try:
        yield box
    finally:
        await box.aclose()


async def _make_assignment(teacher, code: str, title: str) -> tuple[int, int]:
    _r = await teacher.post(
        "/api/courses",
        json={"code": code, "title": title, "academic_year": "2025-26", "semester": 3},
    )
    if _r.status_code != 201:
        raise AssertionError(f"course create {_r.status_code}: {_r.text}")
    course = _r.json()
    assignment = (await teacher.post(
        "/api/assignments",
        json={
            "course_id": course["id"], "code": "A1", "title": "Consensus Essay",
            "brief": "Explain the two generals problem and what Paxos gives up.",
            "max_marks": 100, "due_at": "2099-01-01T00:00:00Z",
            "allow_late": True, "allow_resubmit": True,
        },
    )).json()
    return course["id"], assignment["id"]


@pytest.mark.asyncio
async def test_draft_autosaves_to_the_students_own_drive(sessions, mock_provider):
    teacher = await sessions.open(TEACHER, "Prof. Rao", "STAFF01")
    _course_id, aid = await _make_assignment(teacher, "CS310", "Networking")
    me = (await teacher.get("/auth/me")).json()
    assert me["user"]["role"] in ("teacher", "admin"), me["user"]

    student = await sessions.open("meera@college.edu", "Meera Iyer", "21CS042")
    assert (await student.post(f"/api/courses/{_course_id}/enrol")).status_code == 200

    res = await student.post(
        f"/api/assignments/{aid}/draft",
        json={"text": HUMAN_ESSAY, "filename": "draft.txt"},
    )
    assert res.status_code == 200, res.text
    draft = res.json()
    assert draft["revision"] == 1
    assert draft["drive_path"].endswith("Drafts/A1_Consensus_Essay_draft.txt")
    assert draft["drive_path"].startswith("College Coursework/")

    # The file really is on the student's (simulated) Drive.
    tree = (await student.get("/api/drive/tree/mine")).json()
    names = [e["name"] for e in tree["entries"]]
    assert "A1_Consensus_Essay_draft.txt" in names
    assert "College Coursework" in names
    assert "Drafts" in names

    # A changed autosave bumps the revision but keeps the same live file.
    changed = HUMAN_ESSAY + "\n\nOne more paragraph."
    res = await student.post(
        f"/api/assignments/{aid}/draft", json={"text": changed, "filename": "draft.txt"}
    )
    assert res.json()["revision"] == 2
    assert res.json()["drive_path"] == draft["drive_path"]

    # Unchanged content is a no-op, not another revision.
    again = await student.post(
        f"/api/assignments/{aid}/draft", json={"text": changed, "filename": "draft.txt"}
    )
    assert again.json()["revision"] == 2

    # Pinned snapshots get their own numbered file.
    pinned = await student.post(
        f"/api/assignments/{aid}/draft",
        json={"text": changed, "filename": "draft.txt", "snapshot": True},
    )
    assert pinned.json()["revision"] == 3
    assert pinned.json()["drive_path"].endswith("_draft_r03.txt")


async def test_submitting_a_draft_pins_a_versioned_snapshot(sessions, mock_provider):
    """Handing in a draft must leave an immutable numbered copy behind.

    Autosave overwrites one live file, so without the pin a student's Drive
    would hold no record of the exact text they submitted once they keep
    typing. The snapshot is written by the submit path itself, not left to the
    client to request.
    """
    teacher = await sessions.open(TEACHER, "Prof. Rao", "STAFF01")
    course_id, aid = await _make_assignment(teacher, "CS311", "Compilers")

    student = await sessions.open("arjun@college.edu", "Arjun Nair", "21CS017")
    assert (await student.post(f"/api/courses/{course_id}/enrol")).status_code == 200

    await student.post(
        f"/api/assignments/{aid}/draft",
        json={"text": HUMAN_ESSAY, "filename": "draft.txt"},
    )
    before = {e["name"] for e in (await student.get("/api/drive/tree/mine")).json()["entries"]}
    assert "A1_Consensus_Essay_draft.txt" in before
    assert not any(n.endswith("_draft_r01.txt") for n in before)

    res = await student.post(f"/api/assignments/{aid}/submit-draft")
    assert res.status_code == 200, res.text

    after = {e["name"] for e in (await student.get("/api/drive/tree/mine")).json()["entries"]}
    assert "A1_Consensus_Essay_draft_r01.txt" in after - before, sorted(after - before)
    # The live autosave file is untouched — the pin is additive.
    assert "A1_Consensus_Essay_draft.txt" in after

    from app.services.drive.base import DriveAccount

    account = DriveAccount(user_id=0, email="arjun@college.edu")
    pinned = [
        e for e in (await student.get("/api/drive/tree/mine")).json()["entries"]
        if e["name"] == "A1_Consensus_Essay_draft_r01.txt"
    ]
    assert len(pinned) == 1
    pinned_bytes = await mock_provider.download(account, pinned[0]["id"])
    assert pinned_bytes, "the pinned snapshot must not be empty"

    live = next(
        e for e in (await student.get("/api/drive/tree/mine")).json()["entries"]
        if e["name"] == "A1_Consensus_Essay_draft.txt"
    )

    # A later autosave overwrites the live file but cannot rewrite the pin.
    await student.post(
        f"/api/assignments/{aid}/draft",
        json={"text": HUMAN_ESSAY + "\n\nKept editing after submitting.", "filename": "draft.txt"},
    )
    assert await mock_provider.download(account, live["id"]) != pinned_bytes
    assert await mock_provider.download(account, pinned[0]["id"]) == pinned_bytes

    # The text can be read straight back off Drive.
    content = (await student.get(f"/api/assignments/{aid}/draft/content")).json()
    assert content["text"].startswith("I first ran into the two generals problem")


@pytest.mark.asyncio
async def test_full_submission_journey(sessions, mock_provider):
    teacher = await sessions.open(TEACHER, "Prof. Rao", "STAFF01")
    s1 = await sessions.open("meera@college.edu", "Meera Iyer", "21CS042")
    s2 = await sessions.open("arjun@college.edu", "Arjun Nair", "21CS017")

    course = (await teacher.post(
        "/api/courses",
        json={"code": "CS320", "title": "Distributed Systems",
              "academic_year": "2025-26", "semester": 3},
    )).json()
    assignment = (await teacher.post(
        "/api/assignments",
        json={
            "course_id": course["id"], "code": "A1", "title": "Consensus Essay",
            "brief": "Explain the two generals problem.", "max_marks": 100,
            "due_at": "2099-01-01T00:00:00Z",
        },
    )).json()
    aid = assignment["id"]

    for s in (s1, s2):
        assert (await s.post(f"/api/courses/{course['id']}/enrol")).status_code == 200

    ref = await teacher.post(
        f"/api/courses/{course['id']}/reference",
        json={"title": "Lecture notes: impossibility", "text": UNRELATED_ESSAY},
    )
    assert ref.status_code == 201, ref.text

    # Student 1 drafts then submits original work, and we let it finish
    # analysing before the next submission arrives so the corpus is
    # deterministic rather than dependent on worker scheduling.
    assert (await s1.post(
        f"/api/assignments/{aid}/draft", json={"text": HUMAN_ESSAY, "filename": "draft.txt"}
    )).status_code == 200

    sub1 = await s1.post(
        f"/api/assignments/{aid}/submit",
        files={"file": ("essay.txt", HUMAN_ESSAY.encode(), "text/plain")},
    )
    assert sub1.status_code == 200, sub1.text
    sub1 = sub1.json()
    assert sub1["attempt"] == 1
    assert sub1["archived_name"] == "21CS042_Meera_Iyer_A1_v1.txt"
    await get_pipeline().drain(timeout=60)

    report1 = (await s1.get(f"/api/submissions/{sub1['id']}/report")).json()
    assert report1["analysis_status"] == "done", report1.get("report")
    assert report1["similarity_threshold"] == 0.5
    assert report1["ai_threshold"] == 0.5
    assert report1["word_count"] > 100
    # The very first submission has no peers yet, so nothing to compare against.
    assert report1["report"]["similarity"]["documents_compared"] == 0
    assert report1["flagged"] is False, report1["report"]

    # Student 2 hands in a lightly reworded copy of student 1's essay.
    sub2 = await s2.post(
        f"/api/assignments/{aid}/submit",
        files={"file": ("essay.txt", HUMAN_VARIANT.encode(), "text/plain")},
    )
    assert sub2.status_code == 200, sub2.text
    sub2 = sub2.json()
    await get_pipeline().drain(timeout=60)

    report2 = (await s2.get(f"/api/submissions/{sub2['id']}/report")).json()
    assert report2["analysis_status"] == "done"
    assert report2["report"]["similarity"]["documents_compared"] >= 1
    assert report2["similarity_score"] >= 0.5, report2["report"]["similarity"]
    assert report2["flagged"] is True, report2["report"]["similarity"]
    assert report2["verdict"] in ("similarity_flag", "both_flag")
    assert report2["report"]["similarity"]["strongest_match_label"] == "21CS042 Meera Iyer"

    # Students cannot read each other's reports.
    assert (await s1.get(f"/api/submissions/{sub2['id']}/report")).status_code == 403

    # Re-running analysis on the first submission now sees the near-copy.
    # The index is directional — it measures how much of *this* document is
    # unoriginal — so the longer original stays below the threshold while the
    # shorter copy is flagged. That is the behaviour a reviewer wants.
    assert (await teacher.post(f"/api/submissions/{sub1['id']}/reanalyse")).status_code == 200
    await get_pipeline().drain(timeout=60)
    report1b = (await s1.get(f"/api/submissions/{sub1['id']}/report")).json()
    assert report1b["report"]["similarity"]["documents_compared"] >= 1
    assert report1b["similarity_score"] > 0.0, "re-analysis must now see the copy"
    assert report1b["similarity_score"] < report2["similarity_score"]
    assert report1b["flagged"] is False, report1b["report"]["similarity"]
    assert "directional" in report1b["report"]["similarity"]["interpretation"]

    # ── the teacher's Drive holds both submissions, neatly named
    tree = (await teacher.get("/api/drive/tree")).json()
    names = {e["name"] for e in tree["entries"]}
    paths = {e["path"] for e in tree["entries"]}
    assert "21CS042_Meera_Iyer_A1_v1.txt" in names
    assert "21CS017_Arjun_Nair_A1_v1.txt" in names
    assert "21CS042_Meera_Iyer_A1_integrity_report.json" in names
    assert "21CS017_Arjun_Nair_A1_integrity_report.json" in names
    assert "A1_roster_summary.csv" in names
    expected_prefix = (
        "/Submissions/2025-26 Sem 3/CS320 - Distributed Systems/"
        "A1 - Consensus Essay/submissions/"
    )
    assert any(p.startswith(expected_prefix) for p in paths), sorted(paths)[:10]

    # ── the report on Drive is valid JSON carrying both scores
    instructor = await _user_by_email(TEACHER)
    account, degraded = get_drive_sync().archive_account(instructor)
    assert degraded is False, "the instructor linked their Drive, so nothing should fall back"
    assert account.email == TEACHER
    provider = get_drive_sync().provider
    # Scope by course folder, not filename alone: every seeded course reuses the
    # assignment code "A1", so name-only lookups match other courses' artefacts.
    course_dir = "/Submissions/2025-26 Sem 3/CS320 - Distributed Systems/"
    report_entry = next(
        e for e in tree["entries"]
        if e["name"] == "21CS042_Meera_Iyer_A1_integrity_report.json"
        and e["path"].startswith(course_dir)
    )
    payload = json.loads((await provider.download(account, report_entry["id"])).decode())
    assert payload["student"]["roll_number"] == "21CS042"
    assert payload["thresholds"] == {"similarity": 0.5, "ai_detection": 0.5}
    assert payload["similarity"]["documents_compared"] >= 1
    assert "features" in payload["ai_detection"]

    csv_entry = next(
        e for e in tree["entries"]
        if e["name"] == "A1_roster_summary.csv" and e["path"].startswith(course_dir)
    )
    csv_text = (await provider.download(account, csv_entry["id"])).decode()
    assert csv_text.splitlines()[0] == (
        "roll_number,student,email,attempt,submitted_at,filename,similarity_score,"
        "ai_score,flagged,verdict,teacher_file,marks"
    )
    assert "21CS042" in csv_text and "21CS017" in csv_text

    # ── fingerprints persisted for future comparisons
    async with get_sessionmaker()() as db:
        rows = (
            await db.execute(
                select(DocumentFingerprint).where(
                    DocumentFingerprint.submission_id.in_([sub1["id"], sub2["id"]])
                )
            )
        ).scalars().all()
        assert len(rows) == 2
        assert all(len(r.signature) == 128 * 8 for r in rows)

    # ── cohort summary
    summary = (await teacher.get(f"/api/assignments/{aid}/summary")).json()
    assert summary["total"] == 2
    assert summary["analysed"] == 2
    assert summary["flagged"] == 1  # the copy, not the original

    # ── grading closes the loop
    graded = await teacher.post(
        f"/api/submissions/{sub1['id']}/grade",
        json={"marks": 88.5, "feedback": "Strong argument. Tighten the Paxos section."},
    )
    assert graded.status_code == 200, graded.text
    assert graded.json()["marks_awarded"] == 88.5
    assert graded.json()["status"] == "returned"
    assert (await s1.post(
        f"/api/submissions/{sub1['id']}/grade", json={"marks": 100, "feedback": ""}
    )).status_code == 403

    # the instructor can download the staged file
    dl = await teacher.get(f"/api/submissions/{sub1['id']}/download")
    assert dl.status_code == 200
    assert dl.text.startswith("I first ran into the two generals problem")


@pytest.mark.asyncio
async def test_identical_resubmission_is_rejected(sessions):
    teacher = await sessions.open(TEACHER, "Prof. Rao", "STAFF01")
    student = await sessions.open("zara@college.edu", "Zara Khan", "21CS088")
    course_id, aid = await _make_assignment(teacher, "CS330", "Compilers")
    await student.post(f"/api/courses/{course_id}/enrol")

    first = await student.post(
        f"/api/assignments/{aid}/submit",
        files={"file": ("lexer.txt", UNRELATED_ESSAY.encode(), "text/plain")},
    )
    assert first.status_code == 200, first.text
    dup = await student.post(
        f"/api/assignments/{aid}/submit",
        files={"file": ("lexer.txt", UNRELATED_ESSAY.encode(), "text/plain")},
    )
    assert dup.status_code == 409
    assert dup.json()["error"] == "conflict"


@pytest.mark.asyncio
async def test_changed_resubmission_increments_the_attempt(sessions):
    teacher = await sessions.open(TEACHER, "Prof. Rao", "STAFF01")
    student = await sessions.open("zara@college.edu", "Zara Khan", "21CS088")
    course_id, aid = await _make_assignment(teacher, "CS331", "Compilers II")
    await student.post(f"/api/courses/{course_id}/enrol")

    first = await student.post(
        f"/api/assignments/{aid}/submit",
        files={"file": ("lexer.txt", UNRELATED_ESSAY.encode(), "text/plain")},
    )
    assert first.json()["attempt"] == 1
    second = await student.post(
        f"/api/assignments/{aid}/submit",
        files={
            "file": (
                "lexer.txt", (UNRELATED_ESSAY + "\nAppendix added.").encode(), "text/plain"
            )
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["attempt"] == 2
    assert second.json()["archived_name"] == "21CS088_Zara_Khan_A1_v2.txt"


@pytest.mark.asyncio
async def test_unsupported_file_type_is_rejected(sessions):
    teacher = await sessions.open(TEACHER, "Prof. Rao", "STAFF01")
    student = await sessions.open("yusuf@college.edu", "Yusuf Ali", "21CS055")
    course_id, aid = await _make_assignment(teacher, "CS340", "Graphics")
    await student.post(f"/api/courses/{course_id}/enrol")

    bad = await student.post(
        f"/api/assignments/{aid}/submit",
        files={"file": ("malware.exe", b"MZ\x90\x00binary", "application/octet-stream")},
    )
    assert bad.status_code == 415
    assert bad.json()["error"] == "unsupported_file_type"


@pytest.mark.asyncio
async def test_unenrolled_student_cannot_submit(sessions):
    teacher = await sessions.open(TEACHER, "Prof. Rao", "STAFF01")
    outsider = await sessions.open("outsider@college.edu", "Out Sider", "21CS999")
    _course_id, aid = await _make_assignment(teacher, "CS350", "Security")

    res = await outsider.post(
        f"/api/assignments/{aid}/submit",
        files={"file": ("x.txt", HUMAN_ESSAY.encode(), "text/plain")},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_submit_from_draft(sessions):
    teacher = await sessions.open(TEACHER, "Prof. Rao", "STAFF01")
    student = await sessions.open("nadia@college.edu", "Nadia Farooq", "21CS061")
    course_id, aid = await _make_assignment(teacher, "CS360", "Databases")
    await student.post(f"/api/courses/{course_id}/enrol")

    assert (await student.post(
        f"/api/assignments/{aid}/draft", json={"text": HUMAN_ESSAY, "filename": "draft.txt"}
    )).status_code == 200

    missing = await student.post("/api/assignments/999999/submit-draft")
    assert missing.status_code in (404, 403)

    res = await student.post(f"/api/assignments/{aid}/submit-draft")
    assert res.status_code == 200, res.text
    assert res.json()["archived_name"] == "21CS061_Nadia_Farooq_A1_v1.txt"
    await get_pipeline().drain(timeout=30)

    report = (await student.get(f"/api/submissions/{res.json()['id']}/report")).json()
    assert report["analysis_status"] in ("done", "skipped")
