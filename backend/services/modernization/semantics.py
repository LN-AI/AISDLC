"""Source-idiom fidelity — the conversions that compile, run, and do something else.

`validation.py` asks whether a file is *structurally* sound; the compiler (Tier 2, in the
customer's CI) asks whether it builds. Neither can see the defect this module exists for:
C# that builds perfectly and disagrees with the legacy source about what a value means.

The motivating case, from a real run of the sample PHP app::

    // Salary is stored as int; fractional/non-numeric input is truncated to 0 via TryParse,
    // matching the (int)$_POST['salary'] cast in the source.
    int.TryParse(model.Salary, out var salary);

Every safeguard was already in place and every one of them missed it. The run had
*confirmed the business rule* ("salary is stored as an integer, truncating any decimal
portion") and frozen it into the system prompt. `PhpCompat.IntVal` shipped in the same
project, with a doc comment stating `(int)"12.9"` is `12`. The playbook names the helper in
a table. The model wrote the opposite anyway — and wrote a comment asserting the wrong PHP
semantics to justify it. `4242.99` stores as `0` where PHP stores `4242`.

So the check here is not "did the model read the instructions", which is unknowable. It is
the one question that can be answered from two texts we already hold:

    the source used idiom X, the output used the C# construct that is X's *plausible but
    wrong* translation, and the helper written precisely for X is called nowhere.

All three must hold. Any one alone is noise — `.IndexOf` is fine in a file whose source
never called `strpos`, and `PhpCompat.StrPos` being present means the model already knew.

**Only what is provable.** `strlen` -> `.Length` is a real defect class (bytes versus UTF-16
code units) and is deliberately *not* checked: `.Length` appears on every array and list in
generated C#, so the target side carries no signal and the check would fire on almost every
file. A rule that cannot be stated without guessing does not belong here — a false `fail`
costs a re-conversion, and a check nobody trusts gets switched off.
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Files this check reads. A `.cs` or `.cshtml` file is where converted logic lands; a
# `.csproj` or `.json` has no idioms to get wrong.
_TARGET_EXTENSIONS = (".cs", ".cshtml", ".razor", ".vb")

# `PhpCompat.cs` *implements* every idiom below, so it matches every target pattern by
# construction. It is scaffold — written deterministically, never by the converter — and
# judging it would fail every PHP run at the first unit.
_EXEMPT_BASENAMES = {"phpcompat.cs"}


class Check:
    """One idiom, and the three texts that decide whether it was translated faithfully.

    `helper` is quoted verbatim into the finding because "use PhpCompat.IntVal" is
    actionable and "consider PHP's integer semantics" is not — the model has already
    demonstrated it can reason its way to the wrong answer from the general statement.
    """

    def __init__(self, key: str, severity: str, source: "re.Pattern", target: "re.Pattern",
                 helper: "re.Pattern", helper_name: str, source_idiom: str,
                 target_idiom: str, why: str, rule_hints: tuple = ()):
        self.key = key
        self.severity = severity
        self.source = source
        self.target = target
        self.helper = helper
        self.helper_name = helper_name
        self.source_idiom = source_idiom
        self.target_idiom = target_idiom
        self.why = why
        self.rule_hints = rule_hints


# `fail` is reserved for the two where the divergence is provable from the idiom alone and
# the playbook names the exact replacement. The rest are `warn`: each is a genuine defect
# class, but each has a legitimate translation that would not call the helper, and a `fail`
# there would spend a re-conversion arguing with correct code.
PHP_CHECKS: List[Check] = [
    Check(
        key="int_cast",
        severity="fail",
        # `(int)$x`, `(integer)$x`, `intval($x)`. The `$` matters: `(int)` in front of a
        # literal is a constant the converter can fold, and nothing observable turns on it.
        source=re.compile(r"\(\s*int(?:eger)?\s*\)\s*\$|\bintval\s*\("),
        target=re.compile(r"\b(?:int|long|Int32|Int64)\s*\.\s*(?:Try)?Parse\s*\(|"
                          r"\bConvert\s*\.\s*ToInt(?:32|64)\s*\("),
        helper=re.compile(r"\bPhpCompat\s*\.\s*IntVal\b"),
        helper_name="PhpCompat.IntVal",
        source_idiom="(int) cast / intval()",
        target_idiom="int.TryParse / int.Parse / Convert.ToInt32",
        why=("PHP's `(int)` cast reads a leading numeric prefix and truncates: "
             "`(int)\"4242.99\"` is `4242` and `(int)\"12abc\"` is `12`. "
             "`int.TryParse` accepts neither and yields `0` for both, so a decimal amount "
             "a user typed is silently stored as zero."),
        rule_hints=("truncat", "integer", "numeric", "decimal", "fraction",
                    "whole number", "cast", "rounded"),
    ),
    Check(
        key="strpos",
        severity="fail",
        source=re.compile(r"\bstr[i]?pos\s*\("),
        target=re.compile(r"\.\s*IndexOf\s*\("),
        helper=re.compile(r"\bPhpCompat\s*\.\s*StrPos\b"),
        helper_name="PhpCompat.StrPos",
        source_idiom="strpos()",
        target_idiom=".IndexOf(...)",
        why=("`strpos` returns `false` when there is no match and `0` when the match is at "
             "the start; `.IndexOf` returns `-1` and `0`. Every condition written around "
             "the difference inverts, and `if (strpos(...))` was already a bug in the "
             "source for a match at offset 0 — which `.IndexOf` reproduces while looking "
             "correct."),
        rule_hints=("position", "substring", "contains", "occurrence", "index of",
                    "found", "search", "prefix"),
    ),
    Check(
        key="empty",
        severity="warn",
        source=re.compile(r"\bempty\s*\("),
        target=re.compile(r"\bstring\s*\.\s*IsNullOrEmpty\s*\(|"
                          r"\bstring\s*\.\s*IsNullOrWhiteSpace\s*\("),
        helper=re.compile(r"\bPhpCompat\s*\.\s*(?:Empty|Truthy)\b"),
        helper_name="PhpCompat.Empty / PhpCompat.Truthy",
        source_idiom="empty()",
        target_idiom="string.IsNullOrEmpty / IsNullOrWhiteSpace",
        why=("`empty()` is true for `0`, `\"0\"`, `[]` and `false` as well as `\"\"` and "
             "`null`. `\"0\"` in particular is a value a user can type into any form, and "
             "`string.IsNullOrEmpty(\"0\")` is false where PHP's `empty` is true."),
        rule_hints=("empty", "blank", "missing", "required", "not provided",
                    "optional", "omitted", "absent"),
    ),
    Check(
        key="in_array",
        severity="warn",
        source=re.compile(r"\bin_array\s*\("),
        target=re.compile(r"\.\s*Contains\s*\("),
        helper=re.compile(r"\bPhpCompat\s*\.\s*(?:InArray|ArraySearch)\b"),
        helper_name="PhpCompat.InArray",
        source_idiom="in_array()",
        target_idiom=".Contains(...)",
        why=("`in_array` without a third argument compares loosely, so `in_array(0, "
             "[\"apple\"])` is true before PHP 8. `.Contains` is always strict. Where the "
             "source passed `true` as the third argument, `.Contains` is the right "
             "translation and this finding can be dismissed."),
        rule_hints=("one of", "allowed", "permitted", "member of", "present in",
                    "valid value", "list of", "belongs"),
    ),
    Check(
        key="substr_negative",
        severity="warn",
        # Only a *negative* offset: `substr($s, 0, 5)` maps to `.Substring(0, 5)`
        # faithfully, and flagging it would fire on almost every string-handling file.
        source=re.compile(r"\bsubstr\s*\([^,()]+,\s*-"),
        target=re.compile(r"\.\s*Substring\s*\("),
        helper=re.compile(r"\bPhpCompat\s*\.\s*SubstrPhp\b"),
        helper_name="PhpCompat.SubstrPhp",
        source_idiom="substr() with a negative offset",
        target_idiom=".Substring(...)",
        why=("A negative offset counts back from the end in PHP. `.Substring` throws "
             "`ArgumentOutOfRangeException` on a negative argument, and PHP clamps an "
             "over-long length where `.Substring` throws."),
        rule_hints=("suffix", "trailing", "extension", "last ", "final ",
                    "truncat", "shorten"),
    ),
]

# Keyed on the *source* language, because that is what decides which idioms exist. The
# target matters too, but every stack this pipeline emits so far is .NET, and a second
# entry inventing patterns for a target nobody has run would be untested code that looks
# tested. Add one when a real run needs it.
_BY_LANGUAGE: Dict[str, List[Check]] = {"php": PHP_CHECKS}


def checks_for(language: str) -> List[Check]:
    return _BY_LANGUAGE.get((language or "").strip().lower(), [])


def _strip_comments(text: str) -> str:
    """Code with `//`, `/* */`, `#` and Razor `@* *@` comments blanked out.

    The converter explains itself in comments, and it explains itself using exactly the
    words this module matches on — the motivating case has `(int)$_POST['salary']` and the
    word `TryParse` inside a `//` comment two lines above the real call. Matching comment
    text would let a file that did the right thing and *described* the wrong one fail, and
    would let a file that did the wrong thing pass because it also named the helper in a
    comment. Neither is acceptable, so comments are removed from both sides.

    String literals are tracked only well enough that `"http://example.com"` does not eat
    the rest of its line. Blanked spans are replaced space-for-space so line numbers still
    match the original text.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                # A backslash escape inside a C#/PHP string. `@"..."` verbatim strings have
                # no escapes, but treating `\"` as one there only ends the scan late, which
                # blanks nothing extra because the closing quote is still found.
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if text.startswith("//", i) or (ch == "#" and not text.startswith("#[", i)):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if text.startswith("/*", i) or text.startswith("@*", i):
            closer = "*/" if text[i] == "/" else "*@"
            end = text.find(closer, i + 2)
            end = n if end < 0 else end + 2
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
            continue
        i += 1
    return "".join(out)


