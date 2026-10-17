"""Load harness for the single-node capacity claim.

    python -m scripts.loadtest --users 500 --duration 30

Starts the server as a subprocess (so you are measuring the real HTTP stack,
not an in-process shortcut), signs in a pool of virtual students, then drives a
mixed workload that matches how the portal is actually used:

    60 %  dashboard reads        (the dominant cost: 10k students refreshing)
    20 %  draft autosaves        (a Drive write plus a DB row each)
    15 %  integrity-report reads
     5 %  full submissions       (upload + Drive archive + analysis enqueue)

Reports throughput, latency percentiles, error rate and queue depth. Run it
against the box you intend to deploy on — the numbers are only meaningful for
that hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SAMPLE_ESSAY = (
    "I want to start with the two generals problem because it is where my intuition "
    "kept failing. The standard argument assumes a reliable channel, and that assumption "
    "does most of the work. Take it away and the proof collapses rather than weakens. "
    "I spent a while trying to patch it with extra acknowledgement rounds before accepting "
    "that adding rounds only moves the uncertainty one step further out. What struck me is "
    "that this is an impossibility result, not an engineering limitation, and that changes "
    "what a good design looks like. Instead of chasing certainty you pick which guarantee "
    "to weaken, and you say so explicitly. In practice systems work around it by redefining "
    "the goal: they settle for eventual agreement after the network heals. I tested my "
    "understanding by writing a small simulator, and watching the failure modes appear was "
    "more convincing than any proof. So my conclusion is that consensus is a design "
    "decision rather than a discovery, and the honest position is to treat it as a "
    "trade-off and document which side you gave up."
)


class Stats:
    def __init__(self) -> None:
        self.latencies: dict[str, list[float]] = {}
        self.errors: dict[str, int] = {}
        self.ok = 0
        self.failed = 0

    def record(self, kind: str, elapsed: float, status: int) -> None:
        if status >= 400:
            self.failed += 1
            key = f"{kind}:{status}"
            self.errors[key] = self.errors.get(key, 0) + 1
            return
        self.ok += 1
        self.latencies.setdefault(kind, []).append(elapsed)

    def all_latencies(self) -> list[float]:
        return [v for values in self.latencies.values() for v in values]

    def pct(self, values: list[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(len(ordered) * p / 100))
        return ordered[idx]


async def wait_for_health(base: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{base}/healthz")
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    raise RuntimeError("server did not become healthy in time")


def _cookies_from(response: httpx.Response) -> dict[str, str]:
    """Read the Set-Cookie headers directly.

    All virtual users share one httpx client (and therefore one cookie jar), so
    letting it store cookies would leave every user signed in as whoever logged
    in last. Carrying explicit Cookie headers per user keeps the sessions apart.
    """
    jar: dict[str, str] = {}
    for raw in response.headers.get_list("set-cookie"):
        pair = raw.split(";", 1)[0]
        if "=" in pair:
            name, _, value = pair.partition("=")
            jar[name.strip()] = value.strip()
    return jar


async def sign_in(client: httpx.AsyncClient, base: str, index: int) -> dict[str, str] | None:
    """Returns the per-user Cookie/X-CSRF-Token headers, or None."""
    r = await client.post(
        f"{base}/auth/dev/login",
        json={
            "email": f"load{index:05d}@college.edu",
            "name": f"Load {index}",
            "roll_number": f"LD{index:05d}",
        },
    )
    if r.status_code != 200:
        return None
    jar = _cookies_from(r)
    if "cp_session" not in jar or "cp_csrf" not in jar:
        return None
    return {
        "Cookie": f"cp_session={jar['cp_session']}; cp_csrf={jar['cp_csrf']}",
        "X-CSRF-Token": jar["cp_csrf"],
    }


async def setup_fixture(base: str, students: int) -> tuple[int, int]:
    """One teacher, one course, one assignment that every virtual user enrols in."""
    async with httpx.AsyncClient(timeout=30.0) as admin:
        await admin.post(
            f"{base}/auth/dev/login",
            json={"email": "loadmaster@college.edu", "name": "Load Master"},
        )  # establishes nothing; real headers are built below
        jar = _cookies_from(
            await admin.post(
                f"{base}/auth/dev/login",
                json={"email": "loadmaster@college.edu", "name": "Load Master"},
            )
        )
        headers = {
            "Cookie": f"cp_session={jar['cp_session']}; cp_csrf={jar['cp_csrf']}",
            "X-CSRF-Token": jar["cp_csrf"],
        }

        # Reuse the fixture if a previous run left one behind.
        existing = {c["code"]: c["id"] for c in (await admin.get(
            f"{base}/api/courses", headers=headers
        )).json()}
        if "LOAD1" in existing:
            course_id = existing["LOAD1"]
        else:
            r = await admin.post(
                f"{base}/api/courses",
                json={"code": "LOAD1", "title": "Load Test Course", "semester": 3},
                headers=headers,
            )
            if r.status_code != 201:
                raise RuntimeError(f"could not create course: {r.status_code} {r.text}")
            course_id = r.json()["id"]

        feed = (await admin.get(f"{base}/api/assignments", headers=headers)).json()
        existing_asn = next(
            (a["id"] for a in feed if a.get("course_code") == "LOAD1" and a["code"] == "A1"),
            None,
        )
        if existing_asn:
            assignment_id = existing_asn
        else:
            r = await admin.post(
                f"{base}/api/assignments",
                json={
                    "course_id": course_id, "code": "A1", "title": "Load Essay",
                    "brief": "Explain the two generals problem.", "max_marks": 100,
                    "due_at": "2099-01-01T00:00:00Z", "allow_resubmit": True,
                },
                headers=headers,
            )
            if r.status_code != 201:
                raise RuntimeError(f"could not create assignment: {r.status_code} {r.text}")
            assignment_id = r.json()["id"]
    return course_id, assignment_id


async def virtual_user(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    course_id: int,
    assignment_id: int,
    stop_at: float,
    stats: Stats,
    mix: str = "mixed",
) -> None:
    rng = random.Random()
    try:
        await client.post(f"{base}/api/courses/{course_id}/enrol", headers=headers)
    except httpx.HTTPError:
        pass  # enrolment is best-effort; the measured loop handles its own errors

    while time.monotonic() < stop_at:
        roll = rng.random()
        if mix == "read":
            roll *= 0.59          # force the dashboard/report branches
        elif mix == "write":
            roll = 0.65 + roll * 0.34
        started = time.perf_counter()
        try:
            if roll < 0.60:
                r = await client.get(f"{base}/api/assignments")
                kind = "dashboard"
            elif roll < 0.80:
                r = await client.post(
                    f"{base}/api/assignments/{assignment_id}/draft",
                    json={"text": SAMPLE_ESSAY + f" {rng.random()}", "filename": "draft.txt"},
                    headers=headers,
                )
                kind = "draft"
            elif roll < 0.95:
                mine = await client.get(f"{base}/api/assignments/{assignment_id}/my-submission")
                sid = (mine.json().get("submission") or {}).get("id")
                r = (
                    await client.get(f"{base}/api/submissions/{sid}/report")
                    if sid
                    else await client.get(f"{base}/api/system/health")
                )
                kind = "report"
            else:
                r = await client.post(
                    f"{base}/api/assignments/{assignment_id}/submit",
                    files={
                        "file": (
                            "essay.txt",
                            (SAMPLE_ESSAY + f" {rng.random()}").encode(),
                            "text/plain",
                        )
                    },
                    headers=headers,
                )
                kind = "submit"
            stats.record(kind, time.perf_counter() - started, r.status_code)
        except httpx.HTTPError:
            stats.failed += 1
            stats.errors[f"{kind}:transport"] = stats.errors.get(f"{kind}:transport", 0) + 1


async def run(args) -> int:
    base = f"http://127.0.0.1:{args.port}"
    env = {
        **os.environ,
        "PORTAL_ENV": "development",
        "PORTAL_DATA_DIR": str(ROOT / "data"),
        "RATE_LIMIT_PER_MINUTE": "1000000",
        "DRAFT_RATE_LIMIT_PER_MINUTE": "1000000",
        "ANALYSIS_WORKERS": str(args.workers),
        "ANALYSIS_CPU_THREADS": str(args.cpu_threads),
        "ANALYSIS_QUEUE_CAPACITY": str(args.queue),
    }
    log_path = ROOT / "data" / "loadtest-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    server_log = open(log_path, "wb")
    print(f"server log -> {log_path}")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "0.0.0.0", "--port", str(args.port), "--log-level", "warning"],
        cwd=ROOT, env=env,
        stdout=server_log, stderr=subprocess.STDOUT,
    )
    try:
        await wait_for_health(base)
        print(f"server up on {base} (workers={args.workers}, cpu_threads={args.cpu_threads})")

        course_id, assignment_id = await setup_fixture(base, args.users)
        print(f"fixture ready: course={course_id} assignment={assignment_id}")

        limits = httpx.Limits(
            max_connections=args.users + 50, max_keepalive_connections=args.users + 50
        )
        stats = Stats()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=20.0), limits=limits
        ) as client:
            sessions = []
            for i in range(args.users):
                got = await sign_in(client, base, i)
                if got:
                    sessions.append(got)
            print(f"signed in {len(sessions)} virtual students; running {args.duration}s at "
                  f"concurrency={args.users}\n")

            stop_at = time.monotonic() + args.duration
            started = time.monotonic()
            results = await asyncio.gather(
                *[
                    virtual_user(
                        client, base, hdrs, course_id, assignment_id, stop_at, stats, args.mix
                    )
                    for hdrs in sessions
                ],
                return_exceptions=True,
            )
            crashed = [r for r in results if isinstance(r, BaseException)]
            if crashed:
                print(f"({len(crashed)} virtual users aborted early: {type(crashed[0]).__name__})")
            elapsed = time.monotonic() - started

            try:
                metrics = (await client.get(f"{base}/api/system/metrics")).json()
                queue = (await client.get(f"{base}/api/system/queue")).json()
            except httpx.HTTPError as exc:
                print(f"(could not read server metrics: {exc})")
                metrics = {"rss_mb": 0, "threads": 0, "latency_p99_ms": 0}
                queue = {"depth": 0, "enqueued": 0, "completed": 0, "failed": 0, "dropped": 0}

        lat = stats.all_latencies()
        total = stats.ok + stats.failed
        print("=" * 72)
        print(f"duration            {elapsed:.1f}s")
        print(f"concurrency         {args.users}   mix={args.mix}")
        print(f"requests            {total:,}   ({stats.ok:,} ok / {stats.failed:,} failed)")
        print(f"throughput          {total / elapsed:,.0f} req/s")
        print(f"latency  p50        {stats.pct(lat, 50) * 1000:.1f} ms")
        print(f"latency  p95        {stats.pct(lat, 95) * 1000:.1f} ms")
        print(f"latency  p99        {stats.pct(lat, 99) * 1000:.1f} ms")
        print(f"latency  max        {(max(lat) * 1000) if lat else 0:.1f} ms")
        if stats.errors:
            errs = ", ".join(f"{k}×{v}" for k, v in sorted(stats.errors.items()))
            print("errors              " + errs)
        print("-" * 72)
        print("per-endpoint p95:")
        for kind, values in sorted(stats.latencies.items()):
            print(f"  {kind:<12} n={len(values):<7,} p50={stats.pct(values, 50)*1000:>7.1f}ms "
                  f"p95={stats.pct(values, 95)*1000:>7.1f}ms")
        print("-" * 72)
        print(f"server rss          {metrics['rss_mb']:.0f} MB   threads={metrics['threads']}")
        print(f"server p99          {metrics['latency_p99_ms']:.1f} ms")
        print(f"analysis queue      depth={queue['depth']} enqueued={queue['enqueued']} "
              f"completed={queue['completed']} failed={queue['failed']} dropped={queue['dropped']}")
        print("=" * 72)
        return 0 if stats.failed == 0 else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        server_log.close()
        tail = log_path.read_text(errors="replace").splitlines()[-25:]
        if tail:
            print("\n--- server log (last lines) ---")
            print("\n".join(tail))


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--users", type=int, default=500, help="concurrent virtual students")
    p.add_argument("--duration", type=float, default=30, help="seconds to run")
    p.add_argument("--port", type=int, default=8899)
    p.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    p.add_argument("--cpu-threads", type=int, default=os.cpu_count() or 2)
    p.add_argument("--queue", type=int, default=20000)
    p.add_argument("--mix", choices=["mixed", "read", "write"], default="mixed",
                   help="read = dashboard/report only; write = drafts and submissions only")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
