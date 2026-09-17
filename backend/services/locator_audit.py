"""Which locators in a generated suite came from the session, and which were remembered.

The observation contract is narrow on purpose, and that narrowness is what makes this
checkable rather than a matter of taste. `COLLECT_JS` in `scripts/explore_app.py` emits
exactly three ways to address an element, and the suite writer is shown nothing else:

    getByRole('button', { name: 'Sign In' })       a real role in the accessibility tree
    getByText('Go to shop', { exact: true })       no role, but rendered text
    locator("input[name='username']")              neither resolves; the name attribute does

There is no class name anywhere in that list. There is no id, and no test id. So a
`page.locator('.inventory_item_price')` in the output **cannot** have come from anything this
session observed -- the model recalled it from training, and it happened to be right because
the application was famous. On a customer's application it matches nothing.

That failure runs in the expensive direction. A locator matching nothing does not report a
missing feature; it fails against a *correct* build, and a suite that cries wolf is switched
off by the team that owns it, which leaves them worse off than no suite at all. It cost the
2026-09-16 live run its only arithmetic check: a total computed from `.inventory_item_price`
summed to 0 and was compared against a displayed 55.97 that was right.

Why this is code and not another sentence in the prompt
------------------------------------------------------
The prompt has said "Never write a CSS selector or an XPath" since the first version, and
the model wrote nineteen of them anyway. A rule the model already has and does not follow is
not fixed by repeating it, so this module is the part that does not depend on persuasion: it
counts, it reports, and it gives `generate_suite` something specific to hand back. D-406's
rule again -- a model's claim about its own output is not evidence -- aimed at locators.

The line drawn here, and why it is where it is
----------------------------------------------
Not all raw CSS is invented, and calling it all invented would produce a number nobody
trusts. What matters is whether the selector names something **specific to this application**
that the session never carried:

* **invented** -- a class, an id, a test id, an attribute other than `name`, or an XPath.
  Each names a token unique to the application under test, and none of them was ever sent.
* **grounded** -- the `getBy*` family, `waitForURL`, and the `[name='...']` form the
  collector itself supplies (D-427). The string inside came from the observation.
* **generic** -- bare tags and combinators (`tr`, `tbody td`), Playwright's `text=` engine,
  `:has-text(...)`. Structural or text-based rather than recalled. Fragile, sometimes the
  only honest answer when narrowing inside a row, and counted separately rather than blamed.

`getByTestId` sits under **invented** deliberately, and it is the one people argue about: a
test id is the most durable locator there is *when the application has one*, but this session
was never shown any, so a test id in the output is as recalled as a class name. Counting it
as grounded because it looks like good practice would be the flattering-number error the
eval exists to catch. It carries its own `kind` so the split stays visible.

What this module does not do
----------------------------
It does not edit the suite. Mechanically rewriting generated test code is how a working file
becomes a broken one, and a locator cannot be repaired without knowing what the test meant.
The response to a finding is a bounded retry in `app_explorer_agent.generate_suite` and,
failing that, a count on the result that the reader can see.
"""
import re
from typing import Any, Dict, List

# How many offending selectors travel on the result and into the retry note. Enough to show a
# pattern, bounded because this goes in a stored review row and into a prompt.
MAX_REPORTED = 25

# Locators whose content came from the observation. `waitForURL` is here because a path is
# something the session witnessed (D-430) and the sign-in helper is where it matters most.
_GROUNDED_CALLS = (
    "getByRole", "getByText", "getByLabel", "getByPlaceholder", "getByTitle",
    "getByAltText", "waitForURL",
)
_GROUNDED_RE = re.compile(r"\.(?:" + "|".join(_GROUNDED_CALLS) + r")\s*\(")

# A test id is application-specific and was never supplied, so it is counted with the
# invented ones -- but found separately, because it is not a selector string.
_TESTID_RE = re.compile(r"\.getByTestId\s*\(")

