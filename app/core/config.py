"""Application configuration.

Everything tunable lives here and is sourced from the environment (or a `.env`
file) so that the same image runs unchanged from a laptop to a production box.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="", extra="ignore"
    )

    # ── Core ────────────────────────────────────────────────────────────────
    portal_env: Literal["development", "staging", "production"] = "development"
    portal_name: str = "Coursework Portal"
    portal_base_url: str = "http://localhost:8000"
    portal_secret_key: str = "insecure-development-key-change-me"
    portal_database_url: str = "sqlite+aiosqlite:///./data/portal.db"
    portal_data_dir: Path = Path("./data")

    session_cookie: str = "cp_session"
    session_max_age_seconds: int = 60 * 60 * 12
    csrf_cookie: str = "cp_csrf"

    # ── Google OAuth 2.0 ────────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_scopes: str = (
        "openid,email,profile,"
        "https://www.googleapis.com/auth/drive.file,"
        "https://www.googleapis.com/auth/drive.appdata"
    )
    google_auth_uri: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_uri: str = "https://oauth2.googleapis.com/token"
    google_userinfo_uri: str = "https://openidconnect.googleapis.com/v1/userinfo"
    google_drive_api: str = "https://www.googleapis.com/drive/v3"
    google_upload_api: str = "https://www.googleapis.com/upload/drive/v3"

    # ── Drive provider ──────────────────────────────────────────────────────
    drive_provider: Literal["google", "mock"] = "mock"
    mock_drive_root: Path = Path("./data/mock_drive")
    teacher_drive_root_id: str = "root"
    institution_email_domain: str = "college.edu"

    # Optional institution-owned Drive used when a course instructor has not
    # linked their own Google account. Submissions are never dropped silently.
    institution_email: str = "portal@college.edu"
    institution_refresh_token: str = ""

    # Bootstrap rule for who may create a course. Roles are derived from data
    # (you teach a course => you are a teacher), so somebody has to be allowed
    # to create the first one. Restricted to the institution's own domain.
    allow_open_course_creation: bool = True

    # ── Academic integrity thresholds ───────────────────────────────────────
    similarity_threshold: float = 0.50
    ai_detection_threshold: float = 0.50
    ai_detection_min_words: int = 120

    # ── Similarity engine ───────────────────────────────────────────────────
    shingle_size: int = 5
    minhash_perms: int = 128
    lsh_bands: int = 32
    similarity_max_comparisons: int = 200

    # ── Analysis pipeline ───────────────────────────────────────────────────
    analysis_workers: int = 2
    analysis_queue_capacity: int = 5000
    analysis_cpu_threads: int = 2

    # ── Uploads / limits ────────────────────────────────────────────────────
    max_upload_bytes: int = 25 * 1024 * 1024
    draft_debounce_ms: int = 1500
    rate_limit_per_minute: int = 240
    draft_rate_limit_per_minute: int = 1200

    # ── Database pooling ────────────────────────────────────────────────────
    db_pool_size: int = 20
    db_max_overflow: int = 40
    db_echo: bool = False

    allowed_extensions: tuple[str, ...] = (
        ".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt", ".tex", ".html",
    )

    @field_validator("similarity_threshold", "ai_detection_threshold")
    @classmethod
    def _valid_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("threshold must be between 0.00 and 1.00")
        return v

    @field_validator("google_scopes")
    @classmethod
    def _strip_scopes(cls, v: str) -> str:
        return ",".join(s.strip() for s in v.split(",") if s.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def scope_list(self) -> list[str]:
        return self.google_scopes.split(",")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redirect_uri(self) -> str:
        return f"{self.portal_base_url.rstrip('/')}/auth/google/callback"

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def ensure_dirs(self) -> None:
        self.portal_data_dir.mkdir(parents=True, exist_ok=True)
        (self.portal_data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        if self.drive_provider == "mock":
            self.mock_drive_root.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
