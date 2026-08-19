"""Ask the model what the deterministic extraction missed — then check every word of it.

**Why this exists.** `sanity.py` catches the failures where two extractors the pipeline
already has disagree with each other. It cannot catch the failure where *both* are silent:
a framework nobody wrote a recogniser for, a routing convention expressed in a way no regex
anticipates, a data layer three indirections deep. There is always a next framework, and the
deterministic layer will never be complete — so the general-purpose net is a model that reads
the source and the extracted facts side by side and says what is missing.

**The obvious objection, and the answer.** D-04 settles the principle: the LLM never produces
evidence, because an invented fact is indistinguishable from a real one and corrupts every
prompt after it. That principle is kept intact here and it is not a compromise, because a
claim is not adopted on the model's authority — it is adopted on the authority of a **quote
found in the customer's own file**. Every claim carries `path`, `line` and a verbatim `quote`;
`verify` searches the real source for that quote, corrects the line to where it actually is,
and discards everything it cannot find. What survives is a citation into the source, which is
the same standard `evidence.py` meets. What does not survive costs nothing but tokens.

This is the same trick `rules.py` plays on business rules, and it deliberately reuses the same
two functions to do the matching (see the import below): a second whitespace-normalising
substring search is how two verifiers start disagreeing about what counts as a match.

**What it may and may not add.** Only rows in `evidence.EVIDENCE_KINDS`, only for files the
run actually ingested, and never a dependency edge — the graph stays deterministic (D-04), so
a confirmed claim can tell you a route exists but can never reorder the conversion. Confirmed
rows are written with `source="model_audit"` and a confidence below 1, so every consumer and
every screen can tell them apart from something a parser proved.

**Where it runs.** At the end of the comprehension stage: after the probes, before the system
portrait is frozen and before the plan gate — so a fact it recovers reaches the digest every
conversion call reads, and reaches the human who approves the conversion spend. It cannot run
before the *discovery* gate, because that gate's whole promise is that nothing has been spent.

Nothing here calls the store or the model. The caller does both, which is what lets this be
tested offline against a dict of fake sources.
"""
import logging
import os
from typing import Any, Dict, Iterable, List, Sequence, Tuple

# The same matcher `rules.verify` uses, imported under the same private names and for the
# reason its comment gives: two independent "is this quote really in that file" searches
# eventually disagree, and then a claim confirmed by one path is rejected by the other.
from backend.services.finding_pipeline import line_of_offset as _line_of_offset
from backend.services.finding_pipeline import normalize as _normalize
from backend.services.modernization import evidence as evidence_module

logger = logging.getLogger(__name__)

# A quote shorter than this matches half the file. `rules.py` uses the same floor for the same
# reason: `);` is present in every PHP file ever written and proves nothing about anything.
MIN_QUOTE_CHARS = 12

# What one audit call is allowed to claim. Not a cost control — the cost is one call either
# way — but a signal control: a model asked for "everything wrong" produces a long tail of
# guesses, and a list nobody finishes reading is a list nobody reads.
MAX_CLAIMS = 40

# How much source the audit is shown. It reads *samples* rather than the whole codebase on
# purpose: the question is "what kind of thing did the extraction miss", and a shape that
# appears nowhere in fifty files is not the shape this net is for.
MAX_SAMPLE_FILES = 40
MAX_SAMPLE_CHARS = 90_000
MAX_FILE_CHARS = 6_000
MAX_TREE_ENTRIES = 400

# Kinds a confirmed claim may become. Exactly `evidence.EVIDENCE_KINDS` — a claim of any other
# kind is discarded rather than coerced, because a consumer switching on kind would silently
# never see an invented one and the miss would look like an absence.
ADOPTABLE_KINDS = frozenset(evidence_module.EVIDENCE_KINDS)

# Confidence stamped on an adopted row. Below every parser row (1.0 or 0.75), so a screen
# ordering by confidence puts proved facts above recovered ones, and above nothing — because
# a quote was found in the file, which is more than an unverified claim has.
ADOPTED_CONFIDENCE = 0.5


