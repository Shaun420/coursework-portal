"""Shared FastAPI dependencies: identity, authorisation, rate limiting."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import RateLimiter
from app.db.repositories import UserRepository
from app.db.session import get_db, get_db_write, session_scope
from app.domain.entities import User
from app.domain.enums import Role
from app.services.auth import read_session

_limiter = RateLimiter()


def client_key(request: Request) -> str:
    """Stable per-caller key: authenticated id where known, else IP."""
    cookie = request.cookies.get(settings.session_cookie)
    data = read_session(cookie)
    if data:
        return f"u{data['user_id']}"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "?"
    return f"ip:{ip}"


async def apply_rate_limit(request: Request, *, limit: int | None = None) -> None:
    lim = limit or settings.rate_limit_per_minute
    key = client_key(request)
    result = _limiter.check(key, lim)
    if not result.allowed:
        raise ForbiddenError(
            f"Rate limit exceeded ({result.limit}/min). Try again in {result.retry_after:.0f}s.",
            retry_after=round(result.retry_after, 1),
        )


async def current_user(request: Request) -> User:
    """Resolve the session cookie to a user, then release the connection.

    This deliberately does *not* take the request-scoped `get_db` session: a
    FastAPI dependency's session lives until the response has been sent, so
    depending on one here would pin a pooled connection to every single request
    — including the ones that open further sessions of their own. At a few
    hundred concurrent users that alone exhausts the pool.
    """
    token = request.cookies.get(settings.session_cookie)
    data = read_session(token)
    if not data:
        raise UnauthorizedError("You are not signed in.")
    async with session_scope() as db:
        user = await UserRepository(db).get(int(data["user_id"]))
    if user is None:
        raise UnauthorizedError("That session refers to an account that no longer exists.")
    return user


async def optional_user(request: Request) -> User | None:
    try:
        return await current_user(request)
    except UnauthorizedError:
        return None


async def current_teacher(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role not in (Role.TEACHER.value, Role.ADMIN.value):
        raise ForbiddenError("This action requires a teacher account.")
    return user


async def current_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != Role.ADMIN.value:
        raise ForbiddenError("This action requires an administrator account.")
    return user


DBW = Annotated[AsyncSession, Depends(get_db_write)]
CurrentUser = Annotated[User, Depends(current_user)]
CurrentTeacher = Annotated[User, Depends(current_teacher)]
CurrentAdmin = Annotated[User, Depends(current_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


def reset_rate_limiter() -> None:
    """Test hook."""
    _limiter.reset()
