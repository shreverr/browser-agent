"""GitHub OAuth + allowlist for humans; the bearer token for machines."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from browser_agent.evals.annotate.config import ConfigError, Settings, parse_allowlist

from .conftest import sign_in


def test_no_session_is_401(anon: TestClient) -> None:
    for path in ("/api/me", "/api/trials", "/api/modes", "/api/events", "/api/taxonomy/export"):
        assert anon.get(path).status_code == 401, path
    r = anon.put(
        "/api/trials/x/note", json={"text": "hi"}, headers={"X-Requested-With": "annotate"}
    )
    assert r.status_code == 401


def test_non_allowlisted_login_is_rejected(anon: TestClient) -> None:
    r = sign_in(anon, "mallory")
    assert r.status_code == 403
    assert "mallory" in r.text and "allowlist" in r.text
    assert anon.get("/api/me").status_code == 401


def test_allowlisted_login_gets_a_session(anon: TestClient) -> None:
    r = sign_in(anon, "Alice")  # GitHub logins are case-insensitive
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert anon.get("/api/me").json() == {"login": "alice"}
    assert "httponly" in r.headers["set-cookie"].lower()
    anon.post("/auth/logout")
    assert anon.get("/api/me").status_code == 401


def test_oauth_state_must_match(anon: TestClient) -> None:
    anon.get("/auth/login", follow_redirects=False)
    r = anon.get("/auth/callback?code=alice&state=forged", follow_redirects=False)
    assert r.status_code == 400
    assert anon.get("/api/me").status_code == 401


def test_removal_from_the_allowlist_takes_effect_immediately(
    app: FastAPI, as_user: Callable[[str], TestClient]
) -> None:
    bob = as_user("bob")
    assert bob.get("/api/me").status_code == 200
    app.state.settings = dataclasses.replace(
        app.state.settings, allowed_github=parse_allowlist("alice")
    )
    assert bob.get("/api/me").status_code == 403


def test_cookie_writes_need_the_csrf_header(
    app: FastAPI, as_user: Callable[[str], TestClient]
) -> None:
    alice = as_user("alice")
    r = alice.post("/api/modes", json={"name": "x"}, headers={"X-Requested-With": ""})
    assert r.status_code == 403
    assert alice.post("/api/modes", json={"name": "x"}).status_code == 201


def test_the_token_cannot_act_as_a_human(uploader: TestClient) -> None:
    assert uploader.post("/api/modes", json={"name": "x"}).status_code == 401
    assert uploader.put("/api/trials/x/note", json={"text": "hi"}).status_code == 401
    assert uploader.get("/api/modes").status_code == 200  # but it can read


def test_security_headers(anon: TestClient) -> None:
    r = anon.get("/")
    assert r.status_code == 200
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert anon.get("/healthz").json() == {"ok": True}


def test_settings_from_env() -> None:
    base = {
        "ANNOTATE_DATABASE_URL": "postgresql://u:p@host/db?sslmode=require",
        "ANNOTATE_S3_BUCKET": "traces",
        "ANNOTATE_SESSION_SECRET": "s" * 32,
        "ANNOTATE_UPLOAD_TOKEN": "t" * 32,
        "ANNOTATE_BASE_URL": "https://annotate.example.com",
        "ANNOTATE_ALLOWED_GITHUB": "Alice, bob",
        "ANNOTATE_GITHUB_CLIENT_ID": "id",
        "ANNOTATE_GITHUB_CLIENT_SECRET": "secret",
    }
    s = Settings.from_env(base)
    assert s.database_url.startswith("postgresql+psycopg://")
    assert s.allowed_github == {"alice", "bob"} and s.cookie_secure
    assert s.callback_url == "https://annotate.example.com/auth/callback"

    missing = {k: v for k, v in base.items() if k != "ANNOTATE_UPLOAD_TOKEN"}
    try:
        Settings.from_env(missing)
        raise AssertionError("expected ConfigError")
    except ConfigError as e:
        assert "ANNOTATE_UPLOAD_TOKEN" in str(e)

    try:  # the dev login bypass is refused off localhost
        Settings.from_env({**base, "ANNOTATE_DEV_LOGIN": "alice"})
        raise AssertionError("expected ConfigError")
    except ConfigError as e:
        assert "localhost" in str(e)