def _sample(files: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Which files the audit reads, and how much of each.

    Spread across directories rather than taken in path order: a repository whose first forty
    files are all in `src/Models` teaches the audit nothing about how it routes. One file per
    directory in the first pass, then round-robin, so a wide tree is covered before a deep one
    is covered twice.
    """
    by_dir: Dict[str, List[Dict[str, Any]]] = {}
    for entry in files:
        path = entry.get("path") or ""
        by_dir.setdefault(os.path.dirname(path), []).append(entry)
    for group in by_dir.values():
        group.sort(key=lambda e: len(e.get("content") or ""), reverse=True)

    picked: List[Dict[str, str]] = []
    total = 0
    depth = 0
    while len(picked) < MAX_SAMPLE_FILES and total < MAX_SAMPLE_CHARS:
        took_any = False
        for directory in sorted(by_dir):
            group = by_dir[directory]
            if depth >= len(group):
                continue
            took_any = True
            entry = group[depth]
            body = (entry.get("content") or "")[:MAX_FILE_CHARS]
            if not body.strip():
                continue
            picked.append({"path": entry.get("path") or "", "content": body})
            total += len(body)
            if len(picked) >= MAX_SAMPLE_FILES or total >= MAX_SAMPLE_CHARS:
                break
        if not took_any:
            break
        depth += 1
    return picked


def _tree(files: Sequence[Dict[str, Any]]) -> str:
    paths = sorted((f.get("path") or "") for f in files if f.get("path"))
    shown = paths[:MAX_TREE_ENTRIES]
    lines = list(shown)
    if len(paths) > len(shown):
        lines.append(f"… and {len(paths) - len(shown)} more file(s)")
    return "\n".join(lines)


def _facts_block(signals: Dict[str, Any], evidence_rows: Sequence[Dict[str, Any]]) -> str:
    """What the extraction concluded, in the shape the audit has to argue against.

    Counts *and* values, because "4 tables" invites agreement and "attendance, departments,
    employees, users" invites someone to notice `payroll` is missing. Absences are stated
    explicitly for the same reason the gate states them: a key that is simply not printed
    reads as a key nobody was looking for.
    """
    s = signals or {}
    by_kind: Dict[str, List[str]] = {}
    for row in evidence_rows or ():
        values = by_kind.setdefault(row.get("kind") or "other", [])
        value = str(row.get("value") or "")
        if value and value not in values and len(values) < 30:
            values.append(value)

    lines = [
        f"Files ingested: {s.get('files_ingested', 0)}. "
        f"Read by a real parser: {(s.get('read_by') or {}).get('parser', 0)}; "
        f"by a pattern fallback: {(s.get('read_by') or {}).get('heuristic', 0)}; "
        f"not read at all: {(s.get('read_by') or {}).get('default', 0)}.",
        f"Dependency edges found: {s.get('edges', 0)}.",
        "",
        "Extracted facts, by kind — an empty list means the extraction found none:",
    ]
    for kind in evidence_module.EVIDENCE_KINDS:
        values = by_kind.get(kind) or []
        lines.append(f"  {kind}: {', '.join(values) if values else '(none)'}")
    lines += [
        "",
        f"Data-access APIs recognised: {', '.join(s.get('db_apis') or []) or '(none)'}",
        f"Query call sites found: {s.get('db_sites', 0)}",
        f"Files carrying a hazard that blocks conversion: {len(s.get('blocked') or [])}",
    ]
    return "\n".join(lines)


def render_request(files: Sequence[Dict[str, Any]], evidence_rows: Sequence[Dict[str, Any]],
                   signals: Dict[str, Any]) -> str:
    """The user message: the tree, a sample of the source, and what was concluded from it.

    Belongs in the *user* message and never the cached system block (D-06): every line of it
    is specific to this run.
    """
    samples = _sample(files)
    parts = [
        "## The file tree",
        "",
        _tree(files),
        "",
        "## What the deterministic extraction concluded",
        "",
        _facts_block(signals, evidence_rows),
        "",
        "## Source samples",
        "",
        f"{len(samples)} of {len(files)} file(s), truncated. Judge only what you can see here "
        f"— a claim about a file that is not below cannot be checked and will be discarded.",
    ]
    for entry in samples:
        parts += ["", f"### {entry['path']}", "```", entry["content"], "```"]
    return "\n".join(parts)


def _resolve_path(claimed: Any, sources: Dict[str, str]) -> Tuple[str, Any]:
    """Match a claimed path to a real one, tolerating a leading `./` or a missing prefix.

    A model that reads `src/Core/Router.php` in a heading and cites `Core/Router.php` is
    right about the file and careless about the prefix; rejecting that costs a real finding
    for a formatting reason. An ambiguous suffix is *not* resolved — two files with the same
    basename would otherwise let a claim land on whichever one sorted first.
    """
    path = str(claimed or "").replace("\\", "/").lstrip("./").strip()
    if not path:
        return "", None
    if path in sources:
        return path, sources[path]
    matches = [p for p in sources if p.endswith("/" + path)]
    if len(matches) == 1:
        return matches[0], sources[matches[0]]
    return path, None


def verify(claims: Sequence[Dict[str, Any]], sources: Dict[str, str],
           known: Iterable[Tuple[str, str, str]] = ()) -> Dict[str, Any]:
    """Decide which claims are allowed to become evidence. Pure — no store, no model.

    `known` is `(kind, path, value)` for everything the extraction already found, so a claim
    that repeats a fact we have is dropped as `already_known` rather than duplicating a row.
    That distinction is worth keeping: a model that mostly re-reports what we already had is
    telling us the extraction is good, and a rejection count that lumps it in with invented
    quotes would hide that.
    """
    already = {(str(k), str(p), str(v)) for k, p, v in known or ()}
    confirmed: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen: set = set()

    for claim in (claims or [])[:MAX_CLAIMS]:
        kind = str(claim.get("kind") or "").strip().lower()
        value = str(claim.get("value") or "").strip()
        quote = str(claim.get("quote") or "")
        detail = str(claim.get("detail") or "").strip()

        def _reject(reason: str, path: str = "") -> None:
            rejected.append({"kind": kind, "value": value, "path": path or
                             str(claim.get("path") or ""), "detail": detail,
                             "reject_reason": reason})

        if kind not in ADOPTABLE_KINDS:
            _reject("unknown_kind")
            continue
        if not value:
            _reject("empty_value")
            continue
        needle = _normalize(quote)
        if len(needle) < MIN_QUOTE_CHARS:
            _reject("quote_too_short")
            continue

        path, source = _resolve_path(claim.get("path"), sources)
        if source is None:
            _reject("unknown_path", path)
            continue
        offset = _normalize(source).find(needle)
        if offset == -1:
            _reject("quote_not_found", path)
            continue
        if (kind, path, value) in already:
            _reject("already_known", path)
            continue
        if (kind, path, value) in seen:
            _reject("duplicate", path)
            continue
        seen.add((kind, path, value))

        line = _line_of_offset(source.splitlines(), offset)
        confirmed.append({
            "path": path,
            "kind": kind,
            "value": value[:500],
            "line": line or 0,
            # The quote as it appears in the file, not as the model retyped it — the snippet
            # is shown to a human as their own source, so it has to be their own source.
            "snippet": _snippet_at(source, line or 0),
            "confidence": ADOPTED_CONFIDENCE,
            "source": "model_audit",
            "detail": detail[:500],
        })

    by_reason: Dict[str, int] = {}
    for row in rejected:
        by_reason[row["reject_reason"]] = by_reason.get(row["reject_reason"], 0) + 1
    counts = {
        "claimed": len(claims or []),
        "confirmed": len(confirmed),
        "rejected": len(rejected),
        "by_reason": dict(sorted(by_reason.items())),
        "by_kind": dict(sorted(
            {k: sum(1 for c in confirmed if c["kind"] == k) for k in
             {c["kind"] for c in confirmed}}.items())),
    }
    # Counts only: a value is a fact about a customer's application and a quote is their
    # source, and neither belongs in a log line that ships at info level.
    logger.info(
        "Extraction audit: %d claimed, %d confirmed, %d rejected (%s)",
        counts["claimed"], counts["confirmed"], counts["rejected"],
        ", ".join(f"{k}={v}" for k, v in counts["by_reason"].items()) or "none")
    return {"confirmed": confirmed, "rejected": rejected, "counts": counts}


def _snippet_at(source: str, line: int) -> str:
    lines = source.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()[:evidence_module.MAX_SNIPPET]
    return ""


def describe(counts: Dict[str, Any]) -> str:
    """The event body. Says what was kept *and* what was thrown away, because a net that
    reports only its catches cannot be judged."""
    if not counts.get("claimed"):
        return "the audit found nothing the extraction had missed"
    kept = ", ".join(f"{n} {k}" for k, n in (counts.get("by_kind") or {}).items())
    dropped = ", ".join(f"{n} {r}" for r, n in (counts.get("by_reason") or {}).items())
    return (f"{counts['claimed']} claim(s), {counts['confirmed']} confirmed against the "
            f"source{f' ({kept})' if kept else ''}, {counts['rejected']} discarded"
            f"{f' ({dropped})' if dropped else ''}")
