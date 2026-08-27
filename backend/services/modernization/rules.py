"""Business rules a model proposed, and the check that decides which of them are real.

The comprehension pass exists because a legacy codebase states its business rules in exactly
one place — the code — and a conversion driven by routes and tables alone reproduces the
shape of the application without its behaviour. A discount that only applies above a
threshold, an approval only two roles may give, a status that can never go backwards: those
survive only if something states them before the first file is converted.

The problem with asking a model for them is that it is very good at producing plausible ones.
D-04 settles the principle for dependency edges — the LLM never produces evidence — and it
applies here with the volume turned up: an invented rule is indistinguishable from a real one
in the prompt of every conversion call that follows, and the converted code will implement it.

So a proposed rule is a **claim**, and this module is the part that turns some of them into
facts:

  * `bundle` decides, deterministically, which source files each probe reads. Same input,
    same probes, every time — otherwise two runs over the same codebase produce different
    rules and the whole pass is a coin flip with a citation.
  * `verify` checks each claim's quote against the file it cites, corrects the line number to
    where the quote actually is, and discards everything it cannot find. This is the same
    trick the code reviewer uses on finding evidence, and it reuses the same implementation —
    see the import below for why that matters.
  * `render_rules_block` turns the survivors into the markdown that goes into the frozen
    system addendum (D-51), which means it must be byte-identical given the same rules or a
    500-file run pays full price for it 500 times.

Nothing here calls the store or the model. The caller does both, which is what lets the whole
module be tested offline against a dict of fake sources.
"""
import logging
import posixpath
from typing import Any, Dict, List, Optional, Set, Tuple

# Imported under their private names on purpose. `finding_pipeline` already answers "is this
# quote really in that text, and which line is it on" for every code review the platform runs,
# and a second whitespace-normalising substring search is how two verifiers start disagreeing
# about what counts as a match — at which point a rule confirmed by one path is rejected by the
# other and neither is obviously wrong. They are private only because nothing outside that
# module needed them until now; they want promoting to `normalize` / `line_of_offset`.
from backend.services.finding_pipeline import line_of_offset as _line_of_offset
from backend.services.finding_pipeline import normalize as _normalize

logger = logging.getLogger(__name__)

# The closed vocabulary, shared with the probe tool schema so a category can never arrive that
# `render_rules_block` has no heading for. `other` is deliberately last and deliberately vague:
# a rule that does not fit is still a rule, and forcing it into a wrong category is worse than
# admitting the taxonomy is incomplete.
CATEGORIES: Tuple[str, ...] = (
    "validation",
    "calculation",
    "authorization",
    "workflow",
    "data_integrity",
    "presentation",
    "other",
)

_CATEGORY_TITLES = {
    "validation": "Validation — what the application refuses to accept",
    "calculation": "Calculation — how values are derived",
    "authorization": "Authorization — who may do what",
    "workflow": "Workflow — what may follow what",
    "data_integrity": "Data integrity — what must stay true of stored data",
    "presentation": "Presentation — what the user is shown, and when",
    "other": "Other behaviour that must be preserved",
}

# A quote shorter than this is not evidence. `return true` appears in every file in the
# codebase, so finding it proves nothing about the rule that cites it — and because the
# search is whitespace-normalised, a short quote matches even more loosely than it looks.
# `finding_pipeline` uses 8 for the same reason; 12 here because a rule is a much stronger
# claim than a finding and gets one shot to justify itself.
MIN_QUOTE_CHARS = 12

# Long statements are truncated rather than dropped: a rambling statement is still a real
# rule, but it must not be able to eat the addendum budget on its own.
MAX_STATEMENT_CHARS = 400

# Verification proves the citation, never the reading — the quote is genuinely in the file,
# but whether it means what the statement says is not something a substring search can
# answer. So neither value is 1.0. A quote that appears once pins one site in one file; a
# quote that appears three times does not say which of the three the rule is about.
CONFIDENCE_UNIQUE_QUOTE = 0.9
CONFIDENCE_REPEATED_QUOTE = 0.6

# Used only when a unit carries a line count but no source text — a bundle planned from an
# inventory still has to respect a byte cap. Being 20% out changes packing efficiency, never
# correctness, which is the same trade D-14 already made for chunking.
_AVG_LINE_CHARS = 40