def _is_target_file(path: str) -> bool:
    lowered = (path or "").replace("\\", "/").lower()
    if lowered.rsplit("/", 1)[-1] in _EXEMPT_BASENAMES:
        return False
    return lowered.endswith(_TARGET_EXTENSIONS)


# How many distinct hint words a confirmed rule must contain before it is quoted next to a
# finding. One is not enough: the sample app has two rules on `add_employee.php`, and "a new
# employee's ID is … the next sequential **integer**" matches `int_cast` on that word alone
# while having nothing to do with the defect. Two is the point at which a rule is about the
# same subject rather than merely sharing vocabulary with it.
MIN_RULE_HINTS = 2


def _rule_citation(rules: Optional[Iterable[Dict[str, Any]]],
                   source_paths: Iterable[str], hints: Iterable[str] = ()) -> str:
    """The confirmed rule this finding contradicts, or nothing.

    This is the forward link the run has never had (C-41): comprehension confirms a rule
    against a line of source, conversion is handed the whole confirmed set, and nothing
    afterwards can say *which* rule a given output was supposed to honour. Naming it turns
    "this code uses the wrong helper" into "the run confirmed this and then broke it",
    which is a different and much harder finding to wave away.

    Two filters, and both are deliberately strict. The path match is exact rather than
    fuzzy, for the same reason `rules._resolve_source` refuses a basename match: attaching
    a rule to a file it was not read from points a human at code that does not contain the
    behaviour. The hint match then requires the rule to be about the same *subject* —
    citing a real rule that the finding does not actually contradict is the more damaging
    error of the two, because it reads as evidence and is not, so no citation is returned
    unless one clears the bar.
    """
    if not rules:
        return ""
    wanted = {(p or "").replace("\\", "/").lstrip("./") for p in source_paths if p}
    if not wanted:
        return ""
    hint_list = [h for h in hints if h]
    best, best_score = None, 0
    for rule in rules:
        cited = (rule.get("path") or "").replace("\\", "/").lstrip("./")
        statement = (rule.get("statement") or "").strip()
        if not statement or not cited or cited not in wanted:
            continue
        lowered = statement.lower()
        score = sum(1 for h in hint_list if h in lowered)
        if score > best_score:
            best, best_score = rule, score
    if not best or best_score < MIN_RULE_HINTS:
        return ""
    cited = (best.get("path") or "").replace("\\", "/").lstrip("./")
    line = best.get("line") or 0
    where = f"{cited}:{line}" if line else cited
    return f"{(best.get('statement') or '').strip()} ({where})"


