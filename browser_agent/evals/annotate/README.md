# Annotation server (error analysis, #32)

A small collaborative server for open-coding Trials. Each annotator writes one note per Trial
(verdict, the first `seq` that went wrong, free text). Notes are then grouped into failure
modes, and the result is exported as `taxonomy.yaml`, the snapshot that gets committed to the repo.
It ingests the ea-1 Trace format defined in
[`browser_agent/evals/analysis/TRACE_FORMAT.md`](../analysis/TRACE_FORMAT.md).

```text
capture machine ── upload CLI (bearer token) ──▶ FastAPI on Render ──▶ Neon Postgres (notes, modes, index)
                                                        │           └─▶ Cloudflare R2 (Trace files, blobs)
browsers ── GitHub OAuth session + SSE ─────────────────┘
```

- **Render**: one Docker web service (`render.yaml` at the repo root, `Dockerfile` here). It runs
  Alembic migrations on start, then uvicorn. `/healthz` is the health check.
- **Neon Postgres**: every table (SQLAlchemy 2 + psycopg 3, Alembic migrations in `migrations/`).
- **Cloudflare R2** over the S3 API (boto3). Keys are `blobs/sha256/<hex>` (content-addressed,
  immutable) and `trials/<trial_id>/{trial.json,events.jsonl}`. The `blob` table indexes what is in
  R2, so an upload can check its refs without a HEAD request per blob.
- **Live updates**: `GET /api/events` is a Server-Sent Events stream. It uses an **in-process
  broadcaster** (`broadcast.py`), so the service must run as **exactly one instance**
  (`numInstances: 1`). Scaling out would need Postgres LISTEN/NOTIFY instead. Events are published
  only after the transaction commits, and a stalled viewer is told to resync instead of being
  sent a partial stream.

## Data model

| table | what |
|---|---|
| `trial` | `id` (= trial_id), batch_id, task_id, status, terminal_reason, `header` (the whole trial.json, JSONB), n_events, trial/events sha256, uploaded_at. **Immutable**: re-uploading identical bytes is a no-op, different bytes get 409. |
| `blob` | sha256, bytes, media_type: the index of R2's `blobs/`. |
| `note` | trial_id, author (GitHub login), verdict (`fail`\|`pass_but_bad`\|`ok`, so passing Trials can be reviewed too), first_bad_seq, text. **Unique (trial_id, author)**. |
| `mode` | uuid id, name (unique among active modes), definition, gradability (`code`\|`judge`\|`unknown`), merged_into, created_by. Renames and merges keep ids. |
| `assignment` | note_id, mode_id, proposed_by (`human:<login>`\|`claude`), state (`proposed`\|`accepted`\|`rejected`), decided_by, rationale. Unique (note_id, mode_id). |
| `example` | mode_id → trial_id (+ seq, caption), pinned_by. |
| `audit` | append-only: who, action, entity, before/after JSON for every mutation. `GET /api/audit`. |

Rules the service layer enforces:

- Only signed-in humans create, rename, redefine or merge modes. The token (Claude) can only add
  `proposed` assignments, through `POST /api/proposals`.
- Assigning **your own** note to a mode is accepted at once. Assigning someone else's note is a
  proposal that only that note's author decides. Claude's proposals can be decided by any human.
- **Merge** (`POST /api/modes/<id>/merge {"into": <id>}`) is one transaction. It re-points every
  assignment and example, dedupes pairs that already existed (keeping the stronger state:
  accepted > proposed > rejected), flattens earlier merges into the new target, and sets
  `merged_into`. Trial counts follow the merge, so `A (2) + B (2)` sharing one Trial gives 3.
- A mode's **count** is the number of distinct Trials with an accepted assignment, following
  `merged_into`. Proposals never count.

## API

All JSON, under `/api`. Auth: **T** = `Authorization: Bearer $ANNOTATE_UPLOAD_TOKEN`,
**H** = signed-in allowlisted human (writes also need the header `X-Requested-With: annotate`),
**R** = either.

