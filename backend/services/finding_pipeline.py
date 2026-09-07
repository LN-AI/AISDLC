"""Turn a raw findings list into one a reviewer can trust.

Three deterministic passes, in order. All of them are plain Python — no second model call,
no embeddings, no vector store — so they are free, repeatable, and testable offline.

1. **Verify.** A finding must quote the code it is about. If that quote is not actually in
   the artifact, the finding is discarded as hallucinated. If the quote is present but the
   reported line number disagrees, the line is corrected from where the quote really is.
   This is the cheapest hallucination filter available, and it turns "state exact line
   numbers" from a hope in the prompt into something enforced.

2. **Merge.** The same issue seen from two chunks (or reported twice in one) collapses into
   one finding carrying `occurrences` and `locations`, rather than appearing N times.

3. **Sort.** A stable, total order, so the same input always produces the same output list.
   That determinism is what makes a regression suite possible at all.
"""
import difflib
import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

# Two titles this similar, for the same control in the same file, are the same issue.
# Deliberately high: merging two genuinely different problems is worse than listing a
# near-duplicate, because a merged-away finding is invisible.
TITLE_SIMILARITY_THRESHOLD = 0.85

# Paths whose findings are noise in a security review. Generated, vendored and test code
# is either not shipped or not attacker-reachable, and flagging a fake key in a fixture is
# the fastest way to teach a team to ignore the tool.
_SUPPRESSED_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|fixtures?|mocks?|node_modules|vendor|third_party|"
    r"dist|build|migrations|\.venv|venv)(/|$)"
    r"|\.min\.(js|css)$|\.lock$|\.snap$|(^|/)[^/]*\.(test|spec)\.[^/]+$",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """Collapse whitespace so an evidence quote matches regardless of indentation or how
    the model re-wrapped it."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def is_suppressed_path(path: str) -> bool:
    return bool(path) and bool(_SUPPRESSED_PATH_RE.search(path))


def line_of_offset(haystack_lines: List[str], normalized_offset: int) -> Optional[int]:
    """Map an offset in the whitespace-normalized text back to a 1-based line number.

    The normalized document is the non-empty normalized lines joined by *one* space, so that
    is what this has to reconstruct. Counting `len(normalize(line)) + 1` for every line
    instead is wrong on any file with blank lines: a blank line contributes nothing to
    `normalize(whole_text)` — its newlines are absorbed into the surrounding `\\s+` run — but
    the `+ 1` still advanced the accumulator, so the reported line drifted *earlier* by
    roughly the number of blank lines above the match. On ordinary source that is a citation
    pointing at the line before the one that matters, which is exactly the failure the
    caller uses this to avoid.
    """
    running = 0
    seen_content = False
    for i, line in enumerate(haystack_lines, start=1):
        piece = normalize(line)
        if not piece:
            continue
        if seen_content:
            running += 1        # the single space joining this line to the previous one
        seen_content = True
        running += len(piece)
        if running > normalized_offset:
            return i
    return None


# Public since modernization's business-rule verifier needs exactly this pair: a rule citing
# a line it cannot be found on is the same defect as a review finding citing one, and a
# second whitespace-normalizing substring search would drift from this one the first time
# either was fixed. The private names stay as aliases — they are used throughout this module
# and in at least one other caller, and renaming those is churn with no reader benefit.
_normalize = normalize
_line_of_offset = line_of_offset



# A unified diff carries a one-character marker on every line: `+` added, `-` removed, ` `
# unchanged context. A model asked to "quote the offending code verbatim" quotes the *code*,
# without those markers — which is the natural reading and what a human would do.
_DIFF_HEAD_RE = re.compile(r"^(diff --git |--- a/|\+\+\+ b/|@@ )", re.MULTILINE)


def looks_like_diff(text: str) -> bool:
    """Cheap structural check, on the head only. Mirrors `code_chunker.is_unified_diff`;
    duplicated rather than imported so this module stays dependency-free for its callers."""
    head = (text or "")[:4000]
    return bool(_DIFF_HEAD_RE.search(head)) or ("--- a/" in head and "+++ b/" in head)


def diff_views(text: str) -> Tuple[str, str]:
    """A diff as two searchable views, both excluding what the change removed.

    Returns `(with_markers, without_markers)`. Every finding's evidence is checked against
    both, and the raw diff is **not** searched at all, which fixes two separate defects.

    **Multi-line quotes were being discarded.** Measured on a real pull request on
    2026-09-02: of 13 findings on a 3-file diff, **10 were dropped** as unfounded and none of
    them were invented. The model had quoted two or three lines of code without the `+`
    prefixes — the natural reading of "quote the offending code verbatim" — and the
    normalized diff has a `+` between every pair of lines, so the needle
    `api_key = "..." dsn = "..."` could never be a substring of `+ api_key = "..." + dsn =
    "..."`. Single-line quotes survived by luck, being substrings of their own prefixed line.
    So every multi-line quote on every PR review was thrown away, in the flagship use case,
    and the discard count offered as a hallucination rate was mostly measuring prefix
    stripping. `without_markers` is what fixes it.

    **A quote of a deleted line used to verify.** Searching the raw diff found `-` lines,
    so a finding could report a vulnerability that the change *removes* and be marked
    verified — the reviewer contradicting the fix in front of it. Both views drop `-` lines,
    so such a quote now fails, which is correct: the code is not there any more.

    Neither view admits anything that was not literally in the diff, so this is a more
    accurate check rather than a looser one. Context (` `) lines are kept, because the prompt
    permits reporting a pre-existing problem on one as long as it is flagged
    `is_pre_existing`.
    """
    with_markers, without_markers = [], []
    for line in (text or "").splitlines():
        if line.startswith(("+++", "---", "@@", "diff --git", "index ", "\\ No newline")):
            continue
        if not line:
            with_markers.append("")
            without_markers.append("")
            continue
        marker = line[0]
        if marker == "-":
            continue
        with_markers.append(line)
        without_markers.append(line[1:] if marker in "+ " else line)
    return "\n".join(with_markers), "\n".join(without_markers)


def verify_evidence(
    findings: List[Dict[str, Any]],
    artifact: str,
    correct_lines: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split findings into (kept, discarded) by whether their evidence is really there.

    Always verify against the *whole original artifact*, not against the chunk the finding
    came from: a chunk's text is a substring of the artifact, so presence still holds, and
    the recovered position is then an absolute one rather than an offset into a chunk.

    `correct_lines` must be False for a unified diff. There, the model's `line_start` comes
    from the `@@` header and refers to a line in the *file*, whereas a position recovered
    from the diff text refers to a line in the *diff* — "correcting" one to the other would
    replace a right answer with a wrong one.

    A finding with no evidence at all is kept but marked `evidence_verified: False` — the
    schema asks for evidence, but dropping every finding that omits it would silently
    delete real issues the moment the model got lazy about one field.
    """
    lines = artifact.splitlines()
    normalized = _normalize(artifact)
    # A diff is searched through `diff_views` instead of raw: two views, both without the
    # lines the change removed. See that function for the two defects this fixes.
    is_diff = looks_like_diff(artifact)
    if is_diff:
        marked, unmarked = diff_views(artifact)
        haystacks = [_normalize(marked), _normalize(unmarked)]
    else:
        haystacks = [normalized]
    kept, discarded = [], []

    for f in findings or []:
        evidence = (f.get("evidence") or "").strip()
        if not evidence:
            f["evidence_verified"] = False
            kept.append(f)
            continue

        needle = _normalize(evidence)
        if len(needle) < 8:
            # Too short to be distinctive — "return" appears everywhere. Not proof of
            # anything, so treat it as unverified rather than as a match.
            f["evidence_verified"] = False
            kept.append(f)
            continue

        idx = next((h.find(needle) for h in haystacks if h.find(needle) != -1), -1)
        if idx == -1:
            f["discard_reason"] = (
                "evidence quote does not appear in the change under review"
                if is_diff else
                "evidence quote does not appear in the reviewed artifact"
            )
            discarded.append(f)
            continue

        f["evidence_verified"] = True
        if is_diff:
            f["evidence_scope"] = "diff"
        # A position recovered from a diff view is an offset into that view, not into the
        # diff, so no line is derived from it. `correct_lines` is False for diffs anyway —
        # the model's number comes from the `@@` header and refers to the file.
        if correct_lines and not is_diff:
            actual = _line_of_offset(lines, idx)
            if actual is not None:
                reported = f.get("line_start")
                if reported != actual:
                    f["reported_line_start"] = reported
                    f["line_start"] = actual
        kept.append(f)

    return kept, discarded


