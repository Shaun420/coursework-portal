"""Real Google Drive REST v3 provider.

Design notes that matter for a 10k-user single node:

* One shared ``httpx.AsyncClient`` with HTTP/1.1 keep-alive and a bounded
  connection pool — no per-request TLS handshakes, which is what actually caps
  concurrency against ``www.googleapis.com``.
* Access tokens are cached per account and refreshed ~60 s before expiry,
  under a per-account lock, so a burst of N requests triggers exactly one
  refresh instead of N.
* Retries with exponential backoff + ``Retry-After`` for 429/5xx, because
  Drive's per-user quota is per-second and submissions arrive in deadline
  spikes.
"""

from __future__ import annotations

import asyncio
import json
import random
import urllib.parse
from datetime import UTC, datetime, timedelta

import httpx

from app.core.config import settings
from app.core.errors import DriveError, GoogleNotConfiguredError, NotFoundError
from app.core.logging import get_logger
from app.services.drive.base import FOLDER_MIME, DriveAccount, DriveFile

log = get_logger(__name__)

RETRYABLE = {408, 429, 500, 502, 503, 504}


def _q(value: str) -> str:
    """Escape a literal for a Drive `name = '...'` query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_time(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


class GoogleDriveProvider:
    name = "google"

    FILE_FIELDS = "id,name,mimeType,size,parents,webViewLink,modifiedTime,description"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._tokens: dict[int, tuple[str, datetime]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._folder_cache: dict[tuple[int, str, str], str] = {}

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0),
                limits=httpx.Limits(
                    max_connections=settings.analysis_cpu_threads * 32 or 64,
                    max_keepalive_connections=64,
                    keepalive_expiry=90.0,
                ),
                http2=False,
            )
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── auth ────────────────────────────────────────────────────────────────

    def _lock_for(self, account: DriveAccount) -> asyncio.Lock:
        lock = self._locks.get(account.user_id)
        if lock is None:
            lock = self._locks[account.user_id] = asyncio.Lock()
        return lock

    async def access_token(self, account: DriveAccount) -> str:
        if not settings.google_client_id or not settings.google_client_secret:
            raise GoogleNotConfiguredError(
                "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set"
            )
        if not account.refresh_token:
            raise DriveError("this account has no stored refresh token")

        cached = self._tokens.get(account.user_id)
        if cached and cached[1] > datetime.now(UTC) + timedelta(seconds=60):
            return cached[0]

        async with self._lock_for(account):
            cached = self._tokens.get(account.user_id)
            if cached and cached[1] > datetime.now(UTC) + timedelta(seconds=60):
                return cached[0]
            client = await self._http()
            try:
                resp = await client.post(
                    settings.google_token_uri,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": account.refresh_token,
                        "client_id": settings.google_client_id,
                        "client_secret": settings.google_client_secret,
                    },
                )
            except httpx.HTTPError as exc:  # network level
                raise DriveError(f"token refresh failed: {exc}") from exc
            if resp.status_code >= 400:
                raise DriveError(
                    f"token refresh rejected ({resp.status_code})", body=resp.text[:300]
                )
            data = resp.json()
            token = data["access_token"]
            expires = datetime.now(UTC) + timedelta(
                seconds=int(data.get("expires_in", 3600))
            )
            self._tokens[account.user_id] = (token, expires)
            return token

    def invalidate_token(self, account: DriveAccount) -> None:
        self._tokens.pop(account.user_id, None)

    async def _request(self, account: DriveAccount, method: str, url: str, **kw) -> httpx.Response:
        """Request with bearer auth, one re-auth on 401 and capped retries."""
        client = await self._http()
        last_exc: Exception | None = None
        for attempt in range(4):
            token = await self.access_token(account)
            headers = dict(kw.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {token}"
            try:
                resp = await client.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(min(2 ** attempt * 0.25, 4))
                continue

            if resp.status_code == 401 and attempt == 0:
                self.invalidate_token(account)
                continue
            if resp.status_code in RETRYABLE and attempt < 3:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else (
                    (2 ** attempt) * 0.5 + random.uniform(0, 0.4)
                )
                log.warning(
                    "drive %s %s -> %s, retrying in %.2fs",
                    method, url, resp.status_code, delay,
                )
                await asyncio.sleep(min(delay, 30))
                continue
            return resp

        raise DriveError(f"drive request failed after retries: {last_exc}")

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _to_file(payload: dict) -> DriveFile:
        parents = payload.get("parents") or []
        return DriveFile(
            id=payload["id"],
            name=payload.get("name", ""),
            parent_id=parents[0] if parents else None,
            mime_type=payload.get("mimeType", "application/octet-stream"),
            size=int(payload.get("size") or 0),
            web_view_link=payload.get("webViewLink"),
            modified_at=_parse_time(payload.get("modifiedTime")),
            parents=parents,
        )

    # ── DriveProvider ───────────────────────────────────────────────────────

    async def whoami(self, account: DriveAccount) -> dict:
        resp = await self._request(
            account, "GET", f"{settings.google_drive_api}/about?fields=user"
        )
        if resp.status_code >= 400:
            raise DriveError(f"whoami failed ({resp.status_code})", body=resp.text[:300])
        user = resp.json().get("user", {})
        return {
            "id": user.get("permissionId", ""),
            "email": user.get("emailAddress", account.email),
            "name": user.get("displayName", ""),
        }

    async def ensure_folder(self, account: DriveAccount, parent_id: str, name: str) -> str:
        cache_key = (account.user_id, parent_id, name)
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        query = (
            f"'{_q(parent_id)}' in parents and "
            f"mimeType = '{FOLDER_MIME}' and name = '{_q(name)}' and trashed = false"
        )
        resp = await self._request(
            account,
            "GET",
            f"{settings.google_drive_api}/files",
            params={
                "q": query,
                "fields": f"files({self.FILE_FIELDS})",
                "pageSize": 1,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        if resp.status_code >= 400:
            raise DriveError(f"folder lookup failed ({resp.status_code})", body=resp.text[:300])
        files = resp.json().get("files", [])
        if files:
            fid = files[0]["id"]
            self._folder_cache[cache_key] = fid
            return fid

        resp = await self._request(
            account,
            "POST",
            f"{settings.google_drive_api}/files",
            params={"supportsAllDrives": "true"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        )
        if resp.status_code >= 400:
            raise DriveError(f"folder create failed ({resp.status_code})", body=resp.text[:300])
        fid = resp.json()["id"]
        self._folder_cache[cache_key] = fid
        return fid

    async def _multipart(
        self,
        account: DriveAccount,
        method: str,
        url: str,
        metadata: dict,
        data: bytes,
        mime_type: str,
        filename: str,
    ) -> DriveFile:
        boundary = f"----cp{random.getrandbits(64):016x}"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Type: application/json; charset=UTF-8\r\n\r\n',
                json.dumps(metadata).encode(),
                b"\r\n",
                f"--{boundary}\r\n".encode(),
                f'Content-Type: {mime_type}\r\n'.encode(),
                (
                    f'Content-Disposition: form-data; name="file"; '
                    f'filename="{filename}"\r\n\r\n'
                ).encode(),
                data,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        resp = await self._request(
            account,
            method,
            url,
            params={"uploadType": "multipart", "supportsAllDrives": "true"},
            content=body,
            headers={"Content-Type": f'multipart/related; boundary="{boundary}"'},
        )
        if resp.status_code >= 400:
            raise DriveError(f"upload failed ({resp.status_code})", body=resp.text[:300])
        return self._to_file(resp.json())

    async def upload(
        self,
        account: DriveAccount,
        parent_id: str,
        name: str,
        data: bytes,
        mime_type: str,
        *,
        description: str | None = None,
    ) -> DriveFile:
        metadata: dict = {"name": name, "parents": [parent_id], "mimeType": mime_type}
        if description:
            metadata["description"] = description
        return await self._multipart(
            account,
            "POST",
            f"{settings.google_upload_api}/files",
            metadata,
            data,
            mime_type,
            name,
        )

    async def update(
        self,
        account: DriveAccount,
        file_id: str,
        data: bytes,
        mime_type: str,
        *,
        new_name: str | None = None,
        keep_revision_forever: bool = False,
    ) -> DriveFile:
        metadata: dict = {"mimeType": mime_type, "keepRevisionForever": keep_revision_forever}
        if new_name:
            metadata["name"] = new_name
        return await self._multipart(
            account,
            "PATCH",
            f"{settings.google_upload_api}/files/{urllib.parse.quote(file_id)}",
            metadata,
            data,
            mime_type,
            new_name or file_id,
        )

    async def copy(
        self,
        account: DriveAccount,
        file_id: str,
        name: str,
        parent_id: str,
        *,
        description: str | None = None,
    ) -> DriveFile:
        body: dict = {"name": name, "parents": [parent_id]}
        if description:
            body["description"] = description
        resp = await self._request(
            account,
            "POST",
            f"{settings.google_drive_api}/files/{urllib.parse.quote(file_id)}/copy",
            params={"supportsAllDrives": "true"},
            json=body,
        )
        if resp.status_code >= 400:
            raise DriveError(f"copy failed ({resp.status_code})", body=resp.text[:300])
        return self._to_file(resp.json())

    async def get(self, account: DriveAccount, file_id: str) -> DriveFile | None:
        resp = await self._request(
            account,
            "GET",
            f"{settings.google_drive_api}/files/{urllib.parse.quote(file_id)}",
            params={"fields": self.FILE_FIELDS, "supportsAllDrives": "true"},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise DriveError(f"get failed ({resp.status_code})", body=resp.text[:300])
        return self._to_file(resp.json())

    async def download(self, account: DriveAccount, file_id: str) -> bytes:
        resp = await self._request(
            account,
            "GET",
            f"{settings.google_drive_api}/files/{urllib.parse.quote(file_id)}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        if resp.status_code == 404:
            raise NotFoundError(f"drive file {file_id} not found")
        if resp.status_code >= 400:
            raise DriveError(f"download failed ({resp.status_code})", body=resp.text[:300])
        return resp.content

    async def delete(self, account: DriveAccount, file_id: str) -> None:
        resp = await self._request(
            account,
            "DELETE",
            f"{settings.google_drive_api}/files/{urllib.parse.quote(file_id)}",
            params={"supportsAllDrives": "true"},
        )
        if resp.status_code >= 400 and resp.status_code != 404:
            raise DriveError(f"delete failed ({resp.status_code})", body=resp.text[:300])

    async def list_children(
        self, account: DriveAccount, parent_id: str, *, folder_only: bool = False
    ) -> list[DriveFile]:
        base = f"'{_q(parent_id)}' in parents and trashed = false"
        if folder_only:
            base += f" and mimeType = '{FOLDER_MIME}'"
        out: list[DriveFile] = []
        page_token: str | None = None
        while True:
            params: dict = {
                "q": base,
                "fields": f"nextPageToken,files({self.FILE_FIELDS})",
                "pageSize": 500,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            resp = await self._request(
                account, "GET", f"{settings.google_drive_api}/files", params=params
            )
            if resp.status_code >= 400:
                raise DriveError(f"list failed ({resp.status_code})", body=resp.text[:300])
            payload = resp.json()
            out.extend(self._to_file(f) for f in payload.get("files", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return sorted(out, key=lambda f: (not f.is_folder, f.name.lower()))

    async def find_child(
        self, account: DriveAccount, parent_id: str, name: str
    ) -> DriveFile | None:
        for f in await self.list_children(account, parent_id):
            if f.name == name:
                return f
        return None

    async def share(
        self, account: DriveAccount, file_id: str, email: str, role: str = "reader"
    ) -> str | None:
        resp = await self._request(
            account,
            "POST",
            f"{settings.google_drive_api}/files/{urllib.parse.quote(file_id)}/permissions",
            params={
                "sendNotificationEmail": "false",
                "supportsAllDrives": "true",
                "transferOwnership": "false",
            },
            json={"type": "user", "role": role, "emailAddress": email},
        )
        if resp.status_code >= 400:
            log.warning("share %s with %s failed: %s", file_id, email, resp.text[:200])
            return None
        return resp.json().get("id")
