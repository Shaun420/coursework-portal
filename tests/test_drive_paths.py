"""Drive naming and folder policy — the layout a teacher actually sees."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.drive import paths


@pytest.fixture
def ctx() -> paths.CourseContext:
    return paths.CourseContext(
        academic_year="2025-26", semester=3, course_code="CS301",
        course_title="Distributed Systems",
    )


@pytest.fixture
def asn() -> paths.AssignmentContext:
    return paths.AssignmentContext(code="A1", title="Consensus: Essay & Implementation")


def test_submission_folder_is_term_course_assignment(ctx, asn):
    assert paths.submission_folder_path(ctx, asn) == [
        "Submissions", "2025-26 Sem 3", "CS301 - Distributed Systems",
        "A1 - Consensus Essay & Implementation", "submissions",
    ]


def test_report_folder_sits_beside_submissions(ctx, asn):
    sub = paths.submission_folder_path(ctx, asn)
    rep = paths.report_folder_path(ctx, asn)
    assert sub[:-1] == rep[:-1]
    assert sub[-1] == "submissions" and rep[-1] == "reports"


def test_submission_filename_is_roll_name_code_attempt(ctx, asn):
    name = paths.submission_filename("21CS042", "Meera Iyer", asn, 1, "essay final (2).PDF")
    assert name == "21CS042_Meera_Iyer_A1_v1.pdf"


def test_submission_filename_sanitises_hostile_input(asn):
    name = paths.submission_filename(
        "../../etc", 'Bad/Name:*?"<>|', asn, 3, "weird\\file.Docx"
    )
    assert "/" not in name and "\\" not in name
    assert all(c not in name for c in ':*?"<>|')
    assert name.endswith(".docx")
    assert name == "etc_BadName_A1_v3.docx"


def test_missing_roll_number_is_explicit(asn):
    assert (
        paths.submission_filename(None, "Anonymous", asn, 1, "x.txt")
        == "NOROLL_Anonymous_A1_v1.txt"
    )


def test_attempt_is_never_below_one(asn):
    assert "_v1." in paths.submission_filename("R1", "A B", asn, 0, "x.txt")


def test_report_and_roster_filenames(asn):
    assert (
        paths.report_filename("21CS042", "Meera Iyer", asn)
        == "21CS042_Meera_Iyer_A1_integrity_report.json"
    )
    assert paths.roster_summary_filename(asn) == "A1_roster_summary.csv"


def test_student_draft_path_lives_in_their_own_root(ctx, asn):
    drafts = paths.draft_folder_path(ctx, asn)
    assert drafts[0] == "College Coursework"
    assert drafts[-1] == "Drafts"
    assert paths.student_submitted_path(ctx, asn)[-1] == "Submitted"


def test_live_draft_has_no_revision_but_snapshots_do(asn):
    assert (
        paths.live_draft_filename(asn, "essay.docx")
        == "A1_Consensus_Essay_&_Implementation_draft.docx"
    )
    assert (
        paths.draft_filename(asn, 3, "essay.docx")
        == "A1_Consensus_Essay_&_Implementation_draft_r03.docx"
    )


def test_student_copy_records_the_date(asn):
    when = datetime(2026, 1, 12, 14, 30, tzinfo=UTC)
    name = paths.student_copy_filename(asn, when, 2, "essay.pdf")
    assert name == "A1_Consensus_Essay_&_Implementation_submitted_2026-01-12_v2.pdf"


def test_slugify_handles_unicode_and_empty():
    assert paths.slugify("Ünïcödé Tëxt") == "Unicode_Text"
    assert paths.slugify("///") == "untitled"
    assert paths.slugify("") == "untitled"


def test_display_name_strips_illegal_characters_only():
    assert paths.display_name("A/B:C*D") == "ABCD"
    assert paths.display_name("  spaced   out  ") == "spaced out"


def test_long_names_are_truncated():
    long_title = "T" * 500
    ctx = paths.AssignmentContext("A1", "x")
    name = paths.submission_filename("R" * 200, long_title, ctx, 1, "f.txt")
    assert len(name) < 150
