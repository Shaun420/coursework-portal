"""Drive provider contract.

Two implementations ship with the app:

* :class:`app.services.drive.google.GoogleDriveProvider` — real Drive REST v3.
* :class:`app.services.drive.mock.MockDriveProvider` — offline, filesystem
  backed, used for tests and for running this repo without Google credentials.

Everything above this layer (draft autosave, submission archiving, reports)
talks only to this protocol, so swapping storage is a config change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

FOLDER_MIME = "application/vnd.google-apps.folder"


@dataclass(slots=True)
class DriveFile:
    id: str
    name: str
    parent_id: str | None
    mime_type: str
    size: int = 0
    web_view_link: str | None = None
    modified_at: datetime | None = None
    parents: list[str] = field(default_factory=list)

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


@dataclass(slots=True)
class DriveAccount:
    """Per-user credentials the provider needs to act on someone's behalf."""

    user_id: int
    email: str
    refresh_token: str | None = None
    access_token: str | None = None
    expires_at: datetime | None = None


@runtime_checkable
class DriveProvider(Protocol):
    name: str

    async def whoami(self, account: DriveAccount) -> dict:
        """Return {'id', 'email', 'name'} for the linked account."""

    async def ensure_folder(self, account: DriveAccount, parent_id: str, name: str) -> str:
        """Idempotently create `parent_id/name`, returning the folder id."""

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
        """Create a new file inside `parent_id`."""

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
        """Overwrite the contents of an existing file (used by draft autosave)."""

    async def copy(
        self,
        account: DriveAccount,
        file_id: str,
        name: str,
        parent_id: str,
        *,
        description: str | None = None,
    ) -> DriveFile:
        """Server-side copy — no bytes leave Google when both files are on Drive."""

    async def get(self, account: DriveAccount, file_id: str) -> DriveFile | None: ...

    async def download(self, account: DriveAccount, file_id: str) -> bytes: ...

    async def delete(self, account: DriveAccount, file_id: str) -> None: ...

    async def list_children(
        self, account: DriveAccount, parent_id: str, *, folder_only: bool = False
    ) -> list[DriveFile]: ...

    async def find_child(
        self, account: DriveAccount, parent_id: str, name: str
    ) -> DriveFile | None: ...

    async def share(
        self, account: DriveAccount, file_id: str, email: str, role: str = "reader"
    ) -> str | None:
        """Grant `email` access; returns the permission id."""

    async def close(self) -> None: ...