| | endpoint | |
|---|---|---|
| T | `POST /blobs/missing` `{"sha256": [...]}` → `{"missing": [...]}` | upload contract, step 1 |
| T | `PUT /blobs/<sha256>` raw bytes + `Content-Type` | step 2; the hash is verified (400 on mismatch) |
| T | `PUT /trials/<trial_id>` multipart `trial.json`, `events.jsonl` | step 3; 422 if any ref is missing or the Trace is not ea-1 / not finished, 201 created, 200 identical, 409 different |
| R | `GET /trials?batch=&status=&cell=&has_note=&author=&mode=&coded=uncoded\|coded&mine=noted\|unnoted` | list with note and mode summaries |
| R | `GET /trials/<id>` | header + parsed events |
| R | `GET /trials/<id>/notes`, `GET /notes?trial_id=&author=&mode=&uncoded=true` | notes with their assignments |
| R | `GET /blobs/<sha256>` | streamed from R2 (or a 302 to a 2-minute presigned URL with `ANNOTATE_BLOB_REDIRECT=true`) |
| R | `GET /modes?include_merged=true`, `GET /assignments?mode=&state=&note_id=`, `GET /audit` | |
| R | `GET /taxonomy/export` | `taxonomy.yaml` |
| H | `GET /events` | SSE change stream |
| H | `PUT /trials/<id>/note` `{verdict, first_bad_seq, text}` | upsert **my** note |
| H | `POST /modes`, `PATCH /modes/<id>`, `POST /modes/<id>/merge`, `POST /modes/<id>/examples`, `DELETE /examples/<id>` | taxonomy |
| H | `POST /assignments {note_id, mode_id}`, `POST /assignments/<id>/accept\|reject`, `DELETE /assignments/<id>` | |
| T | `POST /proposals {"proposals": [...]}` | Claude's bulk proposals, all-or-nothing validation |

`taxonomy.yaml` holds each active mode's id, name, definition, gradability, count, pinned
examples, member Trial ids and the names merged into it. It also lists `uncoded` notes (no
accepted mode) and `agreement`. For every pair of authors who noted the same Trials, agreement
gives `shared_trials`, `coded_by_both`, `same_modes` (identical mode sets), `any_shared_mode`,
`mean_jaccard` of their mode sets, `verdict_agreement` and `first_bad_seq_agreement`.

## CLI (`browser-agent-annotate`)

Client commands read `ANNOTATE_URL` and `ANNOTATE_UPLOAD_TOKEN` from the environment or `.env`.

```sh
uv sync --extra annotate
# upload every finished Trial under a batch (or a single Trial directory); running Trials are skipped.
# Blobs are found in the .evals/blobs above each Trial (override with --blobs). Only missing blobs are sent.
uv run browser-agent-annotate upload .evals/error-analysis/2026-09-28-a
uv run browser-agent-annotate export --out docs/error-analysis/taxonomy.yaml
uv run browser-agent-annotate propose proposals.json
uv run browser-agent-annotate serve     # migrate + uvicorn on $PORT (what the container runs)
uv run browser-agent-annotate migrate
```

`proposals.json` is a list (or `{"proposals": [...]}`). Each item names a note, by `note_id` or by
`trial_id` + `author`, and an **existing** mode, by `mode_id` or exact `mode` name, plus an
optional `rationale`:

```json
[{"trial_id": "ev-charger-barstow.t1.a1", "author": "alice", "mode": "Clicked sponsored result",
  "rationale": "note says it opened the first (ad) result"}]
```

Claude reads the notes with the same token (`GET /api/notes?uncoded=true`, `GET /api/modes`).
Pairs that already exist are skipped, never overwritten. An unknown note or mode rejects the whole
file, and nothing is written.

## Viewer

`/` serves a static, no-build page (`static/`) ported from the prototype's layout D:

- **Trials** (left): task id, cell tags, status / terminal_reason, steps, cost, answer snippet,
  your note, the count of others' notes, and mode chips. Filters: uncoded (default), not noted
  by me, coded, all; status; cell; author; mode. `j` / `k` move between Trials.
