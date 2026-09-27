"""`browser-agent-annotate`: upload Traces, export the taxonomy, submit Claude's proposals, serve.

    browser-agent-annotate upload .evals/error-analysis/2026-09-28-a
    browser-agent-annotate export --out taxonomy.yaml
    browser-agent-annotate propose proposals.json
    browser-agent-annotate serve            # migrate, then run the server (Render does this)

Client commands read ANNOTATE_URL and ANNOTATE_UPLOAD_TOKEN from the environment (or .env).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import traceio


class HttpClient(Protocol):
    """The subset of httpx.Client the CLI uses (FastAPI's TestClient also fits)."""

    def post(self, url: str, **kwargs: Any) -> Any: ...
    def put(self, url: str, **kwargs: Any) -> Any: ...
    def get(self, url: str, **kwargs: Any) -> Any: ...


class CliError(RuntimeError):
    pass


def _check(resp: Any, what: str) -> Any:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text[:500]
        raise CliError(f"{what}: HTTP {resp.status_code}: {detail}")
    return resp


@dataclass
class UploadReport:
    blobs_referenced: int = 0
    blobs_uploaded: int = 0
    created: list[str] = field(default_factory=lambda: list[str]())
    existing: list[str] = field(default_factory=lambda: list[str]())
    conflicts: list[str] = field(default_factory=lambda: list[str]())
    skipped: list[str] = field(default_factory=lambda: list[str]())


def upload(
    client: HttpClient, roots: Sequence[Path], blobs: Path | None = None, log: Any = print
) -> UploadReport:
    """Upload every finished Trial under `roots`, sending each missing blob exactly once."""
    report = UploadReport()
    trials: list[tuple[Path, bytes, bytes, traceio.ParsedTrace]] = []
    for root in roots:
        dirs = traceio.find_trial_dirs(root)
        if not dirs:
            raise CliError(f"no Trials (trial.json + events.jsonl) under {root}")
        for d in dirs:
            tb, eb = (d / "trial.json").read_bytes(), (d / "events.jsonl").read_bytes()
            try:
                trials.append((d, tb, eb, traceio.parse_trace(tb, eb)))
            except traceio.TraceError as e:
                report.skipped.append(f"{d.name}: {e}")
                log(f"skip {d}: {e}")

    # sha -> (ref, path on disk)
    wanted: dict[str, tuple[traceio.Ref, Path]] = {}
    for d, _, _, trace in trials:
        root = blobs or traceio.find_blob_root(d)
        for sha, ref in trace.refs().items():
            if sha in wanted:
                continue
            if root is None:
                raise CliError(f"cannot find the blob store for {d}; pass --blobs .evals/blobs")
            wanted[sha] = (ref, traceio.blob_path(root, sha))
    report.blobs_referenced = len(wanted)

    shas = list(wanted)
    missing: list[str] = []
    for i in range(0, len(shas), 1000):
        r = _check(
            client.post("/api/blobs/missing", json={"sha256": shas[i : i + 1000]}), "blobs/missing"
        )
        missing.extend(r.json()["missing"])
    for sha in missing:
        ref, path = wanted[sha]
        if not path.is_file():
            raise CliError(f"blob {sha} is referenced but not on disk at {path}")
        data = path.read_bytes()
        if traceio.sha256_hex(data) != sha:
            raise CliError(f"blob on disk is corrupt (hash mismatch): {path}")
        _check(
            client.put(f"/api/blobs/{sha}", content=data, headers={"Content-Type": ref.media_type}),
            f"blob {sha}",
        )
        report.blobs_uploaded += 1
    log(f"blobs: {report.blobs_referenced} referenced, {report.blobs_uploaded} uploaded")

    for _, tb, eb, trace in trials:
        tid = trace.trial_id
        r = client.put(
            f"/api/trials/{tid}",
            files={
                "trial.json": ("trial.json", tb, "application/json"),
                "events.jsonl": ("events.jsonl", eb, "application/x-ndjson"),
            },
        )
        if r.status_code == 409:
            report.conflicts.append(tid)
            log(f"CONFLICT {tid}: a different Trace is already stored under this id")
            continue
        _check(r, f"trial {tid}")
        (report.created if r.json()["status"] == "created" else report.existing).append(tid)
        log(f"{r.json()['status']:>8} {tid}")
    return report


def export(client: HttpClient, out: Path) -> None:
    r = _check(client.get("/api/taxonomy/export"), "export")
    out.write_text(r.text, encoding="utf-8")


def propose(client: HttpClient, path: Path) -> dict[str, Any]:
    """`path` holds a JSON list (or {"proposals": [...]}) of
    {"note_id" | ("trial_id", "author"), "mode_id" | "mode", "rationale"}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data["proposals"] if isinstance(data, dict) else data
    r = _check(client.post("/api/proposals", json={"proposals": items}), "proposals")
    return r.json()


def _client() -> Any:
    import httpx

    url = os.environ.get("ANNOTATE_URL", "").rstrip("/")
    token = os.environ.get("ANNOTATE_UPLOAD_TOKEN", "")
    if not url or not token:
        raise CliError("set ANNOTATE_URL and ANNOTATE_UPLOAD_TOKEN")
    return httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(60, connect=15),
    )


def serve(host: str, port: int) -> None:
    import uvicorn

    from .config import Settings
    from .db import migrate

    settings = Settings.from_env()
    migrate(settings.database_url)
    uvicorn.run(
        "browser_agent.evals.annotate.app:create_app",
        factory=True,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=5,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass
    p = argparse.ArgumentParser(
        prog="browser-agent-annotate", description="Annotation server client and launcher."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload", help="upload finished Trials and their blobs")
    up.add_argument("trials_dir", nargs="+", type=Path)
    up.add_argument(
        "--blobs", type=Path, help="blob store root (default: the .evals/blobs above each Trial)"
    )
    ex = sub.add_parser("export", help="download taxonomy.yaml")
    ex.add_argument("--out", type=Path, default=Path("taxonomy.yaml"))
    pr = sub.add_parser("propose", help="submit Claude's proposed assignments (bulk)")
    pr.add_argument("file", type=Path)
    sv = sub.add_parser("serve", help="run migrations, then the server")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    sub.add_parser("migrate", help="run database migrations only")
    args = p.parse_args(argv)

    try:
        if args.cmd == "serve":
            serve(args.host, args.port)
        elif args.cmd == "migrate":
            from .config import Settings
            from .db import migrate

            migrate(Settings.from_env().database_url)
        elif args.cmd == "upload":
            with _client() as c:
                rep = upload(c, args.trials_dir, args.blobs)
            print(
                f"{len(rep.created)} created, {len(rep.existing)} already there, "
                f"{len(rep.conflicts)} conflicts, {len(rep.skipped)} skipped"
            )
            return 1 if rep.conflicts else 0
        elif args.cmd == "export":
            with _client() as c:
                export(c, args.out)
            print(f"wrote {args.out}")
        elif args.cmd == "propose":
            with _client() as c:
                res = propose(c, args.file)
            print(
                f"{res['created']} proposals created, {len(res['skipped'])} skipped (already there)"
            )
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
