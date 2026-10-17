"""Provider factory."""

from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.core.logging import get_logger
from app.services.drive.base import DriveAccount, DriveFile, DriveProvider
from app.services.drive.google import GoogleDriveProvider
from app.services.drive.mock import MockDriveProvider

log = get_logger(__name__)


@lru_cache(maxsize=1)
def get_drive_provider() -> DriveProvider:
    if settings.drive_provider == "google":
        if not settings.google_configured:
            log.warning(
                "DRIVE_PROVIDER=google but GOOGLE_CLIENT_ID/SECRET are empty — "
                "falling back to the offline mock provider"
            )
            return MockDriveProvider(settings.mock_drive_root)
        log.info("drive provider: google (REST v3)")
        return GoogleDriveProvider()
    log.info("drive provider: mock (root=%s)", settings.mock_drive_root)
    return MockDriveProvider(settings.mock_drive_root)


async def close_drive_provider() -> None:
    await get_drive_provider().close()


__all__ = [
    "DriveAccount",
    "DriveFile",
    "DriveProvider",
    "GoogleDriveProvider",
    "MockDriveProvider",
    "get_drive_provider",
    "close_drive_provider",
]