- **Trace** (middle): the header line, the task, the Simulated user rules, then `#seq type`
  lines grouped by step. `user_exchange` (with `unscripted_fallback` in red), `intervention`,
  `compaction`, `blocked_navigation` and `vision_fallback` are highlighted. Observations and model
  calls start collapsed, and their blobs (rendered page text, the messages the model saw, tool
  schemas) are fetched only when you expand them. Clicking a `#seq` sets `first_bad_seq` on your note.
- **Note** (right): your verdict, first bad step and text, autosaved as you type. Below it,
  others' notes (read-only) and mode chips. Proposals are dashed, with ✓ / ✗.
- **Taxonomy** (right tab): modes with counts. Definition and gradability are edited inline.
  You can rename a mode, merge it into another (with a confirmation), pin the current Trial as an
  example, and drill down into the notes in a mode. Every open viewer updates live over SSE.

Dark mode follows the OS, and the header button forces light or dark. Below 1000 px the page
becomes one column with tabs.

## Local development

With Docker (Postgres stands in for Neon, MinIO for R2, and a dev login replaces GitHub):

```sh
docker compose -f browser_agent/evals/annotate/docker-compose.yml up --build
open http://localhost:8000               # signed in as "dev" (ANNOTATE_DEV_LOGIN)
ANNOTATE_URL=http://localhost:8000 ANNOTATE_UPLOAD_TOKEN=local-dev-upload-token-not-for-prod \
  uv run browser-agent-annotate upload .evals/error-analysis/<batch>
```

MinIO's console is at http://localhost:9001 (`minioadmin` / `minioadmin`). `ANNOTATE_DEV_LOGIN`
is refused unless `ANNOTATE_BASE_URL` is a localhost URL.

Tests use SQLite and an in-memory S3 fake, or a disposable Postgres if
`ANNOTATE_TEST_DATABASE_URL` is set. **They drop every table in that database.**

```sh
uv run pytest tests/annotate -q
ANNOTATE_TEST_DATABASE_URL=postgresql://annotate:annotate@localhost:5432/annotate uv run pytest tests/annotate -q
```

## Environment variables

| variable | required | meaning |
|---|---|---|
| `ANNOTATE_BASE_URL` | yes | public origin, e.g. `https://browser-agent-annotate.onrender.com`. The OAuth callback is `<base>/auth/callback`. |
| `ANNOTATE_DATABASE_URL` | yes | Neon connection string (`postgresql://…?sslmode=require`; `DATABASE_URL` also works). `postgresql://` is rewritten to the psycopg 3 driver. |
| `ANNOTATE_S3_ENDPOINT_URL` | for R2 | `https://<account id>.r2.cloudflarestorage.com` |
| `ANNOTATE_S3_BUCKET` | yes | R2 bucket name |
| `ANNOTATE_S3_ACCESS_KEY_ID`, `ANNOTATE_S3_SECRET_ACCESS_KEY` | yes | R2 API token (Object Read & Write, this bucket only) |
| `ANNOTATE_S3_REGION` | no | `auto` (R2), default `auto` |
| `ANNOTATE_GITHUB_CLIENT_ID`, `ANNOTATE_GITHUB_CLIENT_SECRET` | yes (unless dev login) | GitHub OAuth app |
| `ANNOTATE_ALLOWED_GITHUB` | yes | comma-separated GitHub logins, case-insensitive. Checked on every request. |
| `ANNOTATE_SESSION_SECRET` | yes | ≥ 24 chars. Signs the session cookie. Rotating it signs everyone out. |
| `ANNOTATE_UPLOAD_TOKEN` | yes | ≥ 24 chars. Bearer token for upload, proposals and CLI reads. |
| `ANNOTATE_COOKIE_SECURE` | no | default `true` (cookie only over HTTPS). Set `false` for plain-http local dev. |
| `ANNOTATE_BLOB_REDIRECT` | no | default `false` (stream blobs through the server). `true` sends a 302 to a presigned R2 URL. |
| `ANNOTATE_MAX_UPLOAD_BYTES` | no | per blob / per Trace file, default 64 MiB |
| `ANNOTATE_DEV_LOGIN` | no | local only: `/auth/login` signs in as this login without GitHub |
| `PORT` | no | set by Render; `serve` listens on it (default 8000) |
| `ANNOTATE_URL` | CLI only | server origin for `upload` / `export` / `propose` |

