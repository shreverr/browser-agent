"""Authentication: GitHub OAuth + allowlist for humans, a bearer token for machines.

- Humans sign in with GitHub; the session cookie (Starlette SessionMiddleware, signed with
  itsdangerous) holds only their login. The allowlist is re-checked on every request, so
  removing someone from ANNOTATE_ALLOWED_GITHUB locks them out immediately.
- The upload CLI and Claude's proposal step use `Authorization: Bearer ANNOTATE_UPLOAD_TOKEN`.
  That token can upload, propose and read; it can never create, rename or merge modes.
- Cookie-authenticated writes must carry `X-Requested-With: annotate`, which a cross-site
  form cannot send without a CORS preflight the server never grants.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from html import escape
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .config import Settings

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "annotate"


class GitHubOAuth:
    authorize_endpoint = "https://github.com/login/oauth/authorize"
    token_endpoint = "https://github.com/login/oauth/access_token"
    user_endpoint = "https://api.github.com/user"

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        # No scope: the public profile (login) is all we need.
        q = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "allow_signup": "false",
        }
        return f"{self.authorize_endpoint}?{urlencode(q)}"

    def login_for_code(self, code: str, redirect_uri: str) -> str:
        with httpx.Client(timeout=10) as client:
            r = client.post(
                self.token_endpoint,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            r.raise_for_status()
            token = r.json().get("access_token")
            if not token:
                raise HTTPException(401, "GitHub did not return an access token")
            u = client.get(
                self.user_endpoint,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            u.raise_for_status()
            return str(u.json()["login"])


@dataclass(frozen=True)
class Principal:
    login: str | None  # None for the bearer token

    @property
    def is_human(self) -> bool:
        return self.login is not None


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def _bearer_ok(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[7:].strip()
    return hmac.compare_digest(token.encode(), settings_of(request).upload_token.encode())


def _session_login(request: Request) -> str | None:
    login = request.session.get("login")
    if not login:
        return None
    if not settings_of(request).is_allowed(login):
        request.session.clear()
        raise HTTPException(403, "your GitHub login is no longer on the allowlist")
    return str(login)


def require_human(request: Request) -> str:
    login = _session_login(request)
    if login is None:
        raise HTTPException(401, "sign in with GitHub")
    return login


def require_human_write(login: Annotated[str, Depends(require_human)], request: Request) -> str:
    if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise HTTPException(403, f"missing {CSRF_HEADER}: {CSRF_VALUE} header")
    return login


def require_token(request: Request) -> None:
    if not _bearer_ok(request):
        raise HTTPException(401, "bearer upload token required", {"WWW-Authenticate": "Bearer"})


def require_reader(request: Request) -> Principal:
    if request.headers.get("authorization"):
        require_token(request)
        return Principal(None)
    return Principal(require_human(request))


Human = Annotated[str, Depends(require_human)]
HumanWrite = Annotated[str, Depends(require_human_write)]
Reader = Annotated[Principal, Depends(require_reader)]
Token = Annotated[None, Depends(require_token)]


def _page(title: str, body: str, status: int) -> HTMLResponse:
    html = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<link rel=stylesheet href='/static/app.css'>"  # the CSP forbids inline styles
        f"<title>{escape(title)}</title><body><div class=gate>"
        f"<h1>{escape(title)}</h1><p>{body}</p></div>"
    )
    return HTMLResponse(html, status_code=status)


router = APIRouter()


@router.get("/auth/login", include_in_schema=False)
def login(request: Request) -> RedirectResponse:
    s = settings_of(request)
    if s.dev_login:
        # Local development only (config refuses it unless ANNOTATE_BASE_URL is localhost).
        if not s.is_allowed(s.dev_login):
            raise HTTPException(403, "ANNOTATE_DEV_LOGIN is not on ANNOTATE_ALLOWED_GITHUB")
        request.session.clear()
        request.session["login"] = s.dev_login
        return RedirectResponse("/", 303)
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    github: GitHubOAuth = request.app.state.github
    return RedirectResponse(github.authorize_url(state, s.callback_url), 303)


@router.get("/auth/callback", include_in_schema=False, response_model=None)
def callback(request: Request, code: str = "", state: str = "") -> Response:
    s = settings_of(request)
    expected = request.session.pop("oauth_state", None)
    if not code or not expected or not hmac.compare_digest(state, expected):
        return _page(
            "Sign-in failed", "The sign-in link expired. <a href='/auth/login'>Try again</a>.", 400
        )
    github: GitHubOAuth = request.app.state.github
    gh_login = github.login_for_code(code, s.callback_url)
    if not s.is_allowed(gh_login):
        request.session.clear()
        return _page(
            "Not on the allowlist",
            f"GitHub user <b>{escape(gh_login)}</b> is not allowed to use this server. "
            "Ask the owner to add you to ANNOTATE_ALLOWED_GITHUB.",
            403,
        )
    request.session.clear()
    request.session["login"] = gh_login.lower()
    return RedirectResponse("/", 303)


@router.post("/auth/logout", include_in_schema=False)
def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}


@router.get("/api/me")
def me(login: Human) -> dict[str, str]:
    return {"login": login}