REJECT_REASONS: Tuple[str, ...] = (
    "empty_statement",
    "empty_quote",
    "quote_too_short",
    "unknown_path",
    "quote_not_found",
    "duplicate_statement",
)


# ── Bundling source files into probes ──────────────────────────────────────

def bundle(units: List[Dict[str, Any]], max_chars: int,
           max_files: int = 12) -> List[List[Dict[str, Any]]]:
    """Split source units into the groups one probe call each will read.

    Pure and total: the same list in produces the same list of lists out, every time. That is
    not a nicety — a probe boundary decides which files are read together, which decides which
    rules are found, so a bundler that shuffled would make two runs over one codebase disagree
    about what the application does.

    Ordering is directory-major, dependency-proximity-minor. Directory first because
    co-located files in a legacy codebase are nearly always one feature; dependency proximity
    within it because an alphabetical slice puts `order_confirm.php` and `order_cancel.php` in
    different probes while separating neither from the unrelated file between them.

    Both caps are respected. A single unit larger than `max_chars` gets a probe to itself
    rather than being dropped — a dropped file contributes no rules, and nothing downstream
    could tell that from a file that genuinely had none.

    `units` are dicts as `store.list_units` returns them; `path` is the only required key,
    `depends_on` and `source_text`/`content` are used when present.
    """
    max_chars = max(_int(max_chars), 1)
    max_files = max(_int(max_files), 1)

    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_chars = 0

    # Packed cluster by cluster rather than file by file. Packing individually let a cap fall
    # in the middle of a group of files that reference each other, so a rule implemented
    # across two of them was invisible to both probes and neither knew the other existed —
    # and the boundary was decided by byte count, which is a cost question being asked to
    # answer a semantic one. A cluster that fits in an empty bundle is never split.
    for cluster in _clusters(units, max_chars, max_files):
        size = sum(_source_chars(u) for u in cluster)
        if current and (len(current) + len(cluster) > max_files
                        or current_chars + size > max_chars):
            groups.append(current)
            current, current_chars = [], 0
        for unit in cluster:
            unit_size = _source_chars(unit)
            # Only reachable for a cluster too big to fit anywhere, which `_clusters` has
            # already broken down — so this is the same file-by-file packing as before,
            # applied to the remainder rather than to everything.
            if current and (len(current) >= max_files
                            or current_chars + unit_size > max_chars):
                groups.append(current)
                current, current_chars = [], 0
            current.append(unit)
            current_chars += unit_size
    if current:
        groups.append(current)
    return groups


def _clusters(units: List[Dict[str, Any]], max_chars: int,
              max_files: int) -> List[List[Dict[str, Any]]]:
    """Files that reference each other, grouped, in a fixed order.

    Connected components of the *undirected* dependency graph — undirected because "worth
    reading together" is symmetric even though a dependency is not.

    Two properties keep this from making things worse:

    **A cluster too large to be one probe is not a cluster.** In a legacy codebase a single
    shared include (`header.php`, `db.php`) is reachable from nearly every file, so the naive
    component containing it is the whole repository — and packing that "cluster" would be
    path-order packing with the directory heuristic thrown away, which is strictly worse than
    what this replaces. An oversized component is therefore handed back in the existing
    directory-major order and packed exactly as it always was.

    **Order is total and derived only from paths.** Clusters sort by their lowest member,
    members by the existing breadth-first walk, so two runs over one codebase group
    identically — the property the whole module exists to preserve.
    """
    ordered = _probe_order(units)
    by_path = {_path_of(u): u for u in ordered}
    neighbours: Dict[str, Set[str]] = {path: set() for path in by_path}
    for path, unit in by_path.items():
        for dep in unit.get("depends_on") or []:
            dep = str(dep)
            if dep in by_path and dep != path:
                neighbours[path].add(dep)
                neighbours[dep].add(path)

    seen: Set[str] = set()
    components: List[List[str]] = []
    for seed in sorted(by_path):
        if seed in seen:
            continue
        seen.add(seed)
        queue, members = [seed], [seed]
        while queue:
            path = queue.pop(0)
            for neighbour in sorted(neighbours[path]):
                if neighbour not in seen:
                    seen.add(neighbour)
                    members.append(neighbour)
                    queue.append(neighbour)
        components.append(sorted(members))

    position = {_path_of(u): i for i, u in enumerate(ordered)}
    out: List[List[Dict[str, Any]]] = []
    for members in sorted(components, key=lambda m: m[0]):
        group = sorted((by_path[p] for p in members), key=lambda u: position[_path_of(u)])
        fits = (len(group) <= max_files
                and sum(_source_chars(u) for u in group) <= max_chars)
        if fits:
            out.append(group)
        else:
            # Degraded to the directory-major order it already had, one file per entry, so the
            # packer treats it exactly as it did before clusters existed.
            out.extend([unit] for unit in group)
    return out