## One-time setup (a human has to do these)

1. **Neon**: create a project and a database (e.g. `annotate`). Copy the **pooled** connection
   string (`…-pooler…?sslmode=require`) → `ANNOTATE_DATABASE_URL`. The server creates the tables
   on its first start.
2. **Cloudflare R2**: create a private bucket (e.g. `browser-agent-traces`) with no public access
   and no custom domain. Create an R2 API token with **Object Read & Write** scoped to that
   bucket. Copy the Access Key ID and Secret → `ANNOTATE_S3_ACCESS_KEY_ID` /
   `ANNOTATE_S3_SECRET_ACCESS_KEY`. Copy the S3 endpoint `https://<account id>.r2.cloudflarestorage.com`
   → `ANNOTATE_S3_ENDPOINT_URL`, and the bucket name → `ANNOTATE_S3_BUCKET`.
3. **Render**: New → Blueprint → this repo. That creates `browser-agent-annotate` from
   `render.yaml`. Fill in the prompted values. `ANNOTATE_SESSION_SECRET` and
   `ANNOTATE_UPLOAD_TOKEN` are generated for you. Note the service URL →
   `ANNOTATE_BASE_URL`.
4. **GitHub OAuth app** (Settings → Developer settings → OAuth Apps → New): set the Homepage URL
   to `ANNOTATE_BASE_URL` and the **Authorization callback URL** to `<ANNOTATE_BASE_URL>/auth/callback`.
   Copy the Client ID and a new client secret → `ANNOTATE_GITHUB_CLIENT_ID` / `ANNOTATE_GITHUB_CLIENT_SECRET`.
5. Set `ANNOTATE_ALLOWED_GITHUB` to the annotators' logins, then deploy manually
   (`autoDeploy` is off). Check that `https://…/healthz` returns `{"ok": true}` and that signing
   in works.
6. On the capture machine, put `ANNOTATE_URL` and `ANNOTATE_UPLOAD_TOKEN` (copied from the
   Render dashboard) in `.env`, then run `browser-agent-annotate upload …`.
7. When coding is done, `browser-agent-annotate export --out <path>/taxonomy.yaml` and commit it.

## Security notes

- **Traces are unredacted.** They hold the text and structure of live-web pages exactly as the
  agent saw them, plus the raw tool-call arguments. They were captured on a clean, logged-out
  browser profile with empty or synthetic memory, so they should contain no personal accounts or
  cookies. Still, treat them as untrusted third-party content and never make them public. This
  differs from ADR 0001 (fixture-only Traces): error analysis runs on the live web by design (#32).
- **Nothing is public.** Every data route needs an allowlisted GitHub session or the upload token.
  Only `/`, `/static/*` (code, no data), `/healthz` and the OAuth routes are open. The R2 bucket
  stays private, and presigned URLs, when enabled, last 2 minutes.
- **The allowlist is the only way in.** A login not on `ANNOTATE_ALLOWED_GITHUB` is refused at the
  callback. Removing a login locks that person out on their next request. The OAuth app asks for
  no scopes, and its access token is used once to read the login, then dropped.
- **The upload token stays server-side** (Render env plus the capture machine's `.env`). It never
  reaches a browser. It can upload, propose and read, but it cannot touch notes or modes.
  Rotate it in Render if it leaks.
- **Trace text is hostile input.** The viewer escapes every string, links only `http(s)` URLs
  (`rel="noopener noreferrer"`), and runs under a CSP with no inline script or style.
  Blobs are served with `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox`,
  and only as `text/plain`, JSON or images. Anything else (such as `text/html`) is served as
  `application/octet-stream`.
- Cookie writes also require `X-Requested-With: annotate`, and the session cookie is
  `HttpOnly; SameSite=Lax; Secure`. Every mutation is written to `audit`.
