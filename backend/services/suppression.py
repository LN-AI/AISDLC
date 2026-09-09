"""Suppression as code — a team's accepted risks, kept in their repository.

**The problem.** A waiver today is a row in `finding_waivers`, set by a person clicking in
the UI. That is right for a governance decision somebody has to sign, and wrong for the
much larger class of "this is a fixture", "this whole tree is scheduled for deletion",
"this line is deliberate and we have said so four times". Those get re-entered by hand on
every project, they are invisible in code review, they do not travel when a repository is
forked, and nobody can see them without logging in.

So: two mechanisms that live in the repository, next to the code they are about.

* **`.lmignore` at the repository root** — path globs, optionally narrowed to specific
  controls. Reviewed like any other file, diffed like any other file.
* **An inline `lm-ignore` comment** — one line, at the line it is about.

**The one rule that makes this safe: an ignore lowers the gate, it does not hide the
evidence.** A suppressed finding is still reported, still exported to SARIF (marked
`suppressed`, with the rule that did it as its justification), and still counted. What it
loses is its vote in the gate. Deleting findings from a file in the repository would mean
anyone with commit access could turn a Red release Green by adding two lines, and the
review would agree with them in writing. This is the same treatment
`finding_pipeline.apply_path_suppression` already gives test and vendor paths, for the
same reason.

**The second rule: a directive that matched nothing says so.** Both mechanisms report
their unused rules (`report()`), because the failure this feature invites is a silently
misspelled pattern — the developer believes they have suppressed something, the build
still fails, and the tool looks broken. An unused rule in the result is a typo you can
see.

Nothing here reads a filesystem or a network. `.lmignore` arrives as text (from the file
list an ingest already produced, or fetched by `source_bundle` for a diff source), which
keeps this module testable with a string and keeps the fetch decision where the source
types are known.

## `.lmignore` syntax

    # A comment. Blank lines ignored.
    legacy/**                        # everything under legacy/
    fixtures                         # any path segment named "fixtures"
    *.generated.ts                   # by basename, anywhere
    src/parser.py: SEC-003           # that file, only that control
    infra/**: SEC-003, SEC-010  known, tracked in JIRA-4412
    !legacy/payments/**              # ...but not this subtree

Glob subset, deliberately smaller than gitignore's and documented rather than implied:
`*` matches within one path segment, `**` matches across segments, `?` matches one
character, a trailing `/` means "and everything under it", a pattern with no `/` and no
wildcard matches any whole path segment (so `vendor` covers `a/vendor/b.py`), and a
pattern with no `/` but with a wildcard matches the basename. Leading `!` negates.
**Last matching rule wins**, which is the one gitignore behaviour people rely on.

Anything after the pattern is `controls` then a free-text reason: a token that looks like
a control id (`SEC-003`, `CWE-89`, `OWASP-A01`) is one, and the first token that does not
starts the reason.

## Inline syntax

    query = f"SELECT * FROM t WHERE id={uid}"   # lm-ignore: SEC-001 admin-only tool
    // lm-ignore-next-line CWE-79 sanitized by the caller
    /* lm-ignore-file  vendored, upstream's problem */

The comment marker is not parsed — the directive is found anywhere in the line — because
this has to work in Python, Go, SQL, YAML, HTML and Terraform without a language table,
and a wrong table would silently drop directives in whichever language it got wrong.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# The one name. A second spelling would mean a repository could have two files disagreeing
# and no rule for which wins.
IGNORE_FILENAME = ".lmignore"

# A cap, because this is attacker-adjacent input in the sense that matters here: it comes
# from the repository under review, and every rule is matched against every finding.
MAX_RULES = 500

# `SEC-003`, `CWE-89`, `OWASP-A01`, `ISO-27001.1`. Deliberately requires a separator and a
# digit, so an ordinary English word in a reason is never mistaken for a control id.
_CONTROL_RE = re.compile(r"^[A-Za-z]{2,10}[-_][A-Za-z]{0,4}\d{1,5}(?:\.\d{1,4})?$")

# `lm-ignore`, `lm-ignore-line` (same thing, spelled explicitly), `lm-ignore-next-line`,
# `lm-ignore-file`. Case-insensitive; `lmignore` without the dash is accepted because it
# is the spelling people type after seeing the filename.
_INLINE_RE = re.compile(
    r"lm-?ignore(?:-(next-line|nextline|line|file))?\b[ \t]*:?[ \t]*([^\r\n]*)",
    re.IGNORECASE,
)

# How far into a file a `lm-ignore-file` directive is honoured. Unbounded would mean a
# directive buried on line 900 silently exempting the whole file, which nobody reading the
# top of the file could see.
FILE_DIRECTIVE_HEADER_LINES = 20


def _norm(path: Any) -> str:
    """Repository-relative, forward slashes, no `./` prefix.

    Not `lstrip` with a "./" argument, which strips a character *set* rather than a prefix
    — it turns `.lmignore` into `lmignore` and `.github/workflows/ci.yml` into
    `github/...`, so the ignore file could not be found by its own name and no rule about a
    dotfile could ever match. Found by running it, which is the only way this shows up.
    """
    p = str(path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


@dataclass
class Rule:
    """One line of `.lmignore`."""

    pattern: str
    regex: Any
    controls: frozenset = frozenset()   # empty = every control
    reason: str = ""
    line_no: int = 0
    negate: bool = False
    hits: int = 0

    def covers(self, path: str, control_id: str) -> bool:
        if not self.regex.match(path):
            return False
        if self.controls and (control_id or "").upper() not in self.controls:
            return False
        return True

    def label(self) -> str:
        bits = [self.pattern]
        if self.controls:
            bits.append(":" + ",".join(sorted(self.controls)))
        return "".join(bits)

    def justification(self, source: str) -> str:
        where = f"{source}:{self.line_no}" if self.line_no else source
        return f"{where} — {self.reason}" if self.reason else f"{where} ({self.label()})"


@dataclass
class Directive:
    """One `lm-ignore` comment found in a file."""

    path: str
    line: int
    kind: str = "line"                  # line | next-line | file
    controls: frozenset = frozenset()
    reason: str = ""
    hits: int = 0

    def covers(self, path: str, line_start: Optional[int], line_end: Optional[int],
               control_id: str) -> bool:
        if (path or "") != self.path:
            return False
        if self.controls and (control_id or "").upper() not in self.controls:
            return False
        if self.kind == "file":
            return True
        # A finding that names no line cannot be matched to a line directive. Returning
        # True here would let one comment exempt a whole file by accident, which is what
        # `lm-ignore-file` is for and has to be asked for.
        if not isinstance(line_start, int) or line_start <= 0:
            return False
        end = line_end if isinstance(line_end, int) and line_end >= line_start else line_start
        target = self.line + 1 if self.kind == "next-line" else self.line
        return line_start <= target <= end

    def justification(self) -> str:
        where = f"{self.path}:{self.line}"
        kind = "lm-ignore-file" if self.kind == "file" else "lm-ignore"
        return f"{kind} at {where} — {self.reason}" if self.reason else f"{kind} at {where}"


@dataclass
class Suppressions:
    """Everything the repository says about what not to gate on."""

    rules: List[Rule] = field(default_factory=list)
    inline: List[Directive] = field(default_factory=list)
    source: str = IGNORE_FILENAME
    errors: List[str] = field(default_factory=list)
    # Set when a `.lmignore` was looked for and not found, versus never looked for. The
    # difference matters in the report: "no ignore file" is a fact, "we did not check" is
    # not the same fact.
    checked: bool = False

    def __bool__(self) -> bool:
        return bool(self.rules or self.inline)

    def reset(self) -> None:
        """Zero the hit counters. Called before a review's findings are matched, so a
        retried run reports which rules fired on *that* run rather than accumulating."""
        for r in self.rules:
            r.hits = 0
        for d in self.inline:
            d.hits = 0

    # ── Matching ─────────────────────────────────────────────────────────
    def match(self, finding: Dict[str, Any]) -> Optional[str]:
        """The justification for suppressing this finding, or None to keep it.

        Inline directives are checked first and separately from path rules: they are more
        specific by construction (a line, not a tree) and a negated path rule is not meant
        to overrule a comment somebody wrote on the line itself.
        """
        path = _norm(finding.get("file_path"))
        control = str(finding.get("control_id") or "")
        line_start, line_end = finding.get("line_start"), finding.get("line_end")

        for d in self.inline:
            if d.covers(path, line_start, line_end, control):
                d.hits += 1
                return d.justification()

        if not path:
            return None

        # Last match wins, so a `!` rule after a broad one carves an exception out of it.
        winner: Optional[Rule] = None
        for rule in self.rules:
            if rule.covers(path, control):
                winner = rule
        if winner is None or winner.negate:
            if winner is not None:
                winner.hits += 1     # a negation that fired did its job
            return None
        winner.hits += 1
        return winner.justification(self.source)

    def match_evidence(self, finding: Dict[str, Any]) -> Optional[str]:
        """An inline directive read out of the finding's own evidence quote.

        The fallback for a **diff** source, where there is no file text to scan and so
        `inline` is empty. Less precise on purpose and worth knowing: an evidence quote can
        span several lines, so a directive on a neighbouring line inside the quote counts
        here where it would not in a file. Accepted because the alternative is that inline
        suppression silently does nothing on the source type most reviews use — a pull
        request — and because a quote is a few lines, not a file.
        """
        control = str(finding.get("control_id") or "").upper()
        for _kind, controls, reason, _offset in _directives_in(str(finding.get("evidence") or "")):
            if controls and control not in controls:
                continue
            note = "lm-ignore in the flagged code"
            return f"{note} — {reason}" if reason else note
        return None

    # ── Reporting ────────────────────────────────────────────────────────
    def report(self) -> Dict[str, Any]:
        """What to put on the result so a reader can audit the suppressions.

        `unused` is the point of this. A misspelled pattern is the failure this feature
        invites, and it is invisible otherwise: the developer believes they suppressed
        something, the gate still fails, and the tool takes the blame.
        """
        unused = [
            {"rule": r.label(), "line": r.line_no}
            for r in self.rules if not r.hits and not r.negate
        ]
        unused_inline = [
            {"rule": "lm-ignore", "path": d.path, "line": d.line}
            for d in self.inline if not d.hits
        ]
        return {
            "source": self.source if self.rules else "",
            "checked": self.checked,
            "rules": len(self.rules),
            "rules_fired": sum(1 for r in self.rules if r.hits),
            "inline_directives": len(self.inline),
            "inline_fired": sum(1 for d in self.inline if d.hits),
            "findings_suppressed": (sum(r.hits for r in self.rules if not r.negate)
                                    + sum(d.hits for d in self.inline)),
            # Capped: an unused-rule list of 400 rows is not read, and the count above is
            # the number that matters.
            "unused": (unused + unused_inline)[:25],
            "unused_count": len(unused) + len(unused_inline),
            # A rule with no stated reason is still honoured — refusing one would block
            # adoption over a style rule — but the count travels, because "9 of 14
            # suppressions give no reason" is a fact a governance report should be able to
            # state.
            "rules_without_reason": sum(1 for r in self.rules if not r.negate and not r.reason),
            "errors": self.errors[:25],
        }


# ── Parsing ──────────────────────────────────────────────────────────────
def _compile(pattern: str) -> Any:
    """Translate the documented glob subset to a regex anchored at both ends.

    Written out rather than handed to `fnmatch`, because `fnmatch`'s `*` matches `/` — so
    `src/*.py` would match `src/a/b/c.py` and a rule meant for one directory would quietly
    cover a subtree.
    """
    p = pattern.strip().replace("\\", "/").lstrip("/")
    if p.endswith("/"):
        p = p.rstrip("/") + "/**"
    has_slash = "/" in p
    has_wild = any(c in p for c in "*?[")

    body, i = [], 0
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            body.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            body.append(".*")
            i += 2
        elif c == "*":
            body.append("[^/]*")
            i += 1
        elif c == "?":
            body.append("[^/]")
            i += 1
        else:
            body.append(re.escape(c))
            i += 1
    core = "".join(body)

    if not has_slash:
        if has_wild:
            # A basename pattern: `*.min.js` anywhere in the tree.
            return re.compile(rf"(?:.*/)?{core}$")
        # A bare name: any whole path segment, file or directory — `vendor` covers
        # `vendor/x.py` and `a/vendor/x.py` and a file literally called `vendor`.
        return re.compile(rf"(?:.*/)?{core}(?:/.*)?$")
    return re.compile(rf"{core}$")


def _split_controls(rest: str) -> tuple:
    """`SEC-003, CWE-89 because reasons` -> ({SEC-003, CWE-89}, 'because reasons')."""
    text = (rest or "").strip()
    if not text:
        return frozenset(), ""
    tokens = text.replace(",", " ").split()
    controls, consumed = [], 0
    for tok in tokens:
        if _CONTROL_RE.match(tok):
            controls.append(tok.upper())
            consumed += 1
        else:
            break
    reason = " ".join(tokens[consumed:]).strip(" \t-–—:#")
    return frozenset(controls), reason


def parse(text: str, source: str = IGNORE_FILENAME) -> Suppressions:
    """Parse a `.lmignore`. Never raises — a bad line is an error in the report.

    Refusing the whole file over one typo would mean a review of a repository with a
    slightly wrong ignore file behaves as though it had none, with no explanation. Every
    line that parses is honoured and every line that does not is named.
    """
    sup = Suppressions(source=source, checked=True)
    for n, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(sup.rules) >= MAX_RULES:
            sup.errors.append(
                f"line {n}: {source} has more than {MAX_RULES} rules; the rest were "
                f"ignored. A file this size is suppressing more than it is reviewing."
            )
            break

        negate = line.startswith("!")
        if negate:
            line = line[1:].strip()

        pattern, sep, rest = line.partition(":")
        pattern = pattern.strip()
        # A pattern can carry the controls after whitespace instead of a colon, because
        # `legacy/** SEC-003` is what people write. Only split on whitespace when there
        # was no colon, so a reason containing spaces survives.
        if not sep and " " in pattern:
            pattern, _, rest = pattern.partition(" ")
        if not pattern:
            sup.errors.append(f"line {n}: no path pattern before the ':'")
            continue

        controls, reason = _split_controls(rest)
        try:
            regex = _compile(pattern)
        except re.error as exc:
            sup.errors.append(f"line {n}: '{pattern}' is not a usable pattern ({exc})")
            continue
        sup.rules.append(Rule(pattern=pattern, regex=regex, controls=controls,
                              reason=reason, line_no=n, negate=negate))
    return sup


def _directives_in(text: str) -> List[tuple]:
    """Every `lm-ignore` in one piece of text, as (kind, controls, reason, offset)."""
    out = []
    for m in _INLINE_RE.finditer(text or ""):
        suffix = (m.group(1) or "line").lower()
        kind = "file" if suffix == "file" else (
            "next-line" if suffix in ("next-line", "nextline") else "line")
        controls, reason = _split_controls(m.group(2) or "")
        out.append((kind, controls, reason, m.start()))
    return out


def scan_inline(files: Sequence[Dict[str, Any]]) -> List[Directive]:
    """Find every inline directive in a file list.

    One regex pass over content already in memory. The cheap guard is `"lm" in content`
    before the regex runs, which skips the overwhelming majority of files for the cost of
    a substring search.
    """
    found: List[Directive] = []
    for f in files or []:
        path = _norm(f.get("path"))
        content = f.get("content") or ""
        if not path or "ignore" not in content.lower():
            continue
        for n, line in enumerate(content.splitlines(), start=1):
            if "ignore" not in line.lower():
                continue
            for kind, controls, reason, _ in _directives_in(line):
                if kind == "file" and n > FILE_DIRECTIVE_HEADER_LINES:
                    # Honoured only near the top, where a reader would see it. Further
                    # down it is recorded as a line directive instead of silently
                    # exempting everything above and below it.
                    kind = "line"
                found.append(Directive(path=path, line=n, kind=kind,
                                       controls=controls, reason=reason))
    return found


def from_files(files: Sequence[Dict[str, Any]],
               comment_files: Optional[Sequence[Dict[str, Any]]] = None) -> Suppressions:
    """Build the whole picture from a file list.

    Two lists on purpose, because the right input differs for the two mechanisms.
    `.lmignore` must be looked for in the **raw** list, before extension filtering: it has
    no reviewable extension, so `repo_ingest.normalize_files` drops it, and reading it from
    the filtered list would mean the feature never worked on a real repository. Inline
    directives want the **reviewed** list (`comment_files`) — scanning content that was
    filtered out costs time and can only produce directives for findings that cannot exist.
    """
    text, where = "", ""
    for f in files or []:
        path = _norm(f.get("path"))
        # Root only. A `.lmignore` in a subdirectory is a gitignore habit that would need
        # per-directory scoping to mean anything, and quietly treating it as
        # repository-wide is the wrong half of that feature.
        if path == IGNORE_FILENAME:
            text, where = f.get("content") or "", path
            break
    sup = parse(text, source=where or IGNORE_FILENAME)
    sup.checked = True
    sup.inline = scan_inline(comment_files if comment_files is not None else files)
    return sup


def fetch(github: Any, owner: str, repo: str, ref: str) -> Suppressions:
    """Read `.lmignore` out of a repository, for a source that has no file list.

    A pull request and a `base...head` compare are single unified diffs — there is no file
    list to find the ignore file in, and the alternative to one extra Contents API call is
    that suppression as code does not work on pull requests, which is where reviews
    actually run.

    Fails open, deliberately and loudly in the log only: a missing `.lmignore` is a 404 and
    the overwhelmingly common case, and a repository whose ignore file cannot be read
    should still get reviewed rather than error.
    """
    sup = Suppressions(checked=True)
    if not (github and owner and repo):
        sup.checked = False
        return sup
    try:
        data = github.get_file_content_at_ref(owner, repo, IGNORE_FILENAME, ref or "")
        return parse(data.get("content") or "")
    except Exception as exc:                                  # noqa: BLE001
        logger.debug("no readable %s in %s/%s@%s (%s)",
                     IGNORE_FILENAME, owner, repo, ref, exc)
        return sup


def to_ignore_file(waivers: Iterable[Dict[str, Any]]) -> str:
    """Render existing waivers as a `.lmignore`, so a team can stop re-entering them.

    The migration path, and the reason this feature is worth having at all: a project that
    has been clicking waivers for months can export them once and commit the result. Each
    row becomes a rule narrowed to the exact path *and* the exact control, which is
    narrower than most people would write by hand — a waiver was granted about one finding,
    and widening it to a whole tree on export would grant something nobody approved.

    Fingerprint-level precision is deliberately *not* reproduced: a fingerprint covers the
    title and the evidence too, and there is no `.lmignore` syntax for that. The comment on
    each line says what was actually waived so the difference is visible in review.
    """
    lines = [
        "# Generated from this project's existing waivers.",
        "# Each rule is narrowed to the path and control the waiver was granted for.",
        "# Review before committing: a waiver covered one finding, a rule covers a path.",
        "",
    ]
    seen = set()
    for w in waivers or []:
        path = _norm(w.get("file_path"))
        if not path:
            # Nothing to key a rule on. Named rather than dropped silently, because a
            # waiver that does not survive the export is one somebody still has to re-enter.
            lines.append(f"# SKIPPED (no file path): {w.get('title') or w.get('fingerprint')}")
            continue
        control = str(w.get("control_id") or "").strip()
        key = (path, control.upper())
        if key in seen:
            continue
        seen.add(key)
        reason = " ".join(str(w.get("reason") or w.get("status") or "waived").split())[:120]
        rule = f"{path}: {control} {reason}" if control else f"{path}: {reason}"
        lines.append(rule)
    return "\n".join(lines) + "\n"
