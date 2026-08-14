"""Stack-neutral charter suggestions — the fallback for any language pair with no advisor.

This file is the answer to "are the charter questions general enough?". They are, but the
*answers* used to be written for .NET, in help text every conversion saw. So the neutral
guidance lives here, phrased so that a COBOL-to-Java or a VB6-to-Python run reads something
written for it: no framework names, no package names, no idiom that belongs to one runtime.

Two rules for anything added here:

* **Name the shape of the decision, not a technology.** "One data-access layer, chosen once"
  transfers; "EF Core 8 with a repository per aggregate" does not.
* **Say what the alternatives cost.** An option list where every entry sounds equally good is a
  list the user will pick the first item from without reading. The trade-off is the content.

Where observations are available they are still used — a codebase with no UI should not be
offered a UI strategy — but nothing here depends on any *language-specific* fact, only on the
target-agnostic ones (`has_ui`, `tables`, `routes`, `config_keys`).
"""
from typing import Any, Dict, List

from backend.services.modernization.advice import Advisor, Option, blocking_hazards


def _cite(observed: Dict[str, Any], *keys: str) -> str:
    """A short 'because we saw…' clause from whichever observations are present."""
    bits = []
    for key in keys:
        value = observed.get(key)
        if isinstance(value, list) and value:
            shown = ", ".join(str(v) for v in value[:4])
            more = f" (+{len(value) - 4} more)" if len(value) > 4 else ""
            bits.append(f"{key.replace('_', ' ')}: {shown}{more}")
        elif isinstance(value, bool) and value:
            bits.append(key.replace("_", " "))
        elif isinstance(value, int) and value and key == "file_count":
            bits.append(f"{value} file(s)")
    return "; ".join(bits)


def _target_stack(observed: Dict[str, Any]) -> List[Option]:
    """The one field the user has already half-answered.

    A run is created with a target stack, so there is always something here — but it is usually
    too thin to convert against ('.NET 8' does not say whether views exist). Echoing it back
    with what is missing is more useful than an empty box, and it is the honest answer for a
    language pair we have no specific knowledge of.
    """
    stated = str(observed.get("target_stack") or "").strip()
    if not stated:
        return []
    # Three words or fewer, or no digit, and it is almost certainly missing the framework or the
    # version — the two things every converted file depends on.
    thin = len(stated.split()) <= 2 or not any(c.isdigit() for c in stated)
    why = ("Taken from the run's target stack. Naming the framework and the major version "
           "matters: it decides package versions and language features in every file, and it is "
           "the one charter answer that cannot be inferred from the source."
           if thin else
           "Taken from the run's target stack, which already names a framework and a version.")
    return [Option(value=stated, label=f"As stated: {stated}", why=why, recommended=True)]


def _project_layout(observed: Dict[str, Any]) -> List[Option]:
    small = (observed.get("file_count") or 0) <= 40
    feature = Option(
        value="A single deployable unit, divided into one folder per feature. Each feature owns "
              "its entry points, its logic and its data access.",
        label="Single unit, folder-per-feature",
        why=("Keeps related code together, which is how the source is already organised. "
             + _cite(observed, "file_count")).strip(),
        recommended=small,
    )
    layered = Option(
        value="One module per layer — entry points, application logic, domain model, "
              "infrastructure — with dependencies pointing inward only.",
        label="One module per layer",
        why="Enforces a dependency direction the source almost certainly does not have. Worth "
            "the extra structure on a large codebase; overhead on a small one.",
        recommended=not small,
    )
    return [feature, layered] if small else [layered, feature]


def _data_access(observed: Dict[str, Any]) -> List[Option]:
    if not (observed.get("tables") or observed.get("datastores") or observed.get("db_apis")):
        return []
    why = _cite(observed, "datastores", "tables")
    return [
        Option(
            value="One mapping layer over the database, used by every module. No hand-written "
                  "queries outside it, and no query text in entry-point code.",
            label="A single mapping layer",
            why=(why + ". Chosen once so two files cannot reach the same table two ways.").strip(". "),
            recommended=True,
        ),
        Option(
            value="A thin query layer that keeps the existing statements as they are, executed "
                  "through one shared helper with parameters bound rather than interpolated.",
            label="Thin query layer, statements preserved",
            why="Closest to the source, so the conversion is easier to review statement by "
                "statement — at the cost of carrying the old data model forward unchanged.",
        ),
    ]


def _error_handling(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value="Raise for genuinely exceptional conditions; return an explicit result value "
                  "for expected failures. One handler at the outer boundary turns anything "
                  "unhandled into a response. No per-function catch-and-continue.",
            label="Raise for exceptional, return for expected",
            why="The source most likely mixes the two per file; deciding once is what makes the "
                "output consistent.",
            recommended=True,
        ),
        Option(
            value="Every operation that can fail returns an explicit result value. Raising is "
                  "reserved for programmer error only.",
            label="Explicit results everywhere",
            why="Makes every failure path visible in the signature. More verbose, and it changes "
                "the shape of nearly every function.",
        ),
    ]


