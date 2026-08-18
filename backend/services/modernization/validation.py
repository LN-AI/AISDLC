"""Tier 0 validation — everything worth checking that costs nothing and executes nothing.

The honest constraint first (DECISIONS.md D-05): this backend runs no subprocesses, and
running `dotnet build` on LLM-generated code derived from customer source would be
arbitrary code execution on the API host. It is also the wrong place for it — a real build
needs the real SDK and a package restore. So compilation is a Tier 2 concern that belongs
in the customer's own CI, reached by pushing a branch.

What is left is still worth a great deal, because it catches the failure modes that
actually make a 500-file conversion worthless:

* **Truncation and placeholders.** `// ... rest unchanged` in file 180 is the single most
  common way a long run quietly produces unusable output.
* **Structural breakage.** Unbalanced braces or an unterminated string mean the file cannot
  compile, and we can know that without compiling.
* **Unresolved symbols.** The converter declares which symbols from *other* units it
  references; those are checked against `modernization_symbols`. This is the check that is
  impossible without a run-scoped symbol registry, and it is exactly the defect class that
  makes naive per-file conversion fall apart — unit 200 calling an interface unit 40 never
  emitted. Nothing in the old `migration-execute` path could ever have detected it.
* **Reintroduced secrets.** Every emitted artifact is re-scanned with the same blocking
  scanner `autofix_pr` uses before committing. Ingest redacted the inputs; this catches a
  model that helpfully filled a placeholder back in with something that looks real.
"""
import ast
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from backend.services import guardrail_service

logger = logging.getLogger(__name__)

# Phrases that mean "I did not actually emit this file". Checked case-insensitively against
# the whole artifact — a converter that elides a region always says so in one of these ways.
_PLACEHOLDER_PATTERNS = [
    re.compile(r"(?:\.\.\.|…)\s*(?:rest|remainder|remaining|other)\s+(?:of\s+)?(?:the\s+)?"
               r"(?:code|file|method|class|implementation|logic)", re.I),
    re.compile(r"(?://|#|--|\{|/\*)\s*(?:rest|remainder)\s+(?:of\s+)?(?:the\s+)?"
               r"(?:code|file|class|method)\s+(?:is\s+)?(?:unchanged|omitted|same)", re.I),
    re.compile(r"(?://|#|--|\{|/\*)\s*(?:unchanged|omitted for brevity|truncated|"
               r"implementation (?:omitted|elided))", re.I),
    re.compile(r"(?://|#|--)\s*TODO:\s*(?:implement|convert|port|migrate)\b", re.I),
    re.compile(r"<\s*(?:insert|paste|original)\s+[^>]{0,40}\s*>", re.I),
]

# Comment syntax per family, for the balance scanner. Anything not listed is scanned with
# C-family rules, which is the common case and degrades to "no comments recognised".
_HASH_COMMENT = {".py", ".rb", ".pl", ".pm", ".yaml", ".yml", ".toml", ".sh", ".ps1"}
_PASCAL_LIKE = {".pas", ".dpr", ".dpk", ".inc"}
_SQL_LIKE = {".sql", ".pls", ".pks", ".pkb", ".prc"}
_NO_BALANCE_CHECK = {".md", ".txt", ".csv", ".properties"}

_PAIRS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _PAIRS.items()}


