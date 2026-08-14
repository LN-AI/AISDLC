"""What this application is, rendered once and frozen for the run.

The converter is shown one file at a time and told, correctly, that it will never see the
whole codebase. That is what keeps a 500-file run affordable — but it also means every call
re-guesses the same context: what the app does, what it talks to, which conventions the
target has to honour. The digest answers that once.

**Where it goes, and why that matters more than what is in it.** The digest is concatenated
into the *system* block, which is where the prompt-cache breakpoint sits (D-06). So it must
be byte-identical across every call in the run, or a 500-file conversion pays full price for
it 500 times with nothing in the logs but `cache_read_tokens=0`. Two consequences:

  * it is rendered **once**, at plan approval, and stored verbatim in
    `modernization_runs.system_addendum`. It is never re-derived per unit from a dict that
    `json.loads` rebuilt in a different order — that is the trap `charter.render` documents.
  * it is **capped**. Above `MAX_ADDENDUM_CHARS` the tail is dropped rather than allowed to
    grow, because a digest that grows with the codebase reintroduces the per-call cost the
    signature-only dependency context was designed to avoid.

Phase 1a renders this purely from deterministic evidence. Phase 1b adds probe/synthesis
calls that fill the same slot — which is cheap precisely because the slot already exists.
"""
import logging
from typing import Any, Dict, List

from backend.services.modernization import evidence as evidence_module
from backend.services.modernization import store

logger = logging.getLogger(__name__)

# ~17k tokens. A 40-component map plus a framework and table list is a few KB; this is the
# ceiling at which "one cache write, N-1 reads" stops being the best economics in the
# product and starts being a tax on every call.
MAX_ADDENDUM_CHARS = 60_000

# How many distinct values of each kind reach the prompt. Ordered by frequency, so the cut
# is always the long tail.
_TOP_N = {
    "framework": 12,
    "datastore": 8,
    "table": 60,
    "integration": 20,
    "entrypoint": 40,
    "route": 40,
    "config_key": 30,
}

_SECTION_TITLES = {
    "framework": "Frameworks and libraries detected in the source",
    "datastore": "Persistence APIs the source uses",
    "table": "Database tables named in SQL literals",
    "integration": "External hosts the source contacts",
    "entrypoint": "Entry points — reachable from outside, or included by nothing",
    "route": "Routes declared in server configuration",
    "config_key": "Configuration keys created by redaction (no value was kept)",
}

# Deliberately not in the digest: `hazard` and `dispatch`. Those are per-file facts that
# belong in the message for the file that has them, not in a system block every other unit
# also pays for — and a global list of hazards invites a converter to "handle" one it was
# never shown.
_DIGEST_KINDS = ("framework", "datastore", "entrypoint", "route", "table", "integration",
                 "config_key")


def render_digest(run_id: str, stats: Dict[str, Any] = None) -> str:
    """The frozen system-prompt block. Deterministic given the same evidence."""
    rows = store.list_evidence(run_id)
    if not rows:
        return ""
    summary = evidence_module.summarize(rows)
    stats = stats or {}

    lines: List[str] = [
        "## What this codebase is",
        "",
        "Observed by parsing the source before any conversion began — not inferred, and not "
        "your summary to revise. It is here so that every file in this run is converted "
        "against the same understanding of the application rather than against whatever can "
        "be guessed from the file in front of you.",
        "",
    ]

    languages = (stats.get("languages") or {})
    if languages:
        ranked = sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))
        lines.append("- **Languages**: "
                     + ", ".join(f"{name} ({count} file(s))" for name, count in ranked[:8]))
    if stats.get("units"):
        lines.append(f"- **Size**: {stats['units']} source file(s), "
                     f"{stats.get('edges', 0)} dependency edge(s), "
                     f"{stats.get('cycles', 0)} circular group(s)")
    if languages or stats.get("units"):
        lines.append("")

    for kind in _DIGEST_KINDS:
        values = summary.get(kind) or []
        if not values:
            continue
        limit = _TOP_N.get(kind, 20)
        shown = values[:limit]
        lines.append(f"### {_SECTION_TITLES[kind]}")
        lines.append("")
        lines.append(", ".join(f"`{v}`" for v in shown)
                     + (f" … and {len(values) - limit} more" if len(values) > limit else ""))
        lines.append("")

    lines.append(
        "If something above contradicts the file you are given, the file wins — this is a "
        "portrait of the whole, and one file may legitimately be the exception. Say so in "
        "`manual_followup` rather than resolving the contradiction silently."
    )

    text = "\n".join(lines)
    if len(text) > MAX_ADDENDUM_CHARS:
        text = text[:MAX_ADDENDUM_CHARS].rsplit("\n", 1)[0] + (
            "\n\n_(truncated — the full evidence set is on the run's evidence endpoint)_")
    return text


def build_and_freeze(run_id: str) -> str:
    """Render the digest and store it. Called once, at plan approval."""
    run = store.get_run(run_id) or {}
    digest = render_digest(run_id, run.get("stats") or {})
    store.update_run(run_id, system_addendum=digest)
    if digest:
        store.append_event(run_id, "digest_frozen",
                           f"{len(digest)} char(s) of comprehension pinned to the system prompt")
    return digest