# Calls whose first argument is a selector string.
#
# `.locator(`, `.waitForSelector(` and `$`/`$$` take a selector on any receiver. The
# one-shot action methods (`click`, `fill`, ...) take a selector *only* when called on a
# page or a frame -- on a Locator they take a value or nothing at all, and reading
# `loc.fill('standard_user')` as a selector would invent a finding out of a correct line.
_SELECTOR_PATTERNS = (
    re.compile(r"\.(?:locator|waitForSelector)\s*\(\s*(['\"`])(.*?)\1", re.S),
    re.compile(r"\b(?:page|frame)\s*\.\s*\$\$?(?:eval)?\s*\(\s*(['\"`])(.*?)\1", re.S),
    re.compile(
        r"\b(?:page|frame)\s*\.\s*(?:click|fill|type|press|check|uncheck|selectOption"
        r"|hover|dblclick|focus|tap|textContent|innerText|innerHTML|isVisible|isEnabled"
        r"|isChecked|getAttribute|setInputFiles|dispatchEvent|dragAndDrop)"
        r"\s*\(\s*(['\"`])(.*?)\1",
        re.S,
    ),
)

# A CSS class or id token: a dot or hash followed by something that can start an identifier.
# The leading-character rule is what keeps `$29.99` and `1.5` out of the class count.
_CLASS_RE = re.compile(r"\.-?[A-Za-z_][\w-]*")
_ID_RE = re.compile(r"#-?[A-Za-z_][\w-]*")
# An attribute selector, capturing the attribute name so `[name=...]` can be told from the
# rest. `[name='x']` is the one raw form the collector itself hands over.
_ATTR_RE = re.compile(r"\[\s*([A-Za-z_:][\w:.-]*)\s*(?:[~^|*$]?=|\])")

# Engine prefixes Playwright accepts in front of a selector.
_ENGINES = ("xpath=", "css=", "text=", "id=", "data-testid=", "role=", "internal:")

# Read by the reporting side so a sentence does not have to re-derive the vocabulary.
KINDS = ("class", "id", "testid", "attribute", "xpath")


def classify_selector(selector: str) -> str:
    """One selector string -> 'class', 'id', 'testid', 'attribute', 'xpath' or 'generic'.

    Precedence runs most-specific first: a selector mixing a class and a tag is reported as a
    class, because the class is the part that was recalled.
    """
    raw = (selector or "").strip()
    if not raw:
        return "generic"

    lowered = raw.lower()
    if lowered.startswith("xpath=") or raw.startswith("//") or raw.startswith(".//"):
        return "xpath"
    if lowered.startswith("id="):
        return "id"
    if lowered.startswith("data-testid="):
        return "testid"
    for engine in _ENGINES:
        if lowered.startswith(engine):
            if engine == "text=":
                return "generic"      # text was observed; it is the value, not a recalled token
            raw = raw[len(engine):]
            break

    # `:has-text('a.b')` and friends carry arbitrary quoted text that must not be read as a
    # class. Blank the quoted runs before looking for selector tokens.
    probe = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "''", raw)

    if _CLASS_RE.search(probe):
        return "class"
    if _ID_RE.search(probe):
        return "id"
    attrs = [a.lower() for a in _ATTR_RE.findall(probe)]
    if attrs:
        if any(a in ("data-test", "data-testid", "data-test-id", "testid") for a in attrs):
            return "testid"
        # `[name='...']` is the collector's own third form (D-427) and is therefore observed.
        if all(a == "name" for a in attrs):
            return "grounded"
        return "attribute"
    return "generic"


