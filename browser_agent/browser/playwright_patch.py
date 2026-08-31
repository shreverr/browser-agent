"""Crash-proof the Playwright Firefox page-error dispatch.

Playwright's bundled Node driver runs `pageError.location.url` when a page
throws an uncaught JS error. Some sites (e.g. Swiggy Instamart) throw errors
whose `location` is undefined — the driver then crashes the whole Node process,
killing the agent mid-task. We rewrite that dispatch in place: omit `location`
when absent (else it fails the driver's string-type validation) and wrap the
whole thing so no malformed page error can ever take the driver down.

Patching a vendored file is ugly, but the alternative is losing every task that
lands on such a site. Idempotent and best-effort: on any failure the original
behaviour is left alone.
"""

from __future__ import annotations

from pathlib import Path

_ORIG = (
    '        this.addObjectListener(BrowserContext.Events.PageError, (pageError, page) => {\n'
    '          this._dispatchEvent("pageError", {\n'
    "            error: serializeError(pageError.error),\n"
    "            page: PageDispatcher.from(this, page),\n"
    "            location: {\n"
    "              url: pageError.location.url,\n"
    "              line: pageError.location.lineNumber,\n"
    "              column: pageError.location.columnNumber\n"
    "            }\n"
    "          });\n"
    "        });"
)
_FIXED = (
    '        this.addObjectListener(BrowserContext.Events.PageError, (pageError, page) => {\n'
    "          try {\n"
    '            this._dispatchEvent("pageError", {\n'
    "              error: serializeError(pageError.error),\n"
    "              page: PageDispatcher.from(this, page),\n"
    "              location: pageError.location ? {\n"
    "                url: pageError.location.url,\n"
    "                line: pageError.location.lineNumber,\n"
    "                column: pageError.location.columnNumber\n"
    "              } : undefined\n"
    "            });\n"
    "          } catch (e) {}\n"
    "        });"
)


def apply() -> None:
    """Patch the bundled driver if needed. Call before the driver spawns."""
    try:
        import playwright

        bundle = (
            Path(playwright.__file__).resolve().parent
            / "driver" / "package" / "lib" / "coreBundle.js"
        )
        text = bundle.read_text(encoding="utf-8")
        if "location: pageError.location ?" in text:
            return  # already patched
        # Handle a fresh install (original) or the earlier null-safe intermediate.
        intermediate = _ORIG.replace("pageError.location.", "pageError.location?.")
        for src in (_ORIG, intermediate):
            if src in text:
                bundle.write_text(text.replace(src, _FIXED), encoding="utf-8")
                return
    except Exception:
        pass
