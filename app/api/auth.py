"""Authentication routes: Google OAuth + development sign-in + session state."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.deps import DB, DBW, CurrentUser
from app.core.config import settings
from app.core.errors import UnauthorizedError, ValidationError
from app.core.logging import get_logger
from app.core.security import csrf_token
from app.db.repositories import UserRepository
from app.domain.entities import User
from app.domain.enums import Role
from app.domain.schemas import UserOut
from app.services import auth as auth_service

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session(response, user: User) -> None:  # noqa: ANN001
    token = auth_service.make_session_token(user)
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.portal_env == "production",
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie,
        csrf_token(settings.portal_secret_key, token),
        max_age=settings.session_max_age_seconds,
        httponly=False,  # JS reads this for the double-submit header
        samesite="lax",
        secure=settings.portal_env == "production",
        path="/",
    )


@router.get("/google/login")
async def google_login(request: Request, redirect: str = "/") -> RedirectResponse:
    url, state, _verifier = auth_service.begin_login(redirect)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        "cp_oauth_redirect", redirect, max_age=600, httponly=True, samesite="lax", path="/"
    )
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request, db: DBW, code: str = "", state: str = "", error: str = ""
):
    if error:
        raise UnauthorizedError(f"Google sign-in was cancelled: {error}")
    if not code or not state:
        raise UnauthorizedError("Google returned an incomplete response.")

    verifier = auth_service.consume_state(state)
    tokens = await auth_service.exchange_code(code, verifier)
    identity = await auth_service.fetch_identity(tokens["access_token"])
    user = await auth_service.upsert_google_user(db, identity, tokens)
    await db.commit()

    redirect = request.cookies.get("cp_oauth_redirect", "/") or "/"
    response = RedirectResponse(redirect, status_code=302)
    response.delete_cookie("cp_oauth_redirect", path="/")
    _set_session(response, user)
    return response


@router.post("/dev/login")
async def dev_login(request: Request, db: DBW) -> JSONResponse:
    """Offline sign-in for development and tests. Disabled in production."""
    if not auth_service.dev_signin_allowed():
        raise UnauthorizedError("Development sign-in is disabled in production.")

    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise ValidationError("A valid email is required.")
    name = (body.get("name") or email.split("@")[0]).strip()
    roll = (body.get("roll_number") or "").strip() or None

    users = UserRepository(db)
    user = await users.by_email(email)
    if user is None:
        user = await users.create(
            google_sub=f"dev-{email}",
            email=email,
            name=name,
            roll_number=roll,
            role=Role.STUDENT.value,
        )
    else:
        user.name = name or user.name
        user.roll_number = roll or user.roll_number
    user.drive_linked = True
    user.drive_email = email
    user.role = await auth_service.resolve_role(db, user)
    await db.commit()

    response = JSONResponse({"user": UserOut.model_validate(user).model_dump()})
    _set_session(response, user)
    return response


@router.post("/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(settings.session_cookie, path="/")
    response.delete_cookie(settings.csrf_cookie, path="/")
    return response


@router.get("/me")
async def me(user: CurrentUser, db: DB) -> dict:
    enrolled = await UserRepository(db).courses_for(user.id)
    return {
        "user": UserOut.model_validate(user).model_dump(),
        "google_configured": settings.google_configured,
        "drive_provider": settings.drive_provider,
        "dev_signin": auth_service.dev_signin_allowed(),
        "csrf": csrf_token(
            settings.portal_secret_key, auth_service.make_session_token(user)
        ),
        "courses": [
            {"id": c.id, "code": c.code, "title": c.title} for c in enrolled
        ],
        "thresholds": {
            "similarity": settings.similarity_threshold,
            "ai_detection": settings.ai_detection_threshold,
        },
    }