def _probe_order(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every unit exactly once, directory by directory, related files adjacent."""
    by_dir: Dict[str, List[Dict[str, Any]]] = {}
    seen: Set[str] = set()
    for unit in units or []:
        path = _path_of(unit)
        # A path appearing twice would be read by two probes and produce the same rules
        # twice, which the statement-level dedup in `verify` would then have to clean up
        # after paying for both calls.
        if not path or path in seen:
            continue
        seen.add(path)
        by_dir.setdefault(posixpath.dirname(path), []).append(unit)

    ordered: List[Dict[str, Any]] = []
    for directory in sorted(by_dir):
        ordered.extend(_dependency_order(by_dir[directory]))
    return ordered


def _dependency_order(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One directory's files, with files that reference each other next to each other.

    A breadth-first walk of the undirected local dependency graph, seeded in path order, so
    the result depends only on the paths and the edges — never on the order the caller
    happened to hand them over. Edges to files outside this directory are ignored: following
    them would make a group's membership depend on the whole graph, and the point of grouping
    by directory is that it is legible to whoever reads the resulting portrait.
    """
    by_path = {_path_of(u): u for u in members}
    neighbours: Dict[str, Set[str]] = {path: set() for path in by_path}
    for path, unit in by_path.items():
        for dep in unit.get("depends_on") or []:
            dep = str(dep)
            if dep in by_path and dep != path:
                neighbours[path].add(dep)
                neighbours[dep].add(path)

    ordered: List[Dict[str, Any]] = []
    visited: Set[str] = set()
    for seed in sorted(by_path):
        if seed in visited:
            continue
        visited.add(seed)
        queue = [seed]
        while queue:
            path = queue.pop(0)
            ordered.append(by_path[path])
            for neighbour in sorted(neighbours[path]):
                if neighbour not in visited:
                    visited.add(neighbour)
                    queue.append(neighbour)
    return ordered


def _source_chars(unit: Dict[str, Any]) -> int:
    text = unit.get("source_text")
    if not isinstance(text, str) or not text:
        text = unit.get("content")
    if isinstance(text, str) and text:
        return len(text)
    return max(_int(unit.get("loc")), 1) * _AVG_LINE_CHARS


# ── Verification ───────────────────────────────────────────────────────────

def verify(run_id: str, proposed: List[Dict[str, Any]],
           sources: Dict[str, str]) -> Dict[str, Any]:
    """Decide which proposed rules are allowed near a prompt.

    `sources` maps path -> the source text the probe was shown (already redacted, since
    everything downstream of `repo_ingest` is). Every rule is checked against the file it
    names, and the line number it claimed is replaced by the line the quote is really on —
    the model's own number is a guess and is wrong often enough that trusting it would put
    wrong citations in front of a human reviewing the run.

    Returns `{"confirmed": [...], "rejected": [...], "counts": {...}}`. Both lists are shaped
    for `store.insert_rules`; inserting them is the caller's job, so this function stays
    testable with a dict of fake files and no database.
    """
    confirmed_candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for rule in proposed or []:
        shaped, reason = _verify_one(rule, sources or {})
        if reason:
            rejected.append(shaped)
        else:
            confirmed_candidates.append(shaped)

    confirmed, duplicates = _dedupe(confirmed_candidates)
    rejected.extend(duplicates)

    by_reason: Dict[str, int] = {}
    for rule in rejected:
        reason = rule["reject_reason"]
        by_reason[reason] = by_reason.get(reason, 0) + 1
    by_category: Dict[str, int] = {}
    for rule in confirmed:
        by_category[rule["category"]] = by_category.get(rule["category"], 0) + 1

    counts = {
        "proposed": len(proposed or []),
        "confirmed": len(confirmed),
        "rejected": len(rejected),
        "by_reason": dict(sorted(by_reason.items())),
        "by_category": dict(sorted(by_category.items())),
    }
    # Counts only. A statement is a customer's business logic and a quote is their source
    # code; neither belongs in a log line that ships at info level.
    logger.info(
        "Comprehension rules for run %s: %d proposed, %d confirmed, %d rejected (%s)",
        run_id or "?", counts["proposed"], counts["confirmed"], counts["rejected"],
        ", ".join(f"{k}={v}" for k, v in counts["by_reason"].items()) or "none",
    )
    return {"confirmed": confirmed, "rejected": rejected, "counts": counts}


def _verify_one(rule: Dict[str, Any], sources: Dict[str, str]) -> Tuple[Dict[str, Any], str]:
    """One rule, checked. Returns the store-shaped dict and the rejection reason ("" = kept)."""
    statement = _text(rule.get("statement"))
    if not statement:
        return _shape(rule, "rejected", reject_reason="empty_statement"), "empty_statement"

    quote = _text(rule.get("quote"))
    if not quote:
        return _shape(rule, "rejected", reject_reason="empty_quote"), "empty_quote"

    needle = _normalize(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return _shape(rule, "rejected", reject_reason="quote_too_short"), "quote_too_short"

    path, source = _resolve_source(rule.get("path"), sources)
    if source is None:
        return _shape(rule, "rejected", reject_reason="unknown_path"), "unknown_path"

    haystack = _normalize(source)
    offset = haystack.find(needle)
    if offset == -1:
        return _shape(rule, "rejected", reject_reason="quote_not_found",
                      path=path), "quote_not_found"

    reported = _int(rule.get("line"))
    # `_line_of_offset` returns None only for an offset past the last line, which the `find`
    # above makes impossible for anything but an empty needle — but falling back to the
    # claimed line is still better than emitting a citation of line 0.
    actual = _line_of_offset(source.splitlines(), offset)
    line = actual if actual is not None else reported

    confirmed = _shape(
        rule, "confirmed", path=path, line=line,
        confidence=(CONFIDENCE_UNIQUE_QUOTE if haystack.count(needle) == 1
                    else CONFIDENCE_REPEATED_QUOTE),
    )
    if reported and reported != line:
        # Not read by the store — kept so the disagreement rate between what the model claims
        # and where the quote actually is can be measured rather than assumed.
        confirmed["reported_line"] = reported
    return confirmed, ""


def _dedupe(rules: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collapse rules whose statements say the same thing, keeping the best-evidenced one.

    Runs *after* verification, never before: a confirmed rule must not lose its slot to an
    identical statement whose quote turned out not to exist. The winner is the longest quote,
    because the longer the quote the more of the file was actually checked; ties keep the
    earlier one so the result does not depend on iteration order.
    """
    best: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    losers: List[Dict[str, Any]] = []

    for rule in rules:
        key = _normalize(rule["statement"]).lower()
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = rule
            order.append(key)
            continue
        if len(rule["quote"]) > len(incumbent["quote"]):
            best[key] = rule
            losers.append(_as_duplicate(incumbent))
        else:
            losers.append(_as_duplicate(rule))
    return [best[key] for key in order], losers


def _as_duplicate(rule: Dict[str, Any]) -> Dict[str, Any]:
    return dict(rule, status="rejected", reject_reason="duplicate_statement", confidence=0.0)


def _resolve_source(path: Any, sources: Dict[str, str]) -> Tuple[str, Optional[str]]:
    """The file this rule cites, or (path, None) when the run has no such file.

    Exact match first, then one spelling normalisation — `./x/y.php` and `x\\y.php` are the
    same file as `x/y.php`, and rejecting a genuine rule over a leading dot-slash would lose
    real behaviour for no safety gained. Deliberately not a fuzzy or basename match: attaching
    a rule to a file it was not read from is worse than dropping it, because the citation then
    points a human at code that does not contain the behaviour.
    """
    raw = _text(path)
    if raw in sources:
        return raw, sources[raw]
    normalized = raw.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in sources:
        return normalized, sources[normalized]
    return raw, None


def _shape(rule: Dict[str, Any], status: str, *, path: Optional[str] = None,
           line: Optional[int] = None, confidence: float = 0.0,
           reject_reason: str = "") -> Dict[str, Any]:
    """Exactly the keys `store.insert_rules` reads, whatever the model sent."""
    return {
        "statement": _text(rule.get("statement")),
        "category": _category_of(rule),
        "path": _text(rule.get("path")) if path is None else path,
        "line": _int(rule.get("line")) if line is None else int(line),
        "quote": _text(rule.get("quote")),
        "status": status,
        "reject_reason": reject_reason,
        "confidence": float(confidence),
        "unit_id": rule.get("unit_id") or "",
    }


def _category_of(rule: Dict[str, Any]) -> str:
    category = _text(rule.get("category")).lower()
    return category if category in CATEGORIES else "other"


# ── Rendering into the frozen system addendum ──────────────────────────────

def render_rules_block(rules: List[Dict[str, Any]], max_chars: int = 12_000) -> str:
    """The confirmed rules as markdown, for the run's system addendum.

    Deterministic given the same rules: categories in `CATEGORIES` order, rules within a
    category by path then line then statement. That is not cosmetic — this string is
    concatenated into the *system* block, which is where the prompt-cache breakpoint sits
    (D-06), so a block that reordered itself between calls would cost full price on every
    file in the run with nothing in the logs but `cache_read_tokens=0`.

    Quotes are not rendered. The statement plus `path:line` is what a converter needs, and
    the quotes are source text that would multiply the size of a block every call pays for.
    """
    kept = [r for r in rules or []
            if _text(r.get("statement")) and (r.get("status") or "confirmed") == "confirmed"]
    if not kept:
        return ""

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for rule in kept:
        grouped.setdefault(_category_of(rule), []).append(rule)

    out = list(_RULES_HEADER)
    used = sum(len(line) + 1 for line in out)
    written = 0
    truncated = False

    # Reserved up front, because the notice is appended *after* the loop stops: without this
    # a block that fills the budget exactly then exceeds `max_chars` by the length of the
    # sentence saying it was truncated. The caller's budget is a cache-cost ceiling (D-06),
    # so overshooting it is the one thing this function must not do.
    _NOTICE_RESERVE = 160
    max_chars = int(max_chars) - _NOTICE_RESERVE
    if used >= max_chars:
        # Not even the preamble fits, so nothing here can be honest: a header telling the
        # model these are the verified business rules, with no rules beneath it, is worse
        # than no section at all.
        return ""

    for category in CATEGORIES:
        entries = sorted(
            grouped.get(category) or [],
            key=lambda r: (_text(r.get("path")), _int(r.get("line")), _text(r.get("statement"))),
        )
        if not entries:
            continue
        pending = [f"### {_CATEGORY_TITLES[category]}", ""]
        for rule in entries:
            line = _rule_line(rule)
            addition = sum(len(p) + 1 for p in pending) + len(line) + 1
            if used + addition > max_chars:
                truncated = True
                break
            out.extend(pending)
            used += sum(len(p) + 1 for p in pending)
            pending = []
            out.append(line)
            used += len(line) + 1
            written += 1
        if truncated:
            break
        out.append("")
        used += 1

    if truncated:
        # Announced rather than silent: a converter that is shown 40 of 90 rules must not be
        # left believing it was shown all of them.
        out.extend(["", f"_({len(kept) - written} further rule(s) omitted for length — the "
                        f"full set is on the run's rules endpoint.)_"])
    return "\n".join(out).rstrip()


def _rule_line(rule: Dict[str, Any]) -> str:
    path = _text(rule.get("path"))
    citation = f"`{path}:{_int(rule.get('line'))}`" if path else "`(uncited)`"
    # Normalised so a statement containing a newline cannot break out of the markdown list.
    statement = _normalize(_text(rule.get("statement")))[:MAX_STATEMENT_CHARS]
    return f"- {citation} — {statement}"


_RULES_HEADER = (
    "## Business rules recovered from this codebase",
    "",
    "Each of these was read out of the source and then **verified**: the quote it was based "
    "on was found in the file named, and the line number is where that quote actually is — "
    "not where it was claimed to be. Anything that could not be found in the source was "
    "discarded and is not listed here.",
    "",
    "These are behaviours the converted code must still have. They are not a description of "
    "the target design, and implementing one of them differently because the target stack "
    "makes a different choice natural is a regression, not a simplification. Where a rule "
    "contradicts the file in front of you, follow the file and say so in `manual_followup` — "
    "one file may legitimately be the exception, and resolving that silently is how a rule "
    "gets lost twice.",
    "",
)


# ── Small shared coercions ─────────────────────────────────────────────────

def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _path_of(unit: Dict[str, Any]) -> str:
    return _text(unit.get("path")).replace("\\", "/")