def check(source: str, artifacts: List[Dict[str, Any]], *, language: str,
          rules: Optional[List[Dict[str, Any]]] = None,
          source_paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """-> {status, issues, checked, applicable}. Deterministic; no model call, no execution.

    `status` is `fail` if any `fail`-severity check fired, `warn` if only warnings did, and
    `ok` otherwise. `applicable` is false when the source language has no check table — a
    Delphi run is not silently reported as clean, it is reported as unchecked.
    """
    table = checks_for(language)
    if not table:
        return {"status": "ok", "issues": [], "checked": 0, "applicable": False}

    judged = [a for a in artifacts if _is_target_file(a.get("path") or "")]
    if not judged:
        return {"status": "ok", "issues": [], "checked": 0, "applicable": True}

    source_code = _strip_comments(source or "")
    stripped = {(a.get("path") or "").replace("\\", "/"): _strip_comments(a.get("content") or "")
                for a in judged}
    # Judged together rather than per file: a converter legitimately splits one PHP page
    # into a controller, a view model and a view, and the helper may be called in whichever
    # of them owns the value. "Did this unit call it anywhere" is the honest question;
    # asking it per file would fire on the two files that correctly delegate.
    target_code = "\n".join(stripped.values())

    issues: List[Dict[str, Any]] = []
    for entry in table:
        if not entry.source.search(source_code):
            continue
        if not entry.target.search(target_code):
            continue
        if entry.helper.search(target_code):
            continue
        issues.append({
            "kind": "semantic_drift",
            "check": entry.key,
            "severity": entry.severity,
            "helper": entry.helper_name,
            "source_idiom": entry.source_idiom,
            "target_idiom": entry.target_idiom,
            "why": entry.why,
            "rule": _rule_citation(rules, source_paths or [], entry.rule_hints),
            "files": sorted(path for path, body in stripped.items()
                            if entry.target.search(body)),
            "detail": (f"The source uses `{entry.source_idiom}` and this output uses "
                       f"`{entry.target_idiom}` without calling `{entry.helper_name}`. "
                       f"{entry.why}"),
        })

    status = "ok"
    if any(i["severity"] == "fail" for i in issues):
        status = "fail"
    elif issues:
        status = "warn"
    return {"status": status, "issues": issues, "checked": len(judged), "applicable": True}


def attach_rules(issues: List[Dict[str, Any]], rules: List[Dict[str, Any]],
                 source_paths: List[str]) -> List[Dict[str, Any]]:
    """Fill in each finding's `rule` citation, in place.

    Separate from `check` so the common path stays query-free: almost every unit in a run
    is clean, and reading the confirmed rules for each one would be a table scan per unit
    to decorate nothing. The caller loads them only once a finding exists.
    """
    by_key = {c.key: c for c in PHP_CHECKS}
    for issue in issues:
        entry = by_key.get(issue.get("check") or "")
        issue["rule"] = _rule_citation(rules, source_paths,
                                       entry.rule_hints if entry else ())
    return issues


def describe(issues: List[Dict[str, Any]]) -> str:
    """One line per finding, for an event or a checkpoint."""
    if not issues:
        return "No source-idiom drift found."
    return "\n".join(
        f"- [{i['severity']}] {i['check']}: {i['source_idiom']} -> {i['target_idiom']}, "
        f"`{i['helper']}` not called ({', '.join(i['files'][:3]) or 'no file'})"
        for i in issues
    )


def summarize_for_prompt(issues: List[Dict[str, Any]]) -> str:
    """The findings as a markdown block for a re-conversion prompt.

    Belongs in the *user* message, never the cached system block (D-06): it is per-unit by
    definition. Written as a statement of fact about the previous output rather than as
    advice, because the previous output already argued itself into the wrong answer from
    the general principle — in a comment, citing the source line it was getting wrong.
    """
    if not issues:
        return ""
    lines = [
        "## Behaviour drift in your previous output for this unit",
        "",
        "These were found by comparing the legacy source against the C# you produced. They "
        "are not style opinions: each one names a construct whose behaviour differs from "
        "the source for inputs a user can actually supply. Fix every one by calling the "
        "helper named, and change nothing else.",
        "",
    ]
    for issue in issues:
        lines.append(f"**{issue['source_idiom']} -> {issue['target_idiom']}** "
                     f"(in {', '.join(issue['files'][:4]) or 'this unit'})")
        lines.append("")
        lines.append(issue["why"])
        lines.append("")
        lines.append(f"Call `{issue['helper']}`. It is already in this project — written "
                     f"deterministically into every PHP conversion. You do not need to "
                     f"define it, import it, or re-derive what it does.")
        if issue.get("rule"):
            lines.append("")
            lines.append(f"This run confirmed the following rule against the source, and "
                         f"the output above does not honour it: *{issue['rule']}*")
        lines.append("")
    lines.append("Do not explain in a comment why the previous construct was equivalent. "
                 "It is not equivalent, which is why this block exists.")
    lines.append("")
    return "\n".join(lines)