def _scan_balance(text: str, ext: str) -> List[str]:
    """Bracket balance and string termination, ignoring comments and string contents.

    A hand-rolled scanner rather than a parser per language: the goal is to catch a file
    that was cut off mid-generation, and for that "the braces don't close" is both
    sufficient and language-agnostic. It deliberately reports nothing it is unsure about —
    a false structural failure would fail a perfectly good conversion.
    """
    issues: List[str] = []
    hash_comments = ext in _HASH_COMMENT
    pascal = ext in _PASCAL_LIKE
    sql = ext in _SQL_LIKE
    stack: List[tuple] = []
    i, line, n = 0, 1, len(text)

    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        # Comments
        if hash_comments and ch == "#":
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if (sql or pascal) and text.startswith("--", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if pascal and ch == "{":
            end = text.find("}", i)
            if end == -1:
                issues.append(f"unterminated {{ }} comment starting at line {line}")
                break
            line += text.count("\n", i, end)
            i = end + 1
            continue
        if pascal and text.startswith("(*", i):
            end = text.find("*)", i)
            if end == -1:
                issues.append(f"unterminated (* *) comment starting at line {line}")
                break
            line += text.count("\n", i, end)
            i = end + 2
            continue
        if not hash_comments and text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if not hash_comments and not pascal and text.startswith("/*", i):
            end = text.find("*/", i)
            if end == -1:
                issues.append(f"unterminated /* */ comment starting at line {line}")
                break
            line += text.count("\n", i, end)
            i = end + 2
            continue
        # Strings
        if ch in ("'", '"') or (ch == "`" and not pascal):
            quote, start_line = ch, line
            j = i + 1
            while j < n:
                if text[j] == "\\" and not pascal and not sql:
                    j += 2
                    continue
                if text[j] == quote:
                    # Pascal and SQL escape a quote by doubling it.
                    if (pascal or sql) and j + 1 < n and text[j + 1] == quote:
                        j += 2
                        continue
                    break
                if text[j] == "\n":
                    line += 1
                    if quote != "`":
                        # A newline inside a single/double-quoted literal is a broken
                        # string in every language here except a JS template literal.
                        issues.append(f"unterminated string starting at line {start_line}")
                        break
                j += 1
            else:
                issues.append(f"unterminated string starting at line {start_line}")
            i = j + 1
            continue
        # Brackets
        if ch in _PAIRS and not pascal:
            stack.append((ch, line))
        elif ch in _CLOSERS and not pascal:
            if not stack:
                issues.append(f"unexpected '{ch}' at line {line}")
            elif stack[-1][0] != _CLOSERS[ch]:
                opener, opened_at = stack.pop()
                issues.append(f"'{opener}' at line {opened_at} closed by '{ch}' at line {line}")
            else:
                stack.pop()
        i += 1

    for opener, opened_at in stack[:5]:
        issues.append(f"'{opener}' opened at line {opened_at} is never closed")
    return issues


def _python_syntax(text: str) -> List[str]:
    try:
        ast.parse(text)
        return []
    except SyntaxError as exc:
        return [f"Python syntax error at line {exc.lineno}: {exc.msg}"]


def validate_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Structural checks on one emitted file. Never raises."""
    path = artifact.get("path") or ""
    content = artifact.get("content") or ""
    ext = os.path.splitext(path)[1].lower()
    issues: List[Dict[str, str]] = []

    if not content.strip():
        issues.append({"severity": "fail", "check": "empty",
                       "detail": "The converted file has no content."})
        return {"path": path, "status": "fail", "issues": issues}

    for pattern in _PLACEHOLDER_PATTERNS:
        match = pattern.search(content)
        if match:
            issues.append({
                "severity": "fail", "check": "placeholder",
                "detail": f"The file elides part of its own content: {match.group(0)[:120]!r}. "
                          f"A conversion that says 'rest unchanged' has not converted the rest.",
            })
            break

    if ext == ".py":
        for detail in _python_syntax(content):
            issues.append({"severity": "fail", "check": "syntax", "detail": detail})
    elif ext not in _NO_BALANCE_CHECK:
        for detail in _scan_balance(content, ext)[:5]:
            issues.append({"severity": "fail", "check": "structure", "detail": detail})

    secrets = guardrail_service.scan_for_hardcoded_secrets(content)
    if secrets:
        issues.append({
            "severity": "fail", "check": "secret",
            "detail": f"{len(secrets)} hardcoded credential pattern(s) in the generated file "
                      f"({', '.join(secrets[:3])}). Ingest redacted the source, so this was "
                      f"introduced during conversion.",
        })

    status = "fail" if any(i["severity"] == "fail" for i in issues) else \
             ("warn" if issues else "pass")
    return {"path": path, "status": status, "issues": issues}


BLOCKED_MARKER = "MIGRATION-BLOCKED"

# Which observed hazards actually block. Not every hazard is one: `removed_mysql_ext` and
# `weak_password_hash` are real findings but they *do* have correct target equivalents, so
# demanding a marker for them would fail honest conversions. These six have no safe
# mechanical translation at all — the correct output is a gap and a note.
BLOCKING_HAZARDS = frozenset({
    "eval", "variable_variable", "variable_include", "extract",
    "unserialize_request", "shell_exec",
})


def summarize_for_prompt(issues: List[Dict[str, Any]]) -> str:
    """The Tier-0 failures as a markdown block for the next attempt at the same unit.

    The mirror of `semantics.summarize_for_prompt`, and it exists for the same reason: without
    it a failed unit re-sent a byte-identical prompt, produced the same output, and spent all
    three attempts arguing with itself. Every other feedback path in this pipeline — build
    diagnostics, behaviour drift — already tells the next attempt what the last one got wrong;
    structural validation was the one that did not, and it is the one that fires first.

    Belongs in the *user* message, never the cached system block (D-06): it is per-unit.

    Phrased as a statement about the previous output rather than as a rule, because the rules
    were already in the prompt when that output was produced. Repeating a rule the model has
    already read and not followed is the least effective thing available; naming what it
    actually emitted is not.
    """
    if not issues:
        return ""
    lines = [
        "## Your previous output for this unit was rejected",
        "",
        "These are mechanical checks on the files you returned last time — not style "
        "opinions, and not re-statements of the rules above. Each one describes something "
        "about that output. Return the whole unit again with every one of them resolved, and "
        "change nothing else.",
        "",
    ]
    for issue in issues:
        where = issue.get("path") or "this unit"
        lines.append(f"- **{issue.get('check') or 'check'}** ({where}): "
                     f"{issue.get('detail') or ''}")
    return "\n".join(lines)


def check_blocked_hazards(artifacts: List[Dict[str, Any]], hazard_kinds: List[str],
                          manual_followup: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """The third and only enforced layer of the hazard rule.

    The source of this unit contained something with no safe mechanical equivalent — an
    `eval`, a variable variable, a dynamic include. The converter was told to leave a
    `// MIGRATION-BLOCKED:` marker and not implement it. If the output contains neither the
    marker nor a `manual_followup` entry, the model resolved it silently, which is the
    outcome the whole rule exists to prevent: an `eval` helpfully turned into a `switch` over
    the three cases visible in the file looks converted, passes review, and quietly drops
    every case that was not visible.

    A `fail` rather than a `warn`, and deliberately: a warning here is indistinguishable from
    the noise every large run produces, and this is the one class of silent behaviour change
    that no later stage can detect.
    """
    # Filtered here rather than by the caller, so a second call site cannot forget to and
    # start failing honest conversions of a `mysql_*` call that has a perfectly good target.
    hazard_kinds = sorted(set(hazard_kinds or []) & BLOCKING_HAZARDS)
    if not hazard_kinds:
        return []
    blob = "\n".join(a.get("content") or "" for a in artifacts)
    followup = " ".join(str(x) for x in (manual_followup or []))
    if BLOCKED_MARKER in blob:
        return []
    if BLOCKED_MARKER.lower() in followup.lower() or any(
            kind.replace("_", " ") in followup.lower() or kind in followup.lower()
            for kind in hazard_kinds):
        return []
    return [{
        "severity": "fail", "check": "blocked_hazard",
        "detail": (f"The source contained {', '.join(sorted(set(hazard_kinds)))}, which has no "
                   f"safe equivalent in the target — but the output contains no "
                   f"`// {BLOCKED_MARKER}:` marker and nothing in `manual_followup` mentions "
                   f"it. Either it was translated silently, or it was dropped silently."),
    }]


def check_symbols(references: Iterable[Dict[str, Any]], known: Set[str],
                  allow_external: bool = True) -> List[Dict[str, str]]:
    """Cross-file coherence: did this unit reference something no unit has emitted?

    `references` is what the converter declared it depends on; `known` is every symbol name
    already in `modernization_symbols` for this run, plus whatever this unit itself
    exports. A reference marked `external: true` (a framework or package type) is accepted
    on trust — we are not converting those and have no registry for them.

    This is the check that only exists because the run has a symbol registry. Without one,
    the platform could produce 500 individually-plausible files that do not fit together
    and report every one of them as a success.
    """
    issues: List[Dict[str, str]] = []
    for reference in references or []:
        name = (reference.get("name") or "").strip()
        if not name:
            continue
        if allow_external and reference.get("external"):
            continue
        if name in known or name.rsplit(".", 1)[-1] in known:
            continue
        issues.append({
            "severity": "warn", "check": "unresolved_symbol",
            "detail": f"References '{name}'"
                      + (f" from {reference['from_unit']}" if reference.get("from_unit") else "")
                      + ", which no converted unit has declared. Either the dependency has "
                        "not been converted yet or the name is wrong.",
        })
    return issues


def validate_unit_output(
    artifacts: List[Dict[str, Any]],
    references: Iterable[Dict[str, Any]],
    known_symbols: Set[str],
    hazard_kinds: Optional[List[str]] = None,
    manual_followup: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Everything Tier 0 has to say about one converted unit.

    `status` is `fail` only for defects that make the output unusable as code. An
    unresolved symbol is a `warn`: it is frequently a real bug, but it is also what a
    legitimate forward reference inside a converted cycle looks like, and failing those
    would make every cyclic codebase unconvertible.
    """
    per_file = [validate_artifact(a) for a in artifacts]
    issues: List[Dict[str, str]] = []
    for report in per_file:
        for issue in report["issues"]:
            issues.append({**issue, "path": report["path"]})
    issues.extend(check_symbols(references, known_symbols))
    issues.extend(check_blocked_hazards(artifacts, hazard_kinds or [], manual_followup))

    failed = [i for i in issues if i["severity"] == "fail"]
    status = "fail" if failed else ("warn" if issues else "pass")
    return {
        "status": status,
        "files": [{"path": r["path"], "status": r["status"]} for r in per_file],
        "issues": issues,
        "failed_checks": sorted({i["check"] for i in failed}),
    }