def _config_strategy(observed: Dict[str, Any]) -> List[Option]:
    keys = observed.get("config_keys") or []
    categories = observed.get("redaction_categories") or []
    why = "No literal credential may survive the conversion."
    if categories:
        why = (f"{len(categories)} kind(s) of secret were redacted at ingest "
               f"({', '.join(categories)}) and each one has to become a configuration key.")
    elif keys:
        why = f"{len(keys)} configuration value(s) were observed in the source."
    return [
        Option(
            value="Configuration is read once at start-up into typed objects and injected where "
                  "needed. Secrets come only from the environment or a secret store — never a "
                  "literal in source, never a default in code.",
            label="Typed config at start-up, secrets from the environment",
            why=why,
            recommended=True,
        ),
        Option(
            value="Configuration is read from environment variables at the point of use, with no "
                  "intermediate object.",
            label="Environment variables read directly",
            why="Fewer moving parts; loses the single place to validate that everything required "
                "was actually supplied.",
        ),
    ]


def _ui_strategy(observed: Dict[str, Any]) -> List[Option]:
    if not observed.get("has_ui"):
        return [Option(
            value="The source renders no user interface. Nothing to carry across.",
            label="No UI in the source",
            why="No markup or rendering was observed in any file.",
            recommended=True,
        )]
    return [
        Option(
            value="Separate rendering from logic: each page in the source becomes one handler "
                  "plus one template. Shared header/footer fragments become a single layout used "
                  "by all of them. Structure is preserved; exact markup and styling need not be.",
            label="Handler + template per page, one shared layout",
            why="The source mixes markup and logic in the same file, which is the split that has "
                "to be decided once rather than per page.",
            recommended=True,
        ),
        Option(
            value="Replace server-rendered pages with a data API plus a separate client "
                  "application.",
            label="Data API plus a separate client",
            why="A rewrite of the presentation layer rather than a conversion of it. Much larger "
                "scope, and behavioural parity becomes hard to demonstrate.",
        ),
    ]


def _composition(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value="Dependencies are passed in explicitly at construction, wired in one place at "
                  "start-up. No global state, no service lookup from inside a function, no "
                  "module-level singletons.",
            label="Passed in explicitly, wired once at start-up",
            why="Legacy code typically reaches for globals; naming the alternative once is what "
                "stops half the output doing the same.",
            recommended=True,
        ),
    ]


def _logging(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value="One structured logger, obtained the same way everywhere, with levels used "
                  "consistently. Nothing writes to standard output directly. No secret, "
                  "credential or personal identifier is ever logged.",
            label="One structured logger, no direct output",
            why="Legacy code prints for both diagnostics and user output; separating them is a "
                "decision, not a detail.",
            recommended=True,
        ),
    ]


def _testing(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value="Convert the tests that already exist and keep them passing. Do not invent new "
                  "tests during conversion — an invented test encodes the converter's guess "
                  "about intent as if it were a requirement.",
            label="Convert existing tests, invent none",
            why="Keeps the conversion reviewable: a failing test means the conversion is wrong, "
                "not that the test is new.",
            recommended=True,
        ),
        Option(
            value="Convert existing tests, and additionally record the current behaviour of each "
                  "entry point as a characterization test before changing it.",
            label="Also characterize current behaviour",
            why="The strongest correctness signal available, because it compares against what the "
                "system does rather than what anyone believes it does. Costs more up front.",
        ),
    ]


def _naming(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value="Follow the target language's conventions for casing and structure, but keep "
                  "the original domain terms exactly — including abbreviations and non-English "
                  "words. A renamed domain concept is untraceable back to the source.",
            label="Target conventions, original domain terms",
            why="Renaming during conversion is the change that makes a later review impossible.",
            recommended=True,
        ),
    ]


def _preserve(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value="Preserve existing behaviour exactly, including behaviour that looks like a "
                  "bug. Do not add features, do not improve logic the target stack does not "
                  "force you to change. Anything that cannot be preserved is reported rather "
                  "than silently altered.",
            label="Preserve exactly, including bugs",
            why="Anything else makes a behavioural difference indistinguishable from a conversion "
                "defect.",
            recommended=True,
        ),
    ]


def _constraints(observed: Dict[str, Any]) -> List[Option]:
    # Only constructs with no safe equivalent. See `advice.blocking_hazards` — the broader
    # hazard vocabulary includes findings that have a correct answer and must be fixed rather
    # than refused.
    hazards = blocking_hazards(observed)
    if not hazards:
        return []
    shown = ", ".join(str(h) for h in hazards[:6])
    return [
        Option(
            value=(f"The source contains constructs that must not be translated silently: "
                   f"{shown}. Each one is to be left unimplemented, marked for follow-up, and "
                   f"reviewed by a human — never replaced with a plausible equivalent."),
            label="Block the unsafe constructs, do not guess",
            why=(f"{len(hazards)} hazard class(es) observed. These are the constructs where a "
                 f"faithful-looking translation changes what the program can do."),
            recommended=True,
        ),
    ]