def audit_content(content: str, path: str = "") -> Dict[str, Any]:
    """Audit one generated file. Pure, so the smoke script can drive it with no server."""
    text = content if isinstance(content, str) else ""
    grounded = len(_GROUNDED_RE.findall(text))
    generic = 0
    invented: List[Dict[str, Any]] = []

    testids = list(_TESTID_RE.finditer(text))
    for match in testids:
        invented.append({
            "path": path,
            "line": text.count("\n", 0, match.start()) + 1,
            "selector": "getByTestId(...)",
            "kind": "testid",
        })

    seen = set()
    for pattern in _SELECTOR_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            if span in seen:
                continue
            seen.add(span)
            selector = match.group(2)
            kind = classify_selector(selector)
            if kind == "grounded":
                grounded += 1
            elif kind == "generic":
                generic += 1
            else:
                invented.append({
                    "path": path,
                    "line": text.count("\n", 0, match.start()) + 1,
                    "selector": selector[:120],
                    "kind": kind,
                })

    invented.sort(key=lambda row: row["line"])
    return {"grounded": grounded, "generic": generic, "invented": invented}


def audit(files) -> Dict[str, Any]:
    """Audit a whole `test_files` array.

    Returns the two counts a reader needs and the offending lines, capped. Rows that are not
    objects are ignored rather than coerced -- the orchestrator has already dropped them
    (D-392), and this must not be the thing that raises on a malformed response.
    """
    grounded = 0
    generic = 0
    invented: List[Dict[str, Any]] = []
    for entry in (files if isinstance(files, list) else []):
        if not isinstance(entry, dict):
            continue
        one = audit_content(entry.get("content") or "", str(entry.get("path") or ""))
        grounded += one["grounded"]
        generic += one["generic"]
        invented.extend(one["invented"])

    kinds: Dict[str, int] = {}
    for row in invented:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1

    return {
        "grounded": grounded,
        "generic": generic,
        "invented_count": len(invented),
        "invented": invented[:MAX_REPORTED],
        "kinds": kinds,
    }


def summary_sentence(report: Dict[str, Any]) -> str:
    """One line for a person, or '' when there is nothing to say.

    Phrased as what it costs rather than as a scolding: the reader's question is whether to
    trust a red test, and the answer is that these fail on a working application.
    """
    count = int((report or {}).get("invented_count") or 0)
    if not count:
        return ""
    grounded = int(report.get("grounded") or 0)
    kinds = report.get("kinds") or {}
    named = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
    return (
        f"{count} locator(s) in this suite name something the session never observed "
        f"({named}), against {grounded} written from what it did observe. The observation "
        "carries roles, accessible names and page text and never a class, id or test id, so "
        "these were recalled rather than seen. A recalled locator that is wrong matches "
        "nothing and fails against a working application, so treat a failure on one of these "
        "lines as a broken test before concluding the application is broken."
    )


def retry_note(report: Dict[str, Any], limit: int = 12) -> str:
    """What to hand back to the model, naming the lines rather than repeating the rule.

    The rule is already in the system prompt and was already ignored, so this quotes the
    model's own output back at it. Naming the offending selector is the part a general
    instruction cannot do.
    """
    rows = (report or {}).get("invented") or []
    if not rows:
        return ""
    listed = "\n".join(
        "- {path}:{line} -> `{selector}` ({kind})".format(**row) for row in rows[:limit]
    )
    more = len(rows) - limit
    if more > 0:
        listed += f"\n- ...and {more} more of the same kind"
    return (
        "## These locators were not in anything you were shown\n"
        "The suite you just wrote addresses elements by tokens this session never carried. "
        "You were given roles, accessible names, page text, and -- where neither of those "
        "resolves -- an explicit `locator(\"input[name='...']\")`. You were never given a "
        "class name, an id or a test id, so each of these came from memory of some other "
        "application:\n\n"
        f"{listed}\n\n"
        "Write the suite again. Keep the tests, the assertions and the coverage you already "
        "have -- this is not a request for a smaller suite -- and replace each of these with "
        "the way the record actually addresses that element, or with the page text the "
        "record shows for the value being checked. Where the record gives you no way to "
        "reach something, drop that assertion and say so in not_automated rather than "
        "guessing a selector. A locator that matches nothing fails against a working "
        "application, which is the one outcome that makes a suite worthless."
    )


__all__ = [
    "KINDS",
    "MAX_REPORTED",
    "audit",
    "audit_content",
    "classify_selector",
    "retry_note",
    "summary_sentence",
]
