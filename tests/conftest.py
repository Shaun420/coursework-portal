"""Shared fixtures.

Environment variables are set *before* any app module is imported, because
`settings`, the drive provider and the pipeline are process-wide singletons
built at import time.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="portal-test-"))

os.environ.update(
    {
        "PORTAL_ENV": "development",
        "PORTAL_SECRET_KEY": "test-secret-key-for-pytest-only-0123456789",
        "PORTAL_DATABASE_URL": f"sqlite+aiosqlite:///{TMP}/portal.db",
        "PORTAL_DATA_DIR": str(TMP / "data"),
        "DRIVE_PROVIDER": "mock",
        "MOCK_DRIVE_ROOT": str(TMP / "mock_drive"),
        "ANALYSIS_WORKERS": "2",
        "ANALYSIS_CPU_THREADS": "2",
        "ANALYSIS_QUEUE_CAPACITY": "50",
        "RATE_LIMIT_PER_MINUTE": "100000",
        "DRAFT_RATE_LIMIT_PER_MINUTE": "100000",
        "SIMILARITY_THRESHOLD": "0.50",
        "AI_DETECTION_THRESHOLD": "0.50",
        "INSTITUTION_EMAIL": "portal@college.edu",
        "TEACHER_DRIVE_ROOT_ID": "root",
    }
)

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.api.deps import reset_rate_limiter  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import get_engine, init_models  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import analysis  # noqa: E402
from app.services.drive import get_drive_provider  # noqa: E402
from app.services.drive.service import get_drive_sync  # noqa: E402
from app.services.workers.pipeline import get_pipeline  # noqa: E402


class ApiClient:
    """httpx wrapper that injects the double-submit CSRF header."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    @property
    def csrf(self) -> str:
        return self.http.cookies.get(settings.csrf_cookie) or ""

    async def get(self, path: str, **kw) -> httpx.Response:
        return await self.http.get(path, **kw)

    async def _send(self, method: str, path: str, **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", {}) or {})
        headers.setdefault("X-CSRF-Token", self.csrf)
        return await self.http.request(method, path, headers=headers, **kw)

    async def post(self, path: str, **kw) -> httpx.Response:
        return await self._send("POST", path, **kw)

    async def patch(self, path: str, **kw) -> httpx.Response:
        return await self._send("PATCH", path, **kw)

    async def delete(self, path: str, **kw) -> httpx.Response:
        return await self._send("DELETE", path, **kw)

    async def login(self, email: str, name: str, roll: str | None = None) -> dict:
        res = await self.post(
            "/auth/dev/login",
            json={"email": email, "name": name, "roll_number": roll or ""},
        )
        assert res.status_code == 200, res.text
        return res.json()


@pytest.fixture(scope="session")
def app():
    return create_app()


@pytest.fixture(scope="session")
async def _bootstrap(app):
    await init_models()
    pipeline = get_pipeline()
    await pipeline.start()
    yield
    await pipeline.stop()
    await get_engine().dispose()


@pytest.fixture
async def client(app, _bootstrap) -> ApiClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield ApiClient(http)
    reset_rate_limiter()


@pytest.fixture(autouse=True)
def _clean_state():
    analysis.clear_caches()
    get_drive_sync().clear_folder_cache()
    reset_rate_limiter()
    yield
    analysis.clear_caches()


@pytest.fixture
async def teacher(client: ApiClient) -> ApiClient:
    await client.login("prof.rao@college.edu", "Prof. Rao")
    return client


@pytest.fixture
async def student(client: ApiClient) -> ApiClient:
    await client.login("meera@college.edu", "Meera Iyer", "21CS042")
    return client


@pytest.fixture
def mock_provider():
    provider = get_drive_provider()
    provider.calls.clear()
    return provider


@pytest.fixture
def tmp_dir() -> Path:
    return TMP


# ── Sample documents ──────────────────────────────────────────────────────────

HUMAN_ESSAY = """I first ran into the two generals problem in a networking class, and honestly
it annoyed me. The idea is simple enough: two armies want to attack at the same time, and the
only messenger they have can be captured. So they can never be quite sure the other one got
the message. I kept thinking there must be some clever trick, but there isn't. It's a genuine
impossibility result, not an engineering failure.

What I found interesting was how the proof generalises. Any channel that can lose messages
breaks the guarantee, no matter how many acknowledgements you add. Adding more rounds just
pushes the uncertainty one step further out. That felt almost cruel.

In practice, though, systems get around it. They don't try to reach certainty; they settle
for something weaker, like both sides eventually agreeing after the network heals. That's
roughly what Paxos does, and I think it's why the protocol is so hard to read at first.
It's solving a problem that can't be solved, so it has to redefine the goal.

I'd argue that's the real lesson. When you can't have safety and liveness together, you pick
which one to give up, and you say so out loud. Most distributed systems papers don't, which
is probably why so many of them are wrong.
"""

HUMAN_VARIANT = """I first ran into the two generals problem in a networking class, and honestly
it annoyed me. The idea is simple enough: two armies want to attack at the same time, and the
only messenger they have can be captured. So they can never be quite sure the other one got
the message. I kept thinking there must be some clever trick, but there isn't. It's a genuine
impossibility result, not an engineering failure.

What I found interesting was how the proof generalises. Any channel that can lose messages
breaks the guarantee, no matter how many acknowledgements you add. Adding more rounds just
pushes the uncertainty one step further out. That felt almost cruel.

In practice, systems work around it by not trying to reach certainty at all. They settle for
something weaker: both sides eventually agree after the network heals. That is roughly what
Paxos does, which is why the protocol is hard to read at first. It solves a problem that
cannot be solved, so it has to redefine the goal.

The real lesson is that when you cannot have safety and liveness together, you choose which
one to give up, and you say so out loud.
"""

UNRELATED_ESSAY = """The migration of monarch butterflies across North America remains one of the
most studied insect migrations, and yet several of its mechanisms are still contested.
Navigation appears to combine a time-compensated sun compass with magnetic cues, and the
antennae seem to matter more than early experiments assumed. Laboratory work with tethered
butterflies in flight simulators has been especially useful, because it lets researchers
change one variable at a time without the confounds of weather.

Overwintering sites in the oyamel fir forests of central Mexico have shrunk considerably,
and the causes are mixed: logging, drought, and shifting rainfall all appear in the record.
Conservation effort has concentrated on protecting contiguous forest rather than isolated
groves, which makes intuitive sense but is difficult to verify from population counts alone.

Milkweed availability along the migration route is another constraint, and one that
interacts with agricultural practice. Herbicide tolerance in crops removed a great deal of
the roadside and field-margin milkweed that larvae depend on. Reforestation programmes that
plant milkweed have had modest but measurable local effects.
"""

AI_STYLE_ESSAY = """Artificial intelligence has fundamentally transformed the landscape of modern
education, and it is important to note that its impact continues to evolve at an unprecedented
pace. Furthermore, educational institutions must leverage these innovative technologies to
foster a comprehensive learning environment that nurtures critical thinking skills. Moreover,
the integration of artificial intelligence in the classroom offers a myriad of opportunities
for educators to enhance their teaching methodologies.

Additionally, personalized learning platforms play a crucial role in this transformation, as
they enable students to progress at their own pace while receiving tailored feedback. The
role of data analytics is equally pivotal, providing educators with nuanced insights into
student performance. Consequently, teachers can identify areas requiring improvement and
implement targeted interventions in a seamless manner.

Furthermore, it is essential to recognize that artificial intelligence fosters collaboration
among students by facilitating communication across geographical boundaries. This robust
framework ultimately underscores the paramount importance of embracing technological
advancement in education. In conclusion, the future of learning is inextricably linked to
the continued development and integration of artificial intelligence technologies.
"""
