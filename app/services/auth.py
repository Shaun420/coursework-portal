"""Authentication.

Two sign-in paths share one session mechanism:

* **Google OAuth 2.0** — the real one. Authorization-code flow with PKCE,
  ``access_type=offline`` so we receive a refresh token, which is what lets the
  portal write to a student's Drive weeks later without prompting again.
* **Dev sign-in** — enabled only outside production, so this repo is usable
  without Google credentials. It creates the same user rows and the same
  mock-Drive linkage.

Sessions are signed (not encrypted) cookies carrying ``user_id:role``; the
browser never holds anything the server cannot verify.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.errors import ForbiddenError, GoogleNotConfiguredError, UnauthorizedError
from app.core.logging import get_logger
from app.core.security import SessionSigner, constant_time_eq, csrf_token, new_state
from app.db.repositories import UserRepository
from app.domain.entities import User
from app.domain.enums import Role

log = get_logger(__name__)

signer = SessionSigner(settings.portal_secret_key)

# OAuth states live in memory: they are single-use, short-lived, and this is a
# single-node deployment. Behind a load balancer move them to Redis.
_pending_states: dict[str, tuple[datetime, str]] = {}
STATE_TTL_SECONDS = 600


@dataclass(slots=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str
    picture: str | None


# ── OAuth plumbing ────────────────────────────────────────────────────────────


def _prune_states() -> None:
    cutoff = datetime.now(UTC) - timedelta(seconds=STATE_TTL_SECONDS)
    for state in [s for s, (ts, _) in _pending_states.items() if ts < cutoff]:
        _pending_states.pop(state, None)


def begin_login(redirect_after: str = "/") -> tuple[str, str, str]:
    """Return (authorisation URL, state, verifier) and remember the state."""
    if not settings.google_configured:
        raise GoogleNotConfiguredError(
            "Google OAuth is not configured on this server. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET, or use the development sign-in."
        )
    _prune_states()
    state = new_state()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    _pending_states[state] = (datetime.now(UTC), verifier)

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "scope": " ".join(settings.scope_list),
        "state": state,
        "access_type": "offline",      # ask for a refresh token
        "prompt": "consent",           # Google only issues one on real consent
        "include_granted_scopes": "true",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": "",
    }
    url = settings.google_auth_uri + "?" + "&".join(
        f"{k}={v}" for k, v in params.items() if v != ""
    )
    return url, state, verifier


def consume_state(state: str) -> str:
    entry = _pending_states.pop(state, None)
    if entry is None:
        raise UnauthorizedError("Sign-in state expired or was already used. Start again.")
    ts, verifier = entry
    if datetime.now(UTC) - ts > timedelta(seconds=STATE_TTL_SECONDS):
        raise UnauthorizedError("Sign-in took too long. Start again.")
    return verifier


async def exchange_code(code: str, verifier: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            settings.google_token_uri,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
    if resp.status_code >= 400:
        raise UnauthorizedError(
            f"Google rejected the authorisation code ({resp.status_code}).",
            detail=resp.text[:300],
        )
    return resp.json()


async def fetch_identity(access_token: str) -> GoogleIdentity:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            settings.google_userinfo_uri, headers={"Authorization": f"Bearer {access_token}"}
        )
    if resp.status_code >= 400:
        raise UnauthorizedError(f"Could not read the Google profile ({resp.status_code}).")
    data = resp.json()
    return GoogleIdentity(
        sub=str(data["sub"]),
        email=data["email"],
        name=data.get("name") or data["email"].split("@")[0],
        picture=data.get("picture"),
    )


# ── user provisioning ─────────────────────────────────────────────────────────


async def upsert_google_user(
    db, identity: GoogleIdentity, tokens: dict  # noqa: ANN001
) -> User:
    users = UserRepository(db)
    user = await users.by_google_sub(identity.sub)
    if user is None:
        user = await users.by_email(identity.email)
        if user is not None and user.google_sub != identity.sub:
            # An existing account (e.g. seeded) is being linked to Google now.
            user.google_sub = identity.sub
    if user is None:
        user = await users.create(
            google_sub=identity.sub,
            email=identity.email,
            name=identity.name,
            picture_url=identity.picture,
            roll_number=_roll_number_from(identity.email),
            role=Role.STUDENT.value,
        )

    user.name = identity.name or user.name
    user.picture_url = identity.picture or user.picture_url
    refresh = tokens.get("refresh_token")
    if refresh:
        user.refresh_token = encrypt_secret(refresh)
    user.drive_linked = True
    user.drive_email = identity.email
    if tokens.get("expires_in"):
        user.token_expires_at = datetime.now(UTC) + timedelta(
            seconds=int(tokens["expires_in"])
        )
    user.linked_at = datetime.now(UTC)
    user.role = await resolve_role(db, user)
    await db.flush()
    log.info(
        "signed in %s (%s) role=%s linked=%s",
        user.email, user.id, user.role, user.drive_linked,
    )
    return user


async def resolve_role(db, user: User) -> str:  # noqa: ANN001
    """Admin if they are the first account, teacher if they instruct a course.

    Roles are derived from data rather than claimed at sign-up: a student
    cannot make themselves a teacher by editing a request.
    """
    from app.domain.entities import Course

    if user.role == Role.ADMIN.value:
        return Role.ADMIN.value
    total = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    if total <= 1:
        return Role.ADMIN.value
    instructs = (
        await db.execute(
            select(func.count()).select_from(Course).where(Course.instructor_id == user.id)
        )
    ).scalar_one()
    return Role.TEACHER.value if instructs else Role.STUDENT.value


def _roll_number_from(email: str) -> str | None:
    local = email.split("@", 1)[0]
    return local if local.isalnum() and len(local) <= 24 else None


# ── sessions ──────────────────────────────────────────────────────────────────


def make_session_token(user: User) -> str:
    return signer.issue(user.id, user.role)


def read_session(token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    return signer.read(token, settings.session_max_age_seconds)


def make_csrf(session_token: str) -> str:
    return csrf_token(settings.portal_secret_key, session_token)


def verify_csrf(session_token: str | None, supplied: str | None) -> None:
    if not session_token or not supplied:
        raise ForbiddenError("Missing CSRF token.")
    if not constant_time_eq(make_csrf(session_token), supplied):
        raise ForbiddenError("CSRF token mismatch — reload the page and try again.")


def dev_signin_allowed() -> bool:
    return settings.portal_env != "production"
