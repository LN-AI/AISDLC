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


def _normalize(text: str) -> str:
    """Collapse whitespace so an evidence quote matches regardless of indentation or how
    the model re-wrapped it."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def is_suppressed_path(path: str) -> bool:
    return bool(path) and bool(_SUPPRESSED_PATH_RE.search(path))


def _line_of_offset(haystack_lines: List[str], normalized_offset: int) -> Optional[int]:
    """Map an offset in the whitespace-normalized text back to a 1-based line number."""
    running = 0
    for i, line in enumerate(haystack_lines, start=1):
        running += len(_normalize(line)) + 1
        if running > normalized_offset:
            return i
    return None


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

        idx = normalized.find(needle)
        if idx == -1:
            f["discard_reason"] = "evidence quote does not appear in the reviewed artifact"
            discarded.append(f)
            continue

        f["evidence_verified"] = True
        if correct_lines:
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
