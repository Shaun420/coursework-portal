"""Local blob staging.

Uploaded files are written to local disk before anything else happens, so a
submission survives a Drive outage and analysis never depends on re-downloading
from Google. Drive is the durable archive; this directory is the work area.

Layout: ``<data>/uploads/<assignment_id>/<submission_id><ext>``
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.config import settings
from app.core.errors import UnsupportedFileTypeError, UploadTooLargeError


def uploads_root() -> Path:
    root = settings.portal_data_dir / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def blob_path(assignment_id: int, submission_id: int, filename: str) -> Path:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    d = uploads_root() / str(assignment_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{submission_id}{ext}"


def validate_upload(filename: str, data: bytes) -> None:
    if len(data) > settings.max_upload_bytes:
        raise UploadTooLargeError(
            f"File is {len(data):,} bytes; the limit is {settings.max_upload_bytes:,}.",
            size=len(data),
            limit=settings.max_upload_bytes,
        )
    if not data:
        raise UnsupportedFileTypeError("The uploaded file is empty.")
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in settings.allowed_extensions:
        raise UnsupportedFileTypeError(
            f"'{ext or filename}' is not an accepted file type. "
            f"Accepted: {', '.join(settings.allowed_extensions)}",
            extension=ext,
            allowed=list(settings.allowed_extensions),
        )


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_blob(path: str | None) -> bytes:
    if not path:
        return b""
    p = Path(path)
    return p.read_bytes() if p.exists() else b""


def remove_blob(path: str | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)
