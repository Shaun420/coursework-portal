"""Seed a realistic demo dataset.

    python -m scripts.seed [--students 40] [--assign 2] [--reset]

Creates one instructor, a set of students, courses and assignments, then
submits work for every student — including one planted near-copy and one
LLM-style essay — and runs the whole thing through the analysis pipeline so the
portal, the scores and both Drive trees are populated.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import UTC

from app.core.config import settings  # noqa: E402
from app.db.session import get_sessionmaker, init_models  # noqa: E402
from app.domain.entities import Course, User  # noqa: E402
from app.domain.enums import Role  # noqa: E402
from app.services import submissions as svc  # noqa: E402
from app.services.drive.service import get_drive_sync  # noqa: E402
from app.services.workers.pipeline import get_pipeline  # noqa: E402

# A pool of distinct sentence frames. Each frame is used at most once per
# essay, and slots are filled from topic vocabulary, so no two students' work
# shares long runs of text. (The corpus is still synthetic — see README.)
TOPICS = {
    "consensus": {
        "problem": "the two generals problem",
        "tool": "Paxos",
        "detail": "message loss",
        "domain": "replicated state machines",
    },
    "consistency": {
        "problem": "linearizability",
        "tool": "quorum reads",
        "detail": "stale replicas",
        "domain": "distributed storage",
    },
    "replication": {
        "problem": "write conflicts",
        "tool": "state machine replication",
        "detail": "divergent histories",
        "domain": "multi-leader setups",
    },
    "clocks": {
        "problem": "causal ordering",
        "tool": "vector clocks",
        "detail": "concurrent updates",
        "domain": "event tracking",
    },
}

OPENERS = [
    "I want to start with {problem} because it is where my intuition kept failing.",
    "My first reaction to {problem} was that it should have been straightforward.",
    "There is a gap between how {problem} gets taught and how it behaves under load.",
    "Nothing in the lectures prepared me for how subtle {problem} turns out to be.",
    "I came at {problem} from the wrong direction and lost about a day to it.",
    "Ask me a month ago and I would have said {problem} was a solved thing.",
]

ARGUMENTS = [
    "The standard argument for {tool} quietly assumes a reliable channel, and that "
    "assumption ends up doing most of the work.",
    "Take the reliable channel away and the proof doesn't weaken, it collapses.",
    "I tried patching it with extra acknowledgement rounds before I accepted that "
    "more rounds just push the uncertainty one step further out.",
    "What struck me is that this is an impossibility result rather than an "
    "engineering shortfall, and that changes what good design looks like.",
    "Instead of chasing certainty you pick which guarantee to give up, and then "
    "you have to say so out loud.",
    "Most papers in {domain} don't say it, which is probably why so many are wrong.",
    "In practice systems get around it by redefining the goal: eventual agreement "
    "after the network heals is weaker, but it's useful.",
    "I think that redefinition is why {tool} reads so badly the first time.",
    "It's solving a problem that can't be solved as stated, so it changes the question.",
    "I tested my reading of this with a small simulator, and that convinced me "
    "more than the proof did.",
    "Watching {detail} appear in the logs was a different experience from reading about it.",
    "It also showed me how easy it is to build something that looks right until "
    "the timing shifts by a few milliseconds.",
    "I'd argue the honest position is to treat {tool} as a trade-off and document "
    "which side you conceded.",
    "There's a version of this where {detail} doesn't matter, and I haven't worked "
    "out where the boundary is.",
    "My simulator disagreed with me twice, and both times it was right.",
    "The part I still find unsatisfying is how little guidance this gives at design time.",
]

CLOSERS = [
    "So {tool} looks to me like a design decision rather than a discovery.",
    "My takeaway is that {domain} rewards engineers who admit the limits instead "
    "of hiding them.",
    "I'm left thinking {detail} deserves more space in the curriculum than it gets.",
    "Next I'd like to run {tool} against a real workload and see whether the "
    "trade-off holds up.",
    "If I'm honest, I changed my mind about {problem} three times while writing this.",
]

CONNECTIVES = ["", "", "", "Honestly, ", "In hindsight, ", "To be fair, ", "I suspect "]
HEDGES = ["", "", "probably ", "arguably ", "in my reading ", "more or less "]


def essay(rng: random.Random, index: int) -> str:
    """Build a distinct, human-flavoured essay: varied length, contractions,
    first person, and no long repeated runs."""
    topic = list(TOPICS.values())[index % len(TOPICS)]
    frames = rng.sample(ARGUMENTS, k=rng.randint(4, 7))
    sentences = [rng.choice(OPENERS).format(**topic)]
    for frame in frames:
        sentence = frame.format(**topic)
        # word-level variation so identical frames never produce identical text
        if rng.random() < 0.3:
            sentence = sentence.replace(" is ", " is " + rng.choice(HEDGES), 1)
        sentences.append(rng.choice(CONNECTIVES) + sentence)
    sentences.append(rng.choice(CLOSERS).format(**topic))

    paragraphs, current = [], []
    for sentence in sentences:
        current.append(sentence)
        if rng.random() < 0.35:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


LLM_STYLE = """Artificial intelligence has fundamentally transformed the landscape of modern
distributed systems, and it is important to note that its impact continues to evolve at an
unprecedented pace. Furthermore, engineers must leverage these innovative technologies to
foster a comprehensive architecture that nurtures fault tolerance. Moreover, the integration
of automated tooling offers a myriad of opportunities to enhance system reliability.

