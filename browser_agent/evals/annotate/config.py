"""Server configuration, read from the environment only (see README.md for every variable)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(RuntimeError):
    pass


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_database_url(url: str) -> str:
    """Neon hands out `postgresql://…`; SQLAlchemy needs the psycopg 3 driver named."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str
    s3_bucket: str
    session_secret: str
    upload_token: str
    base_url: str
    allowed_github: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    github_client_id: str = ""
    github_client_secret: str = ""
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str = "auto"
    cookie_secure: bool = True
    blob_redirect: bool = False
    max_upload_bytes: int = 64 * 1024 * 1024
    dev_login: str | None = None

    @property
    def callback_url(self) -> str:
        return self.base_url.rstrip("/") + "/auth/callback"

    def is_allowed(self, login: str | None) -> bool:
        return login is not None and login.lower() in self.allowed_github

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)

        def get(name: str, default: str = "") -> str:
            return e.get(name, default).strip()

        required = {
            "ANNOTATE_DATABASE_URL": get("ANNOTATE_DATABASE_URL") or get("DATABASE_URL"),
            "ANNOTATE_S3_BUCKET": get("ANNOTATE_S3_BUCKET"),
            "ANNOTATE_SESSION_SECRET": get("ANNOTATE_SESSION_SECRET"),
            "ANNOTATE_UPLOAD_TOKEN": get("ANNOTATE_UPLOAD_TOKEN"),
            "ANNOTATE_BASE_URL": get("ANNOTATE_BASE_URL"),
            "ANNOTATE_ALLOWED_GITHUB": get("ANNOTATE_ALLOWED_GITHUB"),
        }
        dev_login = get("ANNOTATE_DEV_LOGIN") or None
        if not dev_login:
            required["ANNOTATE_GITHUB_CLIENT_ID"] = get("ANNOTATE_GITHUB_CLIENT_ID")
            required["ANNOTATE_GITHUB_CLIENT_SECRET"] = get("ANNOTATE_GITHUB_CLIENT_SECRET")
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ConfigError("missing environment variables: " + ", ".join(missing))

        base_url = required["ANNOTATE_BASE_URL"]
        if dev_login and (urlparse(base_url).hostname or "") not in LOCAL_HOSTS:
            raise ConfigError(
                "ANNOTATE_DEV_LOGIN bypasses GitHub OAuth and is only allowed when "
                "ANNOTATE_BASE_URL points at localhost"
            )
        if len(required["ANNOTATE_UPLOAD_TOKEN"]) < 24:
            raise ConfigError("ANNOTATE_UPLOAD_TOKEN must be at least 24 characters")
        if len(required["ANNOTATE_SESSION_SECRET"]) < 24:
            raise ConfigError("ANNOTATE_SESSION_SECRET must be at least 24 characters")

        return cls(
            database_url=normalize_database_url(required["ANNOTATE_DATABASE_URL"]),
            s3_bucket=required["ANNOTATE_S3_BUCKET"],
            session_secret=required["ANNOTATE_SESSION_SECRET"],
            upload_token=required["ANNOTATE_UPLOAD_TOKEN"],
            base_url=base_url,
            allowed_github=parse_allowlist(required["ANNOTATE_ALLOWED_GITHUB"]),
            github_client_id=get("ANNOTATE_GITHUB_CLIENT_ID"),
            github_client_secret=get("ANNOTATE_GITHUB_CLIENT_SECRET"),
            s3_endpoint_url=get("ANNOTATE_S3_ENDPOINT_URL") or None,
            s3_access_key_id=get("ANNOTATE_S3_ACCESS_KEY_ID") or None,
            s3_secret_access_key=get("ANNOTATE_S3_SECRET_ACCESS_KEY") or None,
            s3_region=get("ANNOTATE_S3_REGION", "auto") or "auto",
            cookie_secure=_flag(e.get("ANNOTATE_COOKIE_SECURE"), True),
            blob_redirect=_flag(e.get("ANNOTATE_BLOB_REDIRECT"), False),
            max_upload_bytes=int(get("ANNOTATE_MAX_UPLOAD_BYTES") or 64 * 1024 * 1024),
            dev_login=dev_login.lower() if dev_login else None,
        )


def parse_allowlist(raw: str) -> frozenset[str]:
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())
