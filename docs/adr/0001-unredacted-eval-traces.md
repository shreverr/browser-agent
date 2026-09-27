# Eval traces are unredacted; eval trials are confined to fixture hosts

The action-evidence store redacts aggressively (typed text, control names and values, context text, sensitive URL parameters), but traces recorded by the eval runner are stored unredacted. Graders must check exactly those values ("typed the right address", "picked the right variant"), step evals are harvested from traces, and the raw tool-call arguments in model messages expose them anyway, so redacting traces would break grading without protecting anything. Safety comes from confinement instead: a task trial may only reach its fixture site's hosts, whose data is synthetic. The eval runner forces `allowed_domains` to those hosts and records any blocked navigation in the trace. The API key, provider headers and environment never enter a trace, and `.evals/` is gitignored. The evidence store keeps its own redaction unchanged.

## Consequences

- Fixture sites must never contain real personal data or credentials.
- Running task evals against the live web would violate this ADR's safety premise. Live-web runs stay manual smoke runs outside the eval runner until this decision is revisited.
- Once Graders depend on raw values, switching traces to redaction means rewriting those Graders, which is why this is recorded.