Additionally, observability platforms play a crucial role in this transformation, as they
enable operators to progress at their own pace while receiving tailored feedback. The role of
data analytics is equally pivotal, providing nuanced insights into system performance.
Consequently, teams can identify areas requiring improvement and implement targeted
interventions in a seamless manner.

Furthermore, it is essential to recognize that automation fosters collaboration among
engineers by facilitating communication across geographical boundaries. This robust framework
ultimately underscores the paramount importance of embracing technological advancement.
In conclusion, the future of distributed systems is inextricably linked to the continued
development and integration of these technologies."""


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--students", type=int, default=40)
    parser.add_argument("--assign", type=int, default=2)
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    args = parser.parse_args()

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{settings.portal_database_url.split('///')[-1]}{suffix}").unlink(missing_ok=True)
        import shutil

        shutil.rmtree(settings.mock_drive_root, ignore_errors=True)
        shutil.rmtree(settings.portal_data_dir / "uploads", ignore_errors=True)

    settings.ensure_dirs()
    await init_models()
    pipeline = get_pipeline()
    await pipeline.start()

    from datetime import datetime, timedelta

    from app.db.repositories import AssignmentRepository, CourseRepository, UserRepository
    from app.domain.entities import Assignment

    sm = get_sessionmaker()
    async with sm() as db:
        users = UserRepository(db)
        courses = CourseRepository(db)
        assignments = AssignmentRepository(db)

        teacher = await users.by_email("prof.rao@college.edu")
        if teacher is None:
            teacher = await users.create(
                google_sub="seed-teacher", email="prof.rao@college.edu",
                name="Prof. Rao", roll_number="STAFF01", role=Role.ADMIN.value,
                drive_linked=True, drive_email="prof.rao@college.edu",
            )
        else:
            teacher.drive_linked = True
            teacher.drive_email = teacher.email

        rng = random.Random(20260117)
        students: list[User] = []
        for i in range(args.students):
            roll = f"21CS{i + 1:03d}"
            email = f"student{i + 1:03d}@college.edu"
            existing = await users.by_email(email)
            if existing is None:
                existing = await users.create(
                    google_sub=f"seed-{email}", email=email,
                    name=f"Student {i + 1:03d}", roll_number=roll,
                    role=Role.STUDENT.value, drive_linked=True, drive_email=email,
                )
            else:
                existing.drive_linked = True
                existing.drive_email = email
            students.append(existing)

        specs = [
            ("CS301", "Distributed Systems"),
            ("CS305", "Database Internals"),
        ]
        made: list[tuple[Course, list[Assignment]]] = []
        for code, title in specs:
            course = next(
                (c for c in await users.courses_for(teacher.id) if c.code == code), None
            )
            if course is None:
                course = await courses.create(
                    code=code, title=title, academic_year="2025-26", semester=3,
                    instructor_id=teacher.id,
                )
                await courses.enrol(teacher.id, course.id, role="teacher")
            asns: list[Assignment] = []
            for n in range(args.assign):
                code_a = f"A{n + 1}"
                existing_a = next(
                    (a for a in await assignments.for_course(course.id) if a.code == code_a), None
                )
                if existing_a is None:
                    existing_a = await assignments.create(
                        course_id=course.id, code=code_a,
                        title=["Consensus Essay", "Replication Design Note"][n % 2],
                        brief=(
                            "Explain the impossibility result in your own words, then "
                            "describe what a real system gives up to work around it."
                        ),
                        max_marks=100,
                        opens_at=datetime.now(UTC) - timedelta(days=14),
                        due_at=datetime.now(UTC) + timedelta(days=7),
                        created_by=teacher.id,
                    )
                asns.append(existing_a)
                for student in students:
                    await courses.enrol(student.id, course.id, role="student")
            made.append((course, asns))
        await db.commit()

    # ── submissions ─────────────────────────────────────────────────────────
    submitted = 0
    for _course, asns in made:
        for assignment in asns:
            for idx, student in enumerate(students):
                rng = random.Random(hash((student.email, assignment.id)) % 2**32)
                if idx == args.students - 1:
                    text = LLM_STYLE                      # the planted AI-style essay
                elif idx == args.students - 2:
                    text = essay(random.Random(7), 0)      # a copy of another student
                else:
                    text = essay(rng, idx)
                async with sm() as db:
                    student_row = await UserRepository(db).get(student.id)
                await svc.submit(
                    student=student_row,
                    assignment_id=assignment.id,
                    filename=f"{assignment.code.lower()}_essay.txt",
                    data=text.encode(),
                    source="seed",
                )
                submitted += 1

    print(f"queued {submitted} submissions — waiting for analysis…")
    await pipeline.drain(timeout=600)

    # ── report ──────────────────────────────────────────────────────────────
    from sqlalchemy import func, select

    from app.domain.entities import Submission

    async with sm() as db:
        total = (await db.execute(select(func.count()).select_from(Submission))).scalar_one()
        flagged = (
            await db.execute(
                select(func.count()).select_from(Submission).where(Submission.flagged.is_(True))
            )
        ).scalar_one()
        print(f"\nsubmissions: {total}   flagged: {flagged}   queue: {pipeline.queue.stats()}")

        worst = (
            await db.execute(
                select(Submission).order_by(Submission.similarity_score.desc()).limit(5)
            )
        ).scalars().all()
        print("\ntop similarity scores:")
        for s in worst:
            print(
                f"  submission {s.id:>4}  sim={s.similarity_score:.3f} "
                f"ai={s.ai_score:.3f}  {s.verdict}  -> {s.archived_name}"
            )

        ai = (
            await db.execute(select(Submission).order_by(Submission.ai_score.desc()).limit(5))
        ).scalars().all()
        print("\ntop AI detection scores:")
        for s in ai:
            print(
                f"  submission {s.id:>4}  ai={s.ai_score:.3f}"
                f"  sim={s.similarity_score:.3f}  {s.verdict}"
            )

    drive = get_drive_sync()
    account, degraded = drive.archive_account(teacher)
    tree = await drive.tree(account, root_id="root", depth=6)
    print(f"\nteacher Drive ({account.email}, degraded={degraded}): {len(tree)} entries")
    for entry in tree[:25]:
        depth = entry["path"].count("/")
        mark = "/" if entry["type"] == "folder" else ""
        print(f"  {'  ' * depth}{entry['name']}{mark}")
    if len(tree) > 25:
        print(f"  … and {len(tree) - 25} more")

    await pipeline.stop()


if __name__ == "__main__":
    asyncio.run(main())
