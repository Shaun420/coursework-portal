"""Offline, filesystem-backed Drive stand-in.

Used when `DRIVE_PROVIDER=mock` (the default) — for tests, for this repo, and
for any deployment that has not been given Google credentials yet. It
implements the full :class:`DriveProvider` contract on disk, so the folder
tree, filenames, sharing and revision behaviour are all exercised exactly as
they would be against real Drive.

Layout on disk::

    <mock_root>/<account>/index.json      # file metadata, mimics Drive records
    <mock_root>/<account>/blobs/<id>      # bytes
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.core.errors import DriveError, NotFoundError
from app.core.logging import get_logger
from app.services.drive.base import FOLDER_MIME, DriveAccount, DriveFile

log = get_logger(__name__)
_SAFE = re.compile(r"[^A-Za-z0-9._@-]+")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MockDriveProvider:
    name = "mock"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # One lock *per account*, and the metadata index is cached in memory.
        # A single global lock plus synchronous file I/O on every call
        # serialises every user in the system behind one blocking write.
        self._locks: dict[str, asyncio.Lock] = {}
        self._index: dict[str, dict[str, dict]] = {}
        self.calls: dict[str, int] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # ── internals ───────────────────────────────────────────────────────────

    @staticmethod
    def _key(account: DriveAccount) -> str:
        return _SAFE.sub("_", (account.email or f"user{account.user_id}").lower()) or "anonymous"

    def _account_dir(self, account: DriveAccount) -> Path:
        d = self.root / self._key(account)
        (d / "blobs").mkdir(parents=True, exist_ok=True)
        return d

    def _read_index(self, account: DriveAccount) -> dict[str, dict]:
        """Cached in memory; read from disk only the first time per account."""
        key = self._key(account)
        cached = self._index.get(key)
        if cached is not None:
            return cached
        p = self._account_dir(account) / "index.json"
        if not p.exists():
            self._index[key] = {}
            return self._index[key]
        try:
            self._index[key] = json.loads(p.read_text())
        except json.JSONDecodeError:
            log.warning("corrupt mock index for %s — starting fresh", account.email)
            self._index[key] = {}
        return self._index[key]

    def _write_index(self, account: DriveAccount, index: dict[str, dict]) -> None:
        self._index[self._key(account)] = index
        p = self._account_dir(account) / "index.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=2, sort_keys=True))
        tmp.replace(p)

    async def _write_index_async(self, account: DriveAccount, index: dict[str, dict]) -> None:
        """Same as _write_index, but the blocking write happens off the loop."""
        self._index[self._key(account)] = index
        await asyncio.to_thread(self._write_index, account, index)

    def _record_to_file(self, rec: dict) -> DriveFile:
        return DriveFile(
            id=rec["id"],
            name=rec["name"],
            parent_id=(rec["parents"] or [None])[0],
            mime_type=rec["mimeType"],
            size=int(rec.get("size", 0)),
            web_view_link=rec.get("webViewLink"),
            modified_at=(
                datetime.fromisoformat(rec["modifiedTime"]) if rec.get("modifiedTime") else None
            ),
            parents=list(rec.get("parents", [])),
        )

    def _new_id(self, seed: str) -> str:
        return hashlib.sha1(f"{seed}:{datetime.now(UTC).isoformat()}".encode()).hexdigest()[:28]

    def _count(self, what: str) -> None:
        self.calls[what] = self.calls.get(what, 0) + 1

    # ── DriveProvider ───────────────────────────────────────────────────────

    async def whoami(self, account: DriveAccount) -> dict:
        return {"id": f"mock-{account.user_id}", "email": account.email, "name": account.email}

    async def ensure_folder(self, account: DriveAccount, parent_id: str, name: str) -> str:
        self._count("ensure_folder")
        async with self._lock_for(self._key(account)):
            index = self._read_index(account)
            for rec in index.values():
                if (
                    rec["mimeType"] == FOLDER_MIME
                    and rec["name"] == name
                    and parent_id in rec["parents"]
                ):
                    return rec["id"]
            fid = self._new_id(f"folder:{parent_id}/{name}")
            index[fid] = {
                "id": fid,
                "name": name,
                "parents": [parent_id],
                "mimeType": FOLDER_MIME,
                "size": 0,
                "modifiedTime": _now(),
                "webViewLink": f"mock://drive/{account.email}/folders/{fid}",
            }
            await self._write_index_async(account, index)
            return fid

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
        self._count("upload")
        async with self._lock_for(self._key(account)):
            index = self._read_index(account)
            if parent_id != "root" and parent_id not in index:
                raise NotFoundError(f"mock parent folder {parent_id} does not exist")
            fid = self._new_id(f"file:{parent_id}/{name}:{len(data)}")
            await asyncio.to_thread((self._account_dir(account) / "blobs" / fid).write_bytes, data)
            rec = {
                "id": fid,
                "name": name,
                "parents": [parent_id],
                "mimeType": mime_type,
                "size": len(data),
                "modifiedTime": _now(),
                "webViewLink": f"mock://drive/{account.email}/files/{fid}",
                "description": description,
            }
            index[fid] = rec
            await self._write_index_async(account, index)
            return self._record_to_file(rec)

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
        self._count("update")
        async with self._lock_for(self._key(account)):
            index = self._read_index(account)
            rec = index.get(file_id)
            if rec is None:
                raise NotFoundError(f"mock file {file_id} does not exist")
            blob_dir = self._account_dir(account) / "blobs"
            if rec["mimeType"] == FOLDER_MIME:
                raise DriveError("cannot write bytes into a folder")
            # Keep the previous revision on disk so history survives, like Drive does.
            prev = blob_dir / file_id
            if prev.exists():
                history = blob_dir / f"{file_id}.history"
                history.mkdir(exist_ok=True)
                (history / f"r{int(rec.get('revisions', 1))}").write_bytes(prev.read_bytes())
            await asyncio.to_thread((blob_dir / file_id).write_bytes, data)
            rec["size"] = len(data)
            rec["mimeType"] = mime_type
            rec["modifiedTime"] = _now()
            rec["revisions"] = int(rec.get("revisions", 1)) + 1
            rec["keepRevisionForever"] = keep_revision_forever
            if new_name:
                rec["name"] = new_name
            await self._write_index_async(account, index)
            return self._record_to_file(rec)

    async def copy(
        self,
        account: DriveAccount,
        file_id: str,
        name: str,
        parent_id: str,
        *,
        description: str | None = None,
    ) -> DriveFile:
        self._count("copy")
        async with self._lock_for(self._key(account)):
            index = self._read_index(account)
            rec = index.get(file_id)
            if rec is None:
                raise NotFoundError(f"mock file {file_id} does not exist")
            new_id = self._new_id(f"copy:{file_id}:{name}")
            src = self._account_dir(account) / "blobs" / file_id
            data = await asyncio.to_thread(src.read_bytes) if src.exists() else b""
            blob = self._account_dir(account) / "blobs" / new_id
            await asyncio.to_thread(blob.write_bytes, data)
            new = {
                **rec,
                "id": new_id,
                "name": name,
                "parents": [parent_id],
                "size": len(data),
                "modifiedTime": _now(),
                "webViewLink": f"mock://drive/{account.email}/files/{new_id}",
                "description": description,
                "revisions": 1,
            }
            new.pop("keepRevisionForever", None)
            index[new_id] = new
            await self._write_index_async(account, index)
            return self._record_to_file(new)

    async def get(self, account: DriveAccount, file_id: str) -> DriveFile | None:
        async with self._lock_for(self._key(account)):
            rec = self._read_index(account).get(file_id)
            return self._record_to_file(rec) if rec else None

    async def download(self, account: DriveAccount, file_id: str) -> bytes:
        self._count("download")
        async with self._lock_for(self._key(account)):
            index = self._read_index(account)
            if file_id not in index:
                raise NotFoundError(f"mock file {file_id} does not exist")
            p = self._account_dir(account) / "blobs" / file_id
            if not p.exists():
                raise DriveError(f"mock blob missing for {file_id}")
            return await asyncio.to_thread(p.read_bytes)

    async def delete(self, account: DriveAccount, file_id: str) -> None:
        self._count("delete")
        async with self._lock_for(self._key(account)):
            index = self._read_index(account)
            rec = index.pop(file_id, None)
            await self._write_index_async(account, index)
            if rec:
                blob = self._account_dir(account) / "blobs" / file_id
                blob.unlink(missing_ok=True)
                shutil.rmtree(blob.parent / f"{file_id}.history", ignore_errors=True)

    async def list_children(
        self, account: DriveAccount, parent_id: str, *, folder_only: bool = False
    ) -> list[DriveFile]:
        async with self._lock_for(self._key(account)):
            index = self._read_index(account)
            out = []
            for rec in index.values():
                if parent_id in rec["parents"]:
                    if folder_only and rec["mimeType"] != FOLDER_MIME:
                        continue
                    out.append(self._record_to_file(rec))
            return sorted(out, key=lambda f: (not f.is_folder, f.name.lower()))

    async def find_child(
        self, account: DriveAccount, parent_id: str, name: str
    ) -> DriveFile | None:
        async with self._lock_for(self._key(account)):
            for rec in self._read_index(account).values():
                if rec["name"] == name and parent_id in rec["parents"]:
                    return self._record_to_file(rec)
            return None

    async def share(
        self, account: DriveAccount, file_id: str, email: str, role: str = "reader"
    ) -> str | None:
        self._count("share")
        async with self._lock_for(self._key(account)):
            index = self._read_index(account)
            rec = index.get(file_id)
            if rec is None:
                raise NotFoundError(f"mock file {file_id} does not exist")
            perms = rec.setdefault("permissions", [])
            for p in perms:
                if p["emailAddress"] == email:
                    p["role"] = role
                    await self._write_index_async(account, index)
                    return p["id"]
            pid = self._new_id(f"perm:{file_id}:{email}")
            perms.append({"id": pid, "type": "user", "role": role, "emailAddress": email})
            await self._write_index_async(account, index)
            return pid

    async def close(self) -> None:
        return None
