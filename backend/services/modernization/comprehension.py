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
from backend.services.modernization import rules as rules_module
from backend.services.modernization import store

logger = logging.getLogger(__name__)

# ~17k tokens. A 40-component map plus a framework and table list is a few KB; this is the
# ceiling at which "one cache write, N-1 reads" stops being the best economics in the
# product and starts being a tax on every call.
MAX_ADDENDUM_CHARS = 60_000

# The rules' share of that budget. Capped separately rather than left to the overall
# truncation, because the tail of this block is where the rules live and a codebase with a
# lot of them would otherwise push the deterministic evidence out of the prompt entirely —
# losing the part that was free and provable to keep the part that was neither.
MAX_RULES_CHARS = 16_000

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
    "auth": 8,
}

_SECTION_TITLES = {
    "auth": "How the source identifies a user and remembers them",
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
                 "config_key", "auth")


def digest_coverage(run_id: str) -> Dict[str, Dict[str, int]]:
    """Per evidence kind: how many the digest names, and how many exist. `{kind: {shown,total}}`.

    The digest caps each kind — routes at 40, tables at 60 — because it sits in the *cached*
    system block and every conversion call in the run pays for its size. That is the right
    trade. What was wrong is that it was silent: the block appends "… and N more" and no gate,
    no check and no reader has ever divided by it, so a codebase whose routes the digest could
    only partly describe rendered exactly like one it described completely.

    Only kinds that are actually capped are returned; a kind that fits entirely is not a
    coverage finding and would only add noise to the gate.
    """
    summary = evidence_module.summarize(store.list_evidence(run_id))
    capped: Dict[str, Dict[str, int]] = {}
    for kind in _DIGEST_KINDS:
        total = len(summary.get(kind) or [])
        limit = _TOP_N.get(kind, 20)
        if total > limit:
            capped[kind] = {"shown": limit, "total": total}
    return capped


def describe_coverage(capped: Dict[str, Dict[str, int]]) -> str:
    """One sentence per capped kind, for the gate that approves the conversion."""
    if not capped:
        return ""
    return "; ".join(
        f"{_SECTION_TITLES.get(kind, kind).lower()}: {counts['shown']} of {counts['total']}"
        for kind, counts in sorted(capped.items()))


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

    lines.extend(_portrait_section(run_id))
    lines.extend(_rules_section(run_id))

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


def _bullets(value: Any) -> List[str]:
    """Render a portrait field, whichever shape the schema gives it.

    Half of `SYNTHESIS_TOOL_SCHEMA`'s arrays are arrays of *objects* (`actors` is
    `{name, description}`, `cross_cutting_concerns` is `{concern, how_it_works}`) and half
    are arrays of strings. A renderer that handled only strings dropped exactly the fields
    the synthesis call exists to produce — `cross_cutting_concerns` is the pattern no single
    file shows, which is the whole reason for paying for a pass that reads every probe.

    Object keys are joined in the schema's declared order rather than sorted, so this stays
    byte-stable regardless of how the dict was rebuilt (D-06).
    """
    if value is None or isinstance(value, (str, int, float)):
        text = str(value or "").strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            # One pass in the dict's own order, so `subject — readings — why_it_matters`
            # reads the way the schema declares it rather than scalars-then-lists.
            parts = []
            for value_part in item.values():
                if isinstance(value_part, (str, int, float)):
                    text_part = str(value_part).strip()
                elif isinstance(value_part, list):
                    text_part = ", ".join(
                        str(x).strip() for x in value_part
                        if isinstance(x, (str, int, float)) and str(x).strip())
                else:
                    continue
                if text_part:
                    parts.append(text_part)
            text = " — ".join(parts)
        else:
            continue
        if text:
            out.append(f"- {text}")
    return out


def _capability_bullets(value: Any) -> List[str]:
    """What the system does, with the shaky ones marked as shaky.

    This block is read by every conversion call in the run, so a capability the readings only
    half-supported carried exactly the authority of one several probes saw plainly — and the
    converter has no way to tell them apart or to go and check. Marking it is cheap and it is
    the difference between a portrait and an assertion.

    Only `medium` and `low` are annotated: well-evidenced is the expected case, and a marker on
    every line would be noise in a block that is paid for on every call. Plain strings are
    still accepted, so a portrait stored before this existed renders unchanged.
    """
    out: List[str] = []
    for item in value or []:
        if isinstance(item, str):
            name, confidence = item.strip(), "high"
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            confidence = str(item.get("confidence") or "high").strip().lower()
        else:
            continue
        if not name:
            continue
        if confidence == "low":
            out.append(f"- {name} — *inferred; no reading pointed at it directly*")
        elif confidence == "medium":
            out.append(f"- {name} — *seen by one reading only*")
        else:
            out.append(f"- {name}")
    return out


def _portrait_section(run_id: str) -> List[str]:
    """What the synthesis pass concluded the system is for.

    Read from the unit's stored meta rather than re-derived, and rendered in a fixed field
    order — this lands in the cached system block, so a section whose order followed a dict's
    iteration order would change the prefix between two runs of the same codebase and cost
    full price on every call.
    """
    units = [u for u in store.list_units(run_id, kind="synthesis") if u["status"] == "converted"]
    meta = (units[0].get("meta") if units else None) or {}
    if not meta:
        return []
    out: List[str] = []
    for key, title in (
        ("system_purpose", "What the system is for"),
        ("primary_capabilities", "What it does"),
        ("actors", "Who uses it"),
        ("data_lifecycle", "How data moves through it"),
        ("cross_cutting_concerns", "Patterns that appear throughout"),
        ("conventions", "Conventions every converted file must honour"),
    ):
        rendered = (_capability_bullets(meta.get(key)) if key == "primary_capabilities"
                    else _bullets(meta.get(key)))
        if not rendered:
            # Emitted only when there is something under it. A heading with nothing beneath
            # it is worse than an absent one here: it is frozen into the cached system block
            # and re-read on every conversion call for the life of the run.
            continue
        out.append(f"### {title}")
        out.append("")
        out.extend(rendered)
        out.append("")
    # `contradictions` is deliberately absent: it is a caveat for the human reading the run,
    # and telling every conversion call that two readings disagreed invites the model to pick
    # one and act on it — silently, in whichever file happens to be in front of it.
    return out


def _rules_section(run_id: str) -> List[str]:
    """Confirmed business rules only. A proposed rule is a claim nobody has checked, and the
    whole point of verification is that it never reaches a prompt (D-04)."""
    confirmed = store.list_rules(run_id, status="confirmed")
    if not confirmed:
        return []
    block = rules_module.render_rules_block(confirmed, max_chars=MAX_RULES_CHARS)
    return [block, ""] if block else []


def build_and_freeze(run_id: str) -> str:
    """Render the digest and store it. Called once, at plan approval."""
    run = store.get_run(run_id) or {}
    digest = render_digest(run_id, run.get("stats") or {})
    store.update_run(run_id, system_addendum=digest)
    if digest:
        store.append_event(run_id, "digest_frozen",
                           f"{len(digest)} char(s) of comprehension pinned to the system prompt")
    return digest
