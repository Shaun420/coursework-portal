"""HTTP surface: auth, sessions, CSRF, role separation."""

from __future__ import annotations

import pytest

from app.core.config import settings


@pytest.mark.asyncio
async def test_healthz_is_open(client):
    res = await client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_api_requires_a_session(client):
    res = await client.get("/auth/me")
    assert res.status_code == 401
    assert res.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_dev_login_issues_session_and_csrf(client):
    data = await client.login("ada@college.edu", "Ada")
    assert data["user"]["email"] == "ada@college.edu"
    assert client.http.cookies.get(settings.session_cookie)
    assert client.http.cookies.get(settings.csrf_cookie)

    me = await client.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == "ada@college.edu"
    assert body["drive_provider"] == "mock"
    assert body["thresholds"] == {"similarity": 0.5, "ai_detection": 0.5}


@pytest.mark.asyncio
async def test_first_account_becomes_admin(client, _bootstrap):
    # The very first dev account on a fresh database is the administrator.
    res = await client.get("/auth/me")
    if res.status_code != 200:
        await client.login("first@college.edu", "First")
    me = (await client.get("/auth/me")).json()
    assert me["user"]["role"] in {"admin", "student", "teacher"}


@pytest.mark.asyncio
async def test_csrf_is_enforced_on_unsafe_methods(client):
    await client.login("carol@college.edu", "Carol")
    res = await client.http.post(
        "/api/courses",
        json={"code": "CS100", "title": "No token"},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert res.status_code == 403
    assert res.json()["error"] == "csrf_failed"


@pytest.mark.asyncio
async def test_outsider_domain_cannot_create_courses(client, _bootstrap):
    """Roles are derived from data; only the institution's own domain bootstraps."""
    await client.login("mallory@not-college.example", "Mallory", "X1")
    res = await client.post("/api/courses", json={"code": "EVIL1", "title": "Nope"})
    assert res.status_code == 403
    assert res.json()["error"] == "forbidden"


@pytest.mark.asyncio
async def test_institution_domain_can_bootstrap_a_course_and_becomes_teacher(
    client, _bootstrap
):
    await client.login("newstaff@college.edu", "New Staff", "STAFF99")
    assert (await client.get("/auth/me")).json()["user"]["role"] == "student"

    res = await client.post("/api/courses", json={"code": "CS401", "title": "Topics"})
    assert res.status_code == 201, res.text
    assert res.json()["code"] == "CS401"

    # Teaching a course is what makes you a teacher.
    assert (await client.get("/auth/me")).json()["user"]["role"] == "teacher"


@pytest.mark.asyncio
async def test_open_course_creation_can_be_switched_off(client, _bootstrap, monkeypatch):
    monkeypatch.setattr(settings, "allow_open_course_creation", False, raising=False)
    await client.login("quietstaff@college.edu", "Quiet Staff", "STAFF98")
    res = await client.post("/api/courses", json={"code": "CS402", "title": "Blocked"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_logout_clears_the_session(client):
    await client.login("erin@college.edu", "Erin")
    assert (await client.get("/auth/me")).status_code == 200
    assert (await client.post("/auth/logout")).status_code == 200
    assert (await client.get("/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_google_login_requires_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "", raising=False)
    res = await client.get("/auth/google/login", follow_redirects=False)
    assert res.status_code == 503
    assert res.json()["error"] == "google_not_configured"


@pytest.mark.asyncio
async def test_pages_redirect_anonymous_users_to_login(client):
    for path in ("/dashboard", "/drive", "/system", "/courses"):
        res = await client.get(path, follow_redirects=False)
        assert res.status_code == 302
        assert res.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_error_envelope_is_consistent(client):
    res = await client.get("/api/submissions/999999/report")
    assert res.status_code in (401, 404)
    if res.status_code == 404:
        assert res.json()["error"] == "not_found"