def _concurrency(observed: Dict[str, Any]) -> List[Option]:
    io = bool(observed.get("db_apis") or observed.get("datastores")
              or observed.get("integrations"))
    if not io:
        return []
    return [
        Option(
            value="Anything that performs I/O is asynchronous, all the way up to the entry "
                  "point. No blocking call inside an asynchronous path.",
            label="Asynchronous throughout",
            why="I/O was observed, and mixing the two models is what produces deadlocks that only "
                "appear under load. It changes almost every signature, so it cannot be decided "
                "per file.",
            recommended=True,
        ),
        Option(
            value="Keep everything synchronous, matching the source's execution model.",
            label="Synchronous, as the source is",
            why="Simplest and closest to the original. Fine for batch or low-concurrency work; a "
                "throughput ceiling for a request-serving application.",
        ),
    ]


def _url_compatibility(observed: Dict[str, Any]) -> List[Option]:
    routes = observed.get("routes") or []
    entries = observed.get("entrypoints") or []
    if not (routes or entries):
        return []
    n = len(routes) or len(entries)
    return [
        Option(
            value="Existing URLs keep working exactly as they are, including query-string "
                  "parameter names. New routes may be added alongside them; none may be removed "
                  "or renamed.",
            label="Preserve existing URLs exactly",
            why=f"{n} externally reachable path(s) were found. Anything bookmarked, linked or "
                f"called by another system breaks if these change.",
            recommended=True,
        ),
        Option(
            value="Redesign the routes to the target's conventions and accept that existing links "
                  "break. No compatibility layer.",
            label="Redesign routes, accept breakage",
            why="Cleaner result. Only safe if you know every caller, which for a legacy system is "
                "rarely true.",
        ),
    ]


def _datastore_fate(observed: Dict[str, Any]) -> List[Option]:
    if not (observed.get("tables") or observed.get("datastores") or observed.get("db_apis")):
        return []
    tables = observed.get("tables") or []
    why = f"{len(tables)} table(s) observed" if tables else "a database was observed"
    return [
        Option(
            value="Keep the current database engine and the current schema exactly as it is. The "
                  "converted application maps onto the existing tables; no data has to move.",
            label="Same engine, same schema",
            why=f"{why}. Keeping the schema means the conversion can be validated against real "
                f"data, and rolled back without a migration.",
            recommended=True,
        ),
        Option(
            value="Keep the schema, but move to a different database engine. Types and reserved "
                  "words are translated; table and column names are not.",
            label="New engine, same schema",
            why="Sensible when the target platform pushes you elsewhere. Adds a data migration "
                "and a second correctness question on top of the conversion.",
        ),
        Option(
            value="Redesign the schema alongside the conversion.",
            label="Redesign the schema",
            why="Two changes at once: every defect afterwards could be either. Strongly advised "
                "against during a conversion.",
        ),
    ]


def _auth_model(observed: Dict[str, Any]) -> List[Option]:
    if not observed.get("sessions"):
        return []
    return [
        Option(
            value="Server-side sessions behind a signed, http-only cookie, matching the source's "
                  "model. Per-user state that the source kept in the session becomes explicit "
                  "typed state, not an untyped bag.",
            label="Server-side sessions, as the source has",
            why="Session usage was observed. Keeping the model means the conversion does not also "
                "have to re-prove the login flow.",
            recommended=True,
        ),
        Option(
            value="Stateless tokens carried by the client, with no server-side session.",
            label="Stateless tokens",
            why="Scales better and suits an API. But anything the source stored in the session "
                "has to move somewhere, and sign-out stops being immediate.",
        ),
    ]


def _target_language(observed: Dict[str, Any]) -> List[Option]:
    return []          # Nothing neutral to say; a pair-specific advisor can answer this.


GENERIC = Advisor(
    source="*", target="*",
    rules={
        "target_stack": _target_stack,
        "project_layout": _project_layout,
        "data_access": _data_access,
        "error_handling": _error_handling,
        "config_strategy": _config_strategy,
        "ui_strategy": _ui_strategy,
        "composition": _composition,
        "logging": _logging,
        "testing": _testing,
        "naming_conventions": _naming,
        "preserve_behaviour": _preserve,
        "constraints": _constraints,
        "concurrency_model": _concurrency,
        "url_compatibility": _url_compatibility,
        "datastore_fate": _datastore_fate,
        "auth_model": _auth_model,
        "target_language": _target_language,
    },
)

# Deliberately **not** registered: `find()` matches a concrete language pair, and a wildcard in
# the registry would shadow real advisors depending on dict order. `suggest()` reaches for
# GENERIC explicitly, per field, when the pair-specific advisor has nothing to say.
