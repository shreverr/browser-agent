"""FastAPI application factory. `uvicorn --factory browser_agent.evals.annotate.app:create_app`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import api, auth
from .broadcast import Broadcaster
from .config import Settings
from .db import make_engine, make_session_factory
from .service import ServiceError
from .storage import BlobStore, make_s3_client

STATIC_DIR = Path(__file__).resolve().parent / "static"

# The viewer renders untrusted live-web text; no inline script/style, nothing third-party.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for k, v in (
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", CSP.encode()),
                ):
                    if k not in present:
                        headers.append((k, v))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    s3_client: Any = None,
    github: auth.GitHubOAuth | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or make_engine(settings.database_url)
    app = FastAPI(
        title="browser-agent annotation server", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.sessions = make_session_factory(engine)
    app.state.store = BlobStore(
        s3_client if s3_client is not None else make_s3_client(settings), settings.s3_bucket
    )
    app.state.github = github or auth.GitHubOAuth(
        settings.github_client_id, settings.github_client_secret
    )
    app.state.broadcaster = Broadcaster()
    app.state.events_cache = api.EventsCache()

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="annotate_session",
        max_age=7 * 24 * 3600,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"detail": [str(e.get("msg")) for e in exc.errors()]}, status_code=422)

    app.include_router(auth.router)
    app.include_router(api.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def index() -> Response:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