def fingerprint(finding: Dict[str, Any]) -> str:
    """Stable identity for a finding across runs.

    Deliberately excludes the line number: the same issue moving down a file when someone
    adds an import above it is still the same issue, and a fingerprint that changed there
    would make every baseline useless on the first unrelated edit.
    """
    parts = [
        str(finding.get("control_id", "")).upper(),
        str(finding.get("file_path", "")).lstrip("./").lower(),
        _normalize(str(finding.get("title", ""))).lower(),
        _normalize(str(finding.get("evidence", "")))[:200].lower(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _location(f: Dict[str, Any]) -> Dict[str, Any]:
    return {"file": f.get("file_path", ""), "line": f.get("line_start")}


def _absorb(target: Dict[str, Any], other: Dict[str, Any]) -> None:
    """Fold `other` into `target`, keeping the more serious of the two."""
    target["occurrences"] = target.get("occurrences", 1) + other.get("occurrences", 1)
    locs = target.setdefault("locations", [_location(target)])
    other_loc = _location(other)
    if other_loc not in locs:
        locs.append(other_loc)
    try:
        if int(other.get("severity", 0)) > int(target.get("severity", 0)):
            target["severity"] = other["severity"]
            target["risk_level"] = other.get("risk_level", target.get("risk_level"))
    except (TypeError, ValueError):
        pass
    if other.get("is_blocking"):
        target["is_blocking"] = True
    if other.get("evidence_verified") and not target.get("evidence_verified"):
        target["evidence_verified"] = True
        target["evidence"] = other.get("evidence", target.get("evidence"))


def merge_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse duplicates. Exact key first, then near-duplicate titles within a file."""
    merged: List[Dict[str, Any]] = []
    exact: Dict[Tuple[str, str, Any], Dict[str, Any]] = {}

    for f in findings or []:
        f.setdefault("occurrences", 1)
        key = (
            str(f.get("control_id", "")).upper(),
            str(f.get("file_path", "")).lstrip("./").lower(),
            f.get("line_start"),
        )
        if key in exact:
            _absorb(exact[key], f)
            continue
        exact[key] = f
        merged.append(f)

    # Near-duplicate pass: same control, same file, near-identical title. Catches the same
    # issue reported once per chunk when it sits in the deliberate overlap between them.
    out: List[Dict[str, Any]] = []
    for f in merged:
        match = None
        for kept in out:
            if str(kept.get("control_id", "")).upper() != str(f.get("control_id", "")).upper():
                continue
            if str(kept.get("file_path", "")).lower() != str(f.get("file_path", "")).lower():
                continue
            ratio = difflib.SequenceMatcher(
                None,
                _normalize(str(kept.get("title", ""))).lower(),
                _normalize(str(f.get("title", ""))).lower(),
            ).ratio()
            if ratio >= TITLE_SIMILARITY_THRESHOLD:
                match = kept
                break
        if match is not None:
            _absorb(match, f)
        else:
            out.append(f)
    return out


def sort_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Severity first, then verified before unverified, then file/line. Total and stable,
    so identical input always yields an identical list."""
    def key(f):
        try:
            sev = -int(f.get("severity", 0))
        except (TypeError, ValueError):
            sev = 0
        line = f.get("line_start")
        return (
            sev,
            0 if f.get("evidence_verified") else 1,
            str(f.get("file_path", "")),
            line if isinstance(line, int) else 10**9,
            str(f.get("title", "")),
        )
    return sorted(findings or [], key=key)


def apply_path_suppression(findings: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Move findings in test/vendor/generated paths out of the main list.

    They are returned, not deleted — a hardcoded secret in a test fixture is still worth
    seeing — but they no longer carry severity into the gate.
    """
    kept, suppressed = [], []
    for f in findings or []:
        if is_suppressed_path(str(f.get("file_path", ""))):
            f["suppressed_reason"] = "path is test, vendored, generated or build output"
            f["is_blocking"] = False
            suppressed.append(f)
        else:
            kept.append(f)
    return kept, suppressed


# Statuses a team can set on a finding to say "we know, and we accept it".
WAIVED_STATUSES = {"waived", "false_positive", "accepted_risk"}


def apply_baseline(
    findings: List[Dict[str, Any]],
    baseline: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Split findings against what this project has already seen and decided.

    Returns the list that should be reported, plus `new_findings` and `waived_findings`.

    Two deliberate choices:

    - Explicitly waived findings are removed from the reported list entirely. That is what
      waiving is for; leaving them in would make the feature pointless.
    - Findings seen before but *not* waived stay in the list and still count toward the
      gate. Excluding them would let a codebase with a hundred existing Criticals report
      Green, which is a far worse failure than a gate that takes work to turn green. They
      are labelled `is_new: False` so a caller can still say "3 new, 41 pre-existing"
      rather than "44 findings" for the tenth run in a row.
    """
    reported, new, waived = [], [], []
    for f in findings or []:
        prior = baseline.get(f.get("fingerprint") or "")
        if prior and prior.get("status") in WAIVED_STATUSES:
            f["is_new"] = False
            f["baseline_status"] = prior.get("status")
            f["waived_by"] = prior.get("resolved_by")
            f["waived_at"] = prior.get("resolved_at")
            f["waiver_comment"] = prior.get("resolution_comment")
            f["is_blocking"] = False
            waived.append(f)
            continue
        if prior:
            f["is_new"] = False
            f["first_seen_at"] = prior.get("first_seen")
        else:
            f["is_new"] = True
            new.append(f)
        reported.append(f)
    return {
        "findings": reported,
        "new_findings": new,
        "waived_findings": waived,
        "new_findings_count": len(new),
        "pre_existing_count": len(reported) - len(new),
        "waived_count": len(waived),
    }


def resolved_since(findings: List[Dict[str, Any]],
                   previously_open: Dict[str, Dict[str, Any]],
                   files_read: Optional[set] = None) -> Dict[str, Any]:
    """What the last review of this code reported and this one does not.

    `apply_baseline` above labels what is *here*. This labels what is *gone*, which is the
    half that was missing — and without it there is no burn-down, no fix rate and no
    time-to-remediate, because "still open" and "fixed" were the same absence.

    **Only files this run actually read can be credited.** Without that restriction a
    narrower review — one file instead of the whole repository — would report every finding
    in every file it did not open as fixed, which is worse than reporting nothing: it is a
    false all-clear that looks like progress. `files_read` comes from the coverage record,
    so a file in a *failed* chunk is correctly not in it. When it is None (a single pasted
    artifact, where there are no paths to compare) nothing is credited and
    `scope_limited` says so, rather than guessing.

    A finding with no `file_path` is never credited either: it is about the design rather
    than a line, so its absence from one run is not evidence anybody changed anything.
    """
    still_here = {f.get("fingerprint") for f in (findings or []) if f.get("fingerprint")}
    resolved, skipped = [], 0
    for fingerprint, prior in (previously_open or {}).items():
        if fingerprint in still_here:
            continue
        path = prior.get("file_path") or ""
        if not path:
            skipped += 1
            continue
        if files_read is None or path not in files_read:
            skipped += 1
            continue
        resolved.append({**prior, "fingerprint": fingerprint})
    resolved.sort(key=lambda r: (-(r.get("severity") or 0), r.get("file_path") or ""))
    return {
        "resolved_findings": resolved,
        "resolved_count": len(resolved),
        # How many previously-open findings this run could not speak to, because the file
        # was not read or the finding names no file. Reported rather than hidden: "2 fixed"
        # means something different when 40 were out of scope.
        "unverifiable_count": skipped,
        "scope_limited": files_read is None,
    }


def process(
    findings: List[Dict[str, Any]],
    artifact: str,
    correct_lines: bool = True,
) -> Dict[str, Any]:
    """Verify, suppress, merge, sort and fingerprint. Returns the trustworthy list plus
    what was removed and why.

    Baseline comparison is a separate step (`apply_baseline`) rather than a parameter here,
    because fingerprints only exist once merging has happened — so the caller has to run
    this first, look the fingerprints up, and then apply what it found.
    """
    verified, discarded = verify_evidence(findings, artifact, correct_lines)
    kept, suppressed = apply_path_suppression(verified)
    merged = sort_findings(merge_findings(kept))
    for f in merged:
        f["fingerprint"] = fingerprint(f)
    return {
        "findings": merged,
        "discarded_findings": discarded,
        "suppressed_findings": sort_findings(suppressed),
    }


# ── Per-file verification ─────────────────────────────────────────────────
#
# `verify_evidence` above searches one haystack. That is right for a paste and for a diff,
# and wrong for a repository, in two separate ways:
#
# 1. **Line numbers become meaningless.** Line 42 of `auth.py` is line 1,847 of a
#    concatenated bundle, so "correcting" a finding's line from a position recovered in the
#    bundle replaces a right answer with a wrong one — the same trap the diff case already
#    avoids by switching correction off. Switching it off for repositories too would mean
#    giving up line numbers on the exact input where they matter most.
#
# 2. **Mis-attribution passes verification.** A finding filed against `login.py` whose quote
#    actually lives in `admin.py` is verified today, because the quote is somewhere in the
#    haystack. It then anchors a PR comment to the wrong file.
#
# Verifying per file fixes both, and turns a weaker check into a stronger one: the evidence
# must appear in *the file the finding names*, not merely somewhere in the input.

# A quote found in exactly one other file is re-filed rather than discarded. More than one
# and we cannot tell which was meant, so the finding keeps its own claim and loses its
# verified status instead.
_REFILE_WHEN_UNIQUE = True


def _index_files(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Path -> everything needed to verify against that file, computed once, not per finding."""
    index: Dict[str, Any] = {}
    by_basename: Dict[str, List[str]] = {}
    for f in files or []:
        path = (f.get("path") or "").lstrip("./")
        if not path:
            continue
        content = f.get("content") or ""
        index[path] = {
            "content": content,
            "lines": content.splitlines(),
            "normalized": normalize(content),
        }
        by_basename.setdefault(path.rsplit("/", 1)[-1].lower(), []).append(path)
    return {"by_path": index, "by_basename": by_basename,
            "lower": {p.lower(): p for p in index}}


def _resolve_path(claimed: str, index: Dict[str, Any]) -> Optional[str]:
    """The real path a model meant, or None.

    Four tolerances, in order of confidence. A model shown `backend/routes/auth.py` will
    sometimes answer `routes/auth.py` or just `auth.py`, and discarding a real finding over a
    dropped directory prefix is a worse error than accepting it. Every tolerance below the
    first requires the match to be *unambiguous*, so nothing is guessed.
    """
    claimed = (claimed or "").lstrip("./").strip().replace("\\", "/")
    if not claimed:
        return None
    by_path = index["by_path"]
    if claimed in by_path:
        return claimed
    lowered = index["lower"].get(claimed.lower())
    if lowered:
        return lowered
    suffix_hits = [
        p for p in by_path
        if p.lower().endswith("/" + claimed.lower())
    ]
    if len(suffix_hits) == 1:
        return suffix_hits[0]
    basename_hits = index["by_basename"].get(claimed.rsplit("/", 1)[-1].lower(), [])
    if len(basename_hits) == 1:
        return basename_hits[0]
    return None


def verify_evidence_per_file(
    findings: List[Dict[str, Any]],
    files: List[Dict[str, Any]],
    correct_lines: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split findings into (kept, discarded), checking each against the file it names.

    Sets, per finding:
      `evidence_verified`   — the quote is really in that file
      `evidence_scope`      — "file", "ambiguous", or "unattributed"
      `reported_file_path`  — the model's original path, kept whenever it was corrected
      `file_path_corrected` — true when the finding was re-filed to where the quote really is

    Falls back to whole-artifact verification when no file carries a path, which is the
    paste and single-diff case — so this is a superset of `verify_evidence`, never a
    replacement that loses a behaviour.
    """
    index = _index_files(files)
    if not index["by_path"]:
        whole = "\n\n".join(f.get("content") or "" for f in files or [])
        return verify_evidence(findings, whole, correct_lines)

    kept, discarded = [], []

    for f in findings or []:
        evidence = (f.get("evidence") or "").strip()
        needle = normalize(evidence)
        claimed = str(f.get("file_path") or "")
        resolved = _resolve_path(claimed, index)

        if not evidence or len(needle) < 8:
            f["evidence_verified"] = False
            f["evidence_scope"] = "file" if resolved else "unattributed"
            if resolved and resolved != claimed.lstrip("./"):
                f["reported_file_path"], f["file_path"] = claimed, resolved
            kept.append(f)
            continue

        if resolved:
            entry = index["by_path"][resolved]
            idx = entry["normalized"].find(needle)
            if idx != -1:
                if resolved != claimed.lstrip("./"):
                    f["reported_file_path"], f["file_path"] = claimed, resolved
                f["evidence_verified"] = True
                f["evidence_scope"] = "file"
                if correct_lines:
                    actual = line_of_offset(entry["lines"], idx)
                    if actual is not None and f.get("line_start") != actual:
                        f["reported_line_start"] = f.get("line_start")
                        f["line_start"] = actual
                kept.append(f)
                continue

        # Either the path is unknown, or the quote is not in the file it named. Look for the
        # quote everywhere before discarding: a real issue filed against the wrong file is
        # still a real issue, and re-filing it is strictly more useful than dropping it.
        elsewhere = [
            p for p, entry in index["by_path"].items()
            if entry["normalized"].find(needle) != -1
        ]
        if _REFILE_WHEN_UNIQUE and len(elsewhere) == 1:
            real = elsewhere[0]
            entry = index["by_path"][real]
            f["reported_file_path"] = claimed
            f["file_path"] = real
            f["file_path_corrected"] = True
            f["evidence_verified"] = True
            f["evidence_scope"] = "file"
            if correct_lines:
                actual = line_of_offset(entry["lines"], entry["normalized"].find(needle))
                if actual is not None and f.get("line_start") != actual:
                    f["reported_line_start"] = f.get("line_start")
                    f["line_start"] = actual
            kept.append(f)
            continue

        if elsewhere:
            # Ambiguous: the quoted code exists, in several files, and the finding named a
            # different one. Unverified is the honest answer — there is real code behind it,
            # but we cannot say where, so nothing downstream should anchor a comment to it.
            f["evidence_verified"] = False
            f["evidence_scope"] = "ambiguous"
            f["evidence_note"] = (
                f"the quoted code appears in {len(elsewhere)} file(s), none of them the one "
                f"this finding names"
            )
            kept.append(f)
            continue

        f["discard_reason"] = (
            f"evidence quote does not appear in "
            f"{resolved or claimed or 'any reviewed file'}"
        )
        discarded.append(f)

    return kept, discarded


def process_files(
    findings: List[Dict[str, Any]],
    files: List[Dict[str, Any]],
    correct_lines: bool = True,
) -> Dict[str, Any]:
    """`process`, for a multi-file artifact. Same three passes, per-file verification."""
    verified, discarded = verify_evidence_per_file(findings, files, correct_lines)
    kept, suppressed = apply_path_suppression(verified)
    merged = sort_findings(merge_findings(kept))
    for f in merged:
        f["fingerprint"] = fingerprint(f)
    return {
        "findings": merged,
        "discarded_findings": discarded,
        "suppressed_findings": sort_findings(suppressed),
    }
