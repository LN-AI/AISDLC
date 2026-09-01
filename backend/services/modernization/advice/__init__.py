"""Suggested charter answers, derived from what the parsers already saw.

**The problem this solves.** The charter is the one screen a human must fill in before a run can
start, and it used to present five empty required boxes whose only guidance was a placeholder.
Meanwhile the run had already been parsed for free: we knew it used `mysqli`, that six of its
seven files emitted HTML, that it called `session_start`, that its `.htaccess` rewrote
`/products/([0-9]+)`, and that a database password had been redacted out of `lib/db.php:2`. All
of that answers charter questions, and all of it was being thrown away.

**Deterministic, never the model.** Three reasons, in order of how much they cost to get wrong:

1. The charter is filled in *before* the discovery gate — `set_charter` is what raises it — so a
   model call here would be the run's first spend, with no approval shown and the screen above
   it promising that ingest and analysis cost nothing.
2. A suggestion that changes between two runs of the same codebase is not a recommendation, it
   is a coin flip with a citation.
3. D-04 already says dependency edges come from parsers rather than the model, for exactly this
   reason. Evidence is the same kind of claim.

So there is no agent in this package and no `llm_gateway` import, and the smoke suite asserts the
call log is empty across this whole path.

**Advisories are advice.** Every option is editable free text in the UI, and `charter.validate()`
neither knows nor cares whether a value came from here. Nothing in this package can make a run
illegal, which is why it is allowed to guess.

Registered per language pair, keyed the same way playbooks are — `playbooks.lookup` is shared
rather than reimplemented, so the two registries cannot disagree about what
'.NET 8 / ASP.NET Core MVC' matches. An unrecognised pair falls back to `generic`, which is
stack-neutral: a COBOL-to-Python run gets useful, non-.NET suggestions rather than a blank form.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from backend.services.modernization import charter as charter_module
from backend.services.modernization import evidence as evidence_module
from backend.services.modernization import playbooks
from backend.services.modernization import validation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Option:
    """One suggested answer.

    `why` is the whole point. A recommendation without its evidence is just a default, and a
    default the user cannot audit is worse than an empty box — they will accept it without
    reading it. `why` cites what was observed and where.

    It must never contain a redacted value. Everything here can end up on a screen, in an
    export report and in the model's prompt, and the redaction map deliberately records path,
    line and category but not the secret itself; a `why` string that quoted the password would
    put it back. The smoke suite checks this against a fixture with a real-looking one.
    """
    value: str
    label: str
    why: str = ""
    recommended: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "label": self.label, "why": self.why,
                "recommended": self.recommended}


# A rule turns observations into options for one field. Returning `[]` means "nothing useful to
# say about this field for this codebase", which is a legitimate answer and leaves the field to
# the generic advisor.
Rule = Callable[[Dict[str, Any]], List[Option]]


@dataclass(frozen=True)
class Advisor:
    source: str                                  # language label, matching `analyzers` output
    target: str                                  # normalised target-stack fragment
    rules: Dict[str, Rule] = field(default_factory=dict)


_REGISTRY: Dict[Tuple[str, str], Advisor] = {}


def register(advisor: Advisor) -> None:
    _REGISTRY[(advisor.source.lower(), advisor.target.lower())] = advisor


def find(source_language: str, target_stack: str) -> Optional[Advisor]:
    return playbooks.lookup(_REGISTRY, source_language, target_stack)


# ── Observation ────────────────────────────────────────────────────────────

# Evidence that puts a file at one end of a layered stack. Nothing here is language-specific:
# "answers a URL" and "talks to a datastore" are facts every analyzer in this package produces.
_TOP_EVIDENCE = {"route", "entrypoint", "output"}
_BOTTOM_EVIDENCE = {"datastore", "table"}

# Web -> Application -> Domain, with Infrastructure below Application. A dependency from a
# lower rank to a higher one is the one a layered split forbids.
_LAYER_RANK = {"web": 0, "application": 1, "infrastructure": 2, "domain": 3}

# Above this share of the dependency graph, a layered split is buying an interface bridge for
# every wrong-way edge and the recommendation flips. Calibrated on two real codebases — 0% and
# 15% — so the threshold is a floor between two measurements rather than a considered value,
# and it is chosen low on purpose: the plan gate now compares both layouts on *measured* gap
# counts, so this only has to pick a sensible starting point rather than be right.
LAYER_FRICTION_PERCENT = 5


def layer_friction(units: Sequence[Dict[str, Any]],
                   evidence_rows: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """How hard this codebase's own dependencies fight a layered split. Never raises.

    -> `{wrong_way, edges, percent, examples, in_cycle, depth}`.

    The file count was the only signal the layout recommendation had, and on the two real
    codebases this product has converted it pointed the **wrong way both times**: seven files
    with nothing resisting layers, and forty-five with 15% of the dependency graph resisting
    them. The second produced two failed conversions.

    Everything here is already in hand when the charter is answered: the dependency graph came
    from the analyzers at ingest, and the evidence rows say which files answer URLs and which
    touch a datastore. Presume a layer from the evidence, project the real graph onto it, and
    count the edges pointing the wrong way — each is a dependency a layered split has no legal
    way to express, needing an interface bridge.

    **It is a propensity, not a prediction**, and the count is soft: the architect may assign
    files differently, a page-script that is markup and a query at once belongs to two layers,
    and the number moved from 24 to 16 on one codebase merely by presuming a domain layer for
    leaf types. What survives that is the comparison — 0% against 15% — which is what this is
    used for.
    """
    try:
        by_path = {u.get("path"): u for u in units or [] if u.get("path")}
        if not by_path:
            return {"wrong_way": 0, "edges": 0, "percent": 0, "examples": [],
                    "in_cycle": 0, "depth": 0}
        kinds: Dict[str, set] = {}
        for row in evidence_rows or ():
            path = row.get("path")
            if path in by_path:
                kinds.setdefault(path, set()).add(row.get("kind") or "")

        layers: Dict[str, str] = {}
        for path, unit in by_path.items():
            seen = kinds.get(path) or set()
            if seen & _TOP_EVIDENCE:
                layers[path] = "web"
            elif seen & _BOTTOM_EVIDENCE:
                layers[path] = "infrastructure"
            elif not (unit.get("depends_on") or []):
                # Depends on nothing and shows no evidence at either end: a plain type. Plain
                # types are what everything else is built on, so they sit at the bottom rather
                # than in the middle, and presuming otherwise inflated the count by half.
                layers[path] = "domain"
            else:
                layers[path] = "application"

        edges = [(path, dep) for path, unit in by_path.items()
                 for dep in (unit.get("depends_on") or []) if dep in by_path]
        wrong = [(a, b) for a, b in edges
                 if _LAYER_RANK[layers[b]] < _LAYER_RANK[layers[a]]]
        return {
            "wrong_way": len(wrong),
            "edges": len(edges),
            "percent": (100 * len(wrong) // len(edges)) if edges else 0,
            "examples": [f"{a} -> {b}" for a, b in sorted(wrong)[:6]],
            "in_cycle": sum(1 for u in by_path.values() if u.get("scc_group") is not None),
            "depth": max((int(u.get("topo_level") or 0) for u in by_path.values()), default=0) + 1,
        }
    except Exception:  # noqa: BLE001
        # A signal that can fail the charter it advises is worse than no signal (D-101). An
        # empty measurement leaves every rule below reading exactly as it did before.
        logger.exception("Could not measure layer friction (continuing without it)")
        return {"wrong_way": 0, "edges": 0, "percent": 0, "examples": [],
                "in_cycle": 0, "depth": 0}


def observe(units: Sequence[Dict[str, Any]],
            evidence_rows: Sequence[Dict[str, Any]] = (),
            redactions: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """What we know about this codebase, flattened into one dict.

    Built from things that already exist — `playbooks.aggregate_facts` for per-file language
    facts and `evidence.summarize` for the cited rows — rather than re-parsing anything. This
    is also the shape `charter.required_for()` and every rule below read, so there is exactly
    one vocabulary for "what did we see".
    """
    facts = playbooks.aggregate_facts(units or [])
    summary = evidence_module.summarize(evidence_rows or [])
    dominant = _dominant_language(units or [])

    observed: Dict[str, Any] = {
        "languages": facts.get("languages") or [],
        "dominant_language": dominant,
        "file_count": len(units or []),
        # How much this codebase's own dependency graph resists being cut into layers. The
        # file count alone pointed the wrong way on both real codebases converted so far.
        "layer_friction": layer_friction(units or [], evidence_rows or []),
        # Language facts (currently populated by the PHP analyzer; empty elsewhere, which the
        # rules all tolerate).
        "db_apis": facts.get("db_apis") or [],
        "superglobals": facts.get("superglobals") or [],
        "hazard_kinds": facts.get("hazard_kinds") or [],
        "tables": facts.get("tables") or [],
        "has_ui": bool(facts.get("html")),
        "sessions": bool(facts.get("sessions")),
        "uploads": bool(facts.get("uploads")),
        "redirects": bool(facts.get("redirects")),
        "json": bool(facts.get("json")),
        # Evidence, which every language produces.
        "frameworks": summary.get("framework") or [],
        "entrypoints": summary.get("entrypoint") or [],
        "routes": summary.get("route") or [],
        "datastores": summary.get("datastore") or [],
        "integrations": summary.get("integration") or [],
        "config_keys": summary.get("config_key") or [],
        "hazards": summary.get("hazard") or [],
        "dispatch": summary.get("dispatch") or [],
    }
    # Redactions are the strongest possible evidence that a value has to become configuration:
    # it was a literal in the source. Categories only — never the value, which is not stored.
    observed["redaction_categories"] = sorted(
        {str(r.get("category") or "") for r in (redactions or [])} - {""})
    if observed["redaction_categories"] and not observed["config_keys"]:
        observed["config_keys"] = list(observed["redaction_categories"])
    # `tables` from language facts is often empty where evidence found some, and vice versa.
    observed["tables"] = sorted(set(observed["tables"]) | set(summary.get("table") or []))
    return observed


def blocking_hazards(observed: Dict[str, Any]) -> List[str]:
    """Only the hazards that actually refuse to convert.

    Two different things arrive under similar names and conflating them is a real bug, not a
    cosmetic one. `hazard_kinds` (from the language facts) and the evidence `hazard` rows both
    include things like `sql_string_building` — an injection *finding*, whose correct treatment
    is to become a parameterised query. `validation.BLOCKING_HAZARDS` is the much smaller set
    with no safe mechanical equivalent at all (`eval`, variable variables, dynamic includes,
    `extract`, `unserialize` on request data, shell-out), and only those may be left
    unimplemented behind a `MIGRATION-BLOCKED` marker.

    Without this filter the charter would tell the converter to refuse every interpolated query
    in the codebase — which for legacy PHP is very nearly every data-access line — and D-55
    already records why demanding a marker for a hazard *with* a correct equivalent fails honest
    conversions. Filtering against validation's own constant rather than a second hand-written
    list is what stops the two drifting.
    """
    seen = set(observed.get("hazard_kinds") or []) | set(observed.get("hazards") or [])
    return sorted(seen & set(validation.BLOCKING_HAZARDS))


def _dominant_language(units: Sequence[Dict[str, Any]]) -> str:
    """The language most of the codebase is written in, which is the one an advisor is chosen
    for. A PHP app with three JavaScript files is a PHP conversion."""
    counts: Dict[str, int] = {}
    for unit in units:
        language = (unit.get("language") or "").strip().lower()
        if language and language != "unknown":
            counts[language] = counts.get(language, 0) + 1
    if not counts:
        return ""
    # Count first, then name, so a tie resolves the same way every time.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# ── Suggestion ─────────────────────────────────────────────────────────────

def suggest(target_stack: str, observed: Dict[str, Any]) -> Dict[str, List[Option]]:
    """Options per charter field, most-recommended first.

    A pair-specific advisor answers what it can and the generic one covers the rest, so every
    field gets something. Deterministic: same `observed` in, byte-identical options out —
    `sorted()` on nothing, no dict iteration order that isn't already fixed, no clock.
    """
    # Rules see the target stack alongside the observations. `target_stack` is itself a charter
    # field, and the only sensible thing to say about it is grounded in what the user already
    # typed when the run was created — so the rules need it, and threading it through `observed`
    # keeps every rule's signature to one argument. Copied, not mutated: callers pass the dict
    # they got from `observe()` and it is also what `required_for` reads.
    observed = {**(observed or {}), "target_stack": target_stack or ""}
    advisor = find(observed.get("dominant_language") or "", target_stack)
    out: Dict[str, List[Option]] = {}
    for key in charter_module.ALL_KEYS:
        options = _apply(advisor, key, observed) if advisor else []
        if not options:
            options = _apply(GENERIC, key, observed)
        if options:
            out[key] = _rank(options)
    return out


def _apply(advisor: Optional[Advisor], key: str, observed: Dict[str, Any]) -> List[Option]:
    rule = (advisor.rules if advisor else {}).get(key)
    if rule is None:
        return []
    try:
        return list(rule(observed) or [])
    except Exception:
        # One broken rule must not cost the form every other suggestion on it.
        logger.exception("Charter advice rule failed for field %s", key)
        return []


def _rank(options: Sequence[Option]) -> List[Option]:
    """Recommended first, otherwise source order.

    Deliberately a stable sort on one boolean: rules list their options in a meaningful order
    (most conservative first, usually), and re-sorting by anything else would discard it.
    """
    return sorted(options, key=lambda o: not o.recommended)


def emphasis_for(key: str, observed: Dict[str, Any], required: Sequence[str]) -> str:
    """`required` | `recommended` | `optional` — how loudly the form should ask.

    This is where evidence is allowed to act. It cannot make a field required (see
    `charter.required_for` for why that would break API callers), but it can say "your codebase
    has a UI, so this optional question is one you should answer", which is the thing that was
    actually missing: `ui_strategy` was optional on a codebase that was six-sevenths UI.
    """
    if key in required:
        return "required"
    if key in _RELEVANT_WHEN and _RELEVANT_WHEN[key](observed or {}):
        return "recommended"
    return "optional"


# Optional fields that this particular codebase makes worth answering. Keyed on observations,
# never on the target stack — "you have a UI" is true regardless of what it is converted to.
_RELEVANT_WHEN: Dict[str, Callable[[Dict[str, Any]], bool]] = {
    "ui_strategy": lambda o: bool(o.get("has_ui")),
    "auth_model": lambda o: bool(o.get("sessions")),
    "url_compatibility": lambda o: bool(o.get("routes") or o.get("entrypoints")),
    "datastore_fate": lambda o: bool(o.get("db_apis") or o.get("datastores") or o.get("tables")),
    # Only the blocking set. `sql_string_building` is a finding to be fixed, not a construct to
    # refuse — see `blocking_hazards`.
    "constraints": lambda o: bool(blocking_hazards(o)),
    "concurrency_model": lambda o: bool(o.get("db_apis") or o.get("integrations")),
}


def describe(target_stack: str, observed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The suggestions payload: one entry per charter field, in charter order.

    Charter order, not suggestion order, so the form reads the same way every time and matches
    the order the charter itself renders in.
    """
    observed = observed or {}
    options = suggest(target_stack, observed)
    # What `charter.validate()` will actually enforce — the static list, deliberately, for the
    # reason given in its docstring. This used to be `required_for(observed)`, which is
    # *subtractive*: on a codebase with no database it dropped `data_access` from the form's
    # required set, so the field rendered inside the collapsed "optional" fold with no
    # suggested value — and then the save was refused with "…before any file is converted:
    # Data access". The form hid a question the server went on to demand. A form may ask for
    # more than is enforced; it must never ask for less.
    required = list(charter_module.REQUIRED_KEYS)
    out = []
    for f in charter_module.CHARTER_FIELDS:
        out.append({
            "key": f.key,
            "label": f.label,
            "help": f.help,
            "required": f.key in required,
            "emphasis": emphasis_for(f.key, observed, required),
            "options": [o.as_dict() for o in options.get(f.key, [])],
        })
    return out


# `generic` is imported for its side effect of defining GENERIC; the pair-specific advisors
# register themselves. Imported at the bottom because both import this module for `Option`.
from backend.services.modernization.advice.generic import GENERIC  # noqa: E402
from backend.services.modernization.advice import php_dotnet  # noqa: E402,F401
