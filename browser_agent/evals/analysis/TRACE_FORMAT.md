# Error-analysis Trace format (`trace_schema_version: "ea-1"`)

Throwaway subset of the Trial record decided in shreverr/browser-agent#28, used only
by error analysis (#32). The capture patch writes it, and the annotation server
ingests and renders it. The v0 eval runner will define the full format.

## Layout on disk

```text
.evals/error-analysis/<batch_id>/trials/<trial_id>/trial.json
.evals/error-analysis/<batch_id>/trials/<trial_id>/events.jsonl
.evals/blobs/sha256/<first 2 hex>/<sha256 hex>        # raw bytes, uncompressed
```

- `trial_id` = `<task_id>.t<k>.a<attempt>`, e.g. `ev-charger-barstow.t1.a1`.
- A blob's name is the sha256 of its bytes. Blobs are immutable and shared across Trials.
- JSON blobs are UTF-8, canonicalized (`sort_keys`, `separators=(",", ":")`, `ensure_ascii=False`).
- A "ref" is always `{"sha256": "<hex>", "bytes": <int>, "media_type": "<type>"}`.

## `trial.json`

It is written when the Trial starts with `status: "running"` and rewritten when it ends.

```json
{
  "trace_schema_version": "ea-1",
  "trial_id": "ev-charger-barstow.t1.a1",
  "batch_id": "2026-09-28-a",
  "attempt": 1,
  "kind": "task",
  "task": {
    "id": "ev-charger-barstow",
    "text": "…task text as given to the agent…",
    "start_url": "https://www.plugshare.com/",
    "sites": ["plugshare.com"],
    "cell": {"task_type": "research", "horizon": "medium", "interaction": ["filters", "map"]},
    "notes": "free text from the task list"
  },
  "variant": {
    "code_revision": "<git sha>[+dirty]",
    "model": "qwen/qwen3.7-flash",
    "checker_model": "qwen/qwen3.7-flash",
    "max_steps": 50,
    "check_every": 10,
    "vision": false,
    "system_prompt_ref": {"sha256": "…", "bytes": 1234, "media_type": "text/plain"},
    "tools_ref": {"sha256": "…", "bytes": 5678, "media_type": "application/json"}
  },
  "initial_memory": {"name": "Asha Rao"},
  "simulated_user": [{"id": "dates", "match": "date|when", "reply": "20–22 October 2026"}],
  "environment": {"os": "darwin", "chrome_version": "…", "headless": false},
  "started_at": "2026-09-28T10:00:00.000Z",
  "ended_at": "2026-09-28T10:03:12.400Z",
  "status": "completed",
  "terminal_reason": "done",
  "answer": "…final done answer…",
  "automation_outcome": "succeeded",
  "unscripted_fallback": false,
  "memory_writes": [{"op": "remember", "key": "k", "value": "v"}],
  "infra": null,
  "totals": {
    "steps": 14,
    "model_calls": {"agent": 14, "supervisor": 1},
    "prompt_tokens": 81234,
    "completion_tokens": 2311,
    "cost_usd": 0.0123,
    "wall_s": 192.4,
    "peak_context_pct": 31.5
  }
}
```

- `status`: `running` | `completed` | `infra_error` | `aborted`.
- `terminal_reason` (only when completed): `done` | `no_tool_call` | `max_steps` | `no_response` | `timeout`.
- `infra` (only when infra_error): `{"class": "captcha|chrome|provider|cdp|other", "message": "…", "traceback": "…"}`.
- `automation_outcome`: `succeeded` | `failed`, where failed means a human intervention was counted.

## `events.jsonl`

One JSON object per line, appended as things happen. Every event has this envelope:

```json
{"seq": 0, "t_wall": "2026-09-28T10:00:00.123Z", "t_mono": 0.123, "step": 0, "type": "…", ...}
```

`seq` starts at 0 and is dense. `step` is the agent loop step: 0 before the first model call, then 1, 2, and so on.
`t_mono` counts seconds since the Trial started.

| `type` | fields |
|---|---|
| `observation` | `observation_id`, `url`, `title`, `rendered_ref` (text/plain: `render_state` output), `structured_ref` (application/json: pre-render observation), `n_controls`, `truncated` |
| `model_call` | `role` (`agent`\|`supervisor`), `message_refs` (ordered refs, one per message as sent; application/json), `tools_ref` (or null), `request_sha256`, `requested_model`, `served_model`, `provider`, `usage` `{prompt_tokens, completion_tokens, reasoning_tokens, cached_tokens}`, `cost_usd`, `context_length`, `context_pct`, `latency_ms`, `finish_reason`, `retries`, `content` (assistant text or null), `raw_tool_calls` (`[{id, name, arguments}]`, arguments as the raw string), `error` (string or null) |
| `tool_call` | `call_id`, `name`, `arguments` (parsed object, or `{"_raw": "…"}` if unparsable) |
| `action_result` | `call_id`, `name`, `status`, `error_code`, `message`, `rendered` (text the model got back, including the page text after `PAGE_MARK`; ≤ 64 KiB inline, else `rendered_ref`), `structured_ref` (application/json ActionResult without the observation), `observation_id` (of the resulting observation, or null) |
| `compaction` | `replaced` `[{"index": 3, "before_ref": …, "after_ref": …}]` |
| `user_exchange` | `kind` (`ask_user`\|`confirmation`\|`verification`), `question`, `reply`, `source` (`scripted:<rule id>`\|`unscripted_fallback`\|`policy`), `counts_as_intervention` |
| `intervention` | `source` (`supervisor`\|`repeat_guard`\|`stuck_warning`), `trigger` (`cadence`\|`loop`\|`stall`\|`repeat`), `injected_text_ref` (or null), `action_refused` (bool) |
| `memory_write` | `op` (`remember`\|`forget`), `key`, `value` |
| `vision_fallback` | `error` |
| `blocked_navigation` | `url`, `message` |
| `trial_end` | `status`, `terminal_reason`, `answer` |

Notes:
- A `tool_call` that the agent answers without the browser (`done`, `ask_user`, `remember`, `forget`, a declined confirmation, a repeat-guard refusal) still gets an `action_result` with `status: "handled"` and the text the model saw in `rendered`.
- The system prompt is message 0 of every `model_call`, so its ref repeats. The blob store dedupes it.
- Secrets never enter a Trace: no API key, request headers, or environment dump.

## Upload contract (capture machine → annotation server)

1. `POST /api/blobs/missing` `{"sha256": ["…", …]}`, answered by `{"missing": ["…"]}`
2. `PUT /api/blobs/<sha256>` with the raw bytes and a `Content-Type` header. The server verifies the hash.
3. `PUT /api/trials/<trial_id>` as multipart with `trial.json` and `events.jsonl`. Every ref inside them must already exist. The call is idempotent when the bytes are identical, and returns 409 when a different Trace already has that id.

Auth: `Authorization: Bearer <ANNOTATE_UPLOAD_TOKEN>`.
