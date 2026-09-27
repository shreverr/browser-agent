"""Fixtures for the annotation server: a migrated database, a fake S3 client, fake GitHub.

Uses ANNOTATE_TEST_DATABASE_URL (a disposable Postgres) when set, else a SQLite file.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("fastapi", reason="needs the annotate extra: uv sync --extra annotate")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, text  # noqa: E402

from browser_agent.evals.annotate.app import create_app  # noqa: E402
from browser_agent.evals.annotate.config import Settings, parse_allowlist  # noqa: E402
from browser_agent.evals.annotate.db import make_engine, migrate  # noqa: E402
from browser_agent.evals.annotate.models import Base  # noqa: E402

TOKEN = "test-upload-token-0123456789abcdef"
ALLOWED = "alice,bob,Carol"


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._io = io.BytesIO(data)

    def read(self) -> bytes:
        return self._io.read()

    def iter_chunks(self, chunk_size: int = 1024) -> Iterator[bytes]:
        while chunk := self._io.read(chunk_size):
            yield chunk

    def close(self) -> None:
        pass


class FakeS3Client:
    """In-memory stand-in for the boto3 S3 client calls BlobStore makes."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.puts = 0

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        self.objects[(Bucket, Key)] = (bytes(Body), ContentType)
        self.puts += 1

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        data, ctype = self.objects[(Bucket, Key)]
        return {"Body": FakeBody(data), "ContentType": ctype}

    def generate_presigned_url(self, op: str, Params: dict[str, str], ExpiresIn: int) -> str:  # noqa: N803
        return f"https://r2.test/{Params['Bucket']}/{Params['Key']}?X-Expires={ExpiresIn}"


class FakeGitHub:
    """`code` is the GitHub login the fake OAuth exchange returns."""

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return f"https://github.test/login/oauth/authorize?state={state}"

    def login_for_code(self, code: str, redirect_uri: str) -> str:
        return code


def _reset_postgres(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = os.environ.get("ANNOTATE_TEST_DATABASE_URL")
    if url:
        engine = make_engine(url)
        _reset_postgres(engine)
        engine.dispose()
    else:
        url = f"sqlite:///{tmp_path / 'annotate.db'}"
    migrate(url)
    return url


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings(
        database_url=db_url,
        s3_bucket="traces",
        session_secret="test-session-secret-0123456789",
        upload_token=TOKEN,
        base_url="http://testserver",
        allowed_github=parse_allowlist(ALLOWED),
        github_client_id="cid",
        github_client_secret="csecret",
        cookie_secure=False,
    )


@pytest.fixture
def s3() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture
def app(settings: Settings, s3: FakeS3Client) -> Iterator[Any]:
    engine = make_engine(settings.database_url)
    application = create_app(settings, engine=engine, s3_client=s3, github=FakeGitHub())  # type: ignore[arg-type]
    yield application
    engine.dispose()


@pytest.fixture
def anon(app: Any) -> TestClient:
    return TestClient(app)


@pytest.fixture
def uploader(app: Any) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})


def sign_in(client: TestClient, login: str) -> Any:
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 303, r.text
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return client.get(f"/auth/callback?code={login}&state={state}", follow_redirects=False)


@pytest.fixture
def as_user(app: Any) -> Callable[[str], TestClient]:
    def make(login: str) -> TestClient:
        c = TestClient(app, headers={"X-Requested-With": "annotate"})
        r = sign_in(c, login)
        assert r.status_code == 303, r.text
        return c

    return make


# ---------- ea-1 Trace builder ----------


def canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class TraceDir:
    """Writes Trials in the on-disk layout of TRACE_FORMAT.md under <root>/.evals."""

    def __init__(self, root: Path) -> None:
        self.evals = root / ".evals"
        self.blobs = self.evals / "blobs"

    def blob(self, data: bytes, media_type: str) -> dict[str, Any]:
        sha = hashlib.sha256(data).hexdigest()
        p = self.blobs / "sha256" / sha[:2] / sha
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return {"sha256": sha, "bytes": len(data), "media_type": media_type}

    def trial(
        self,
        trial_id: str,
        *,
        batch: str = "2026-09-28-a",
        status: str = "completed",
        steps: int = 2,
        answer: str = "Found 3 chargers",
        page_text: str = "PlugShare map",
    ) -> Path:
        system = self.blob(b"You are a browser agent.", "application/json")
        tools = self.blob(canon([{"name": "click"}]), "application/json")
        task_id = trial_id.split(".")[0]
        header = {
            "trace_schema_version": "ea-1",
            "trial_id": trial_id,
            "batch_id": batch,
            "attempt": 1,
            "kind": "task",
            "task": {
                "id": task_id,
                "text": "Find EV chargers near Barstow",
                "start_url": "https://www.plugshare.com/",
                "sites": ["plugshare.com"],
                "cell": {
                    "task_type": "research",
                    "horizon": "medium",
                    "interaction": ["filters", "map"],
                },
                "notes": "",
            },
            "variant": {
                "model": "qwen/qwen3.7-flash",
                "system_prompt_ref": system,
                "tools_ref": tools,
            },
            "simulated_user": [{"id": "dates", "match": "date|when", "reply": "20-22 October"}],
            "status": status,
            "terminal_reason": "done" if status == "completed" else None,
            "answer": answer,
            "automation_outcome": "succeeded",
            "unscripted_fallback": False,
            "infra": None,
            "totals": {
                "steps": steps,
                "cost_usd": 0.0123,
                "prompt_tokens": 100,
                "completion_tokens": 10,
            },
        }
        events: list[dict[str, Any]] = []
        for step in range(1, steps + 1):
            rendered = self.blob(f"{page_text} step {step}".encode(), "text/plain")
            msg = self.blob(canon({"role": "user", "content": f"obs {step}"}), "application/json")
            base = {"t_wall": "2026-09-28T10:00:00Z", "t_mono": float(step), "step": step}
            events.append(
                {
                    **base,
                    "type": "observation",
                    "observation_id": f"o{step}",
                    "url": "https://www.plugshare.com/",
                    "title": "PlugShare",
                    "rendered_ref": rendered,
                    "structured_ref": None,
                    "n_controls": 10,
                    "truncated": False,
                }
            )
            events.append(
                {
                    **base,
                    "type": "model_call",
                    "role": "agent",
                    "message_refs": [system, msg],
                    "tools_ref": tools,
                    "content": None,
                    "raw_tool_calls": [{"id": "c1", "name": "click", "arguments": '{"target": 1}'}],
                }
            )
        events.append(
            {
                "t_wall": "2026-09-28T10:00:09Z",
                "t_mono": 9.0,
                "step": steps,
                "type": "trial_end",
                "status": status,
                "terminal_reason": "done",
                "answer": answer,
            }
        )
        for i, e in enumerate(events):
            e["seq"] = i
        d = self.evals / "error-analysis" / batch / "trials" / trial_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "trial.json").write_bytes(canon(header))
        (d / "events.jsonl").write_bytes(b"".join(canon(e) + b"\n" for e in events))
        return d


@pytest.fixture
def traces(tmp_path: Path) -> TraceDir:
    return TraceDir(tmp_path)


@pytest.fixture
def uploaded(uploader: TestClient, traces: TraceDir) -> Callable[..., str]:
    """Build a Trial on disk and upload it through the CLI; returns the trial_id."""
    from browser_agent.evals.annotate import cli

    def make(trial_id: str, **kw: Any) -> str:
        d = traces.trial(trial_id, **kw)
        rep = cli.upload(uploader, [d], log=lambda *_: None)
        assert trial_id in rep.created + rep.existing, rep
        return trial_id

    return make
