"""The charter — the architectural decisions that bind every file in a run.

The single defect that makes `migration-execute` unusable as a modernization tool is that
`migration_target` is one free-text string. Every per-file call re-guesses the
architecture from it, so file 1 gets EF Core, file 40 gets Dapper and file 90 gets raw
ADO.NET — and nothing in the system can notice, because nothing compares file 1 to file 40.

The charter replaces that guess with an answer, decided once, by a human, before any
conversion is billed. It is:

* **Non-skippable.** `validate()` refuses a run whose charter is missing a required
  decision. A run cannot leave `awaiting_charter` without one.
* **Byte-identical across every call in the run.** `render()` output goes into the *system*
  prompt, which is where the prompt-cache breakpoint sits (D-06). On a 500-file run that
  turns the charter from a per-call cost into a single cache write plus 499 reads.
* **Frozen once conversion starts.** Changing it mid-run would mean file 200 was converted
  against different rules than file 20, which is the exact inconsistency it exists to
  prevent. Editing it requires the run to be paused, and every already-converted unit is
  flagged for review.
"""
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    """One charter decision.

    `help` states the *decision*, never an example, and deliberately names no framework: this
    same list is served to a COBOL-to-Java run and a PHP-to-.NET one. Concrete examples come
    from `advice`, which is keyed by language pair and can therefore be specific without being
    wrong. (An earlier version wrote '.NET 8', 'EF Core 8' and 'Blazor' into this text, which
    read as boilerplate written for somebody else on every other conversion.)
    """
    key: str
    label: str
    required: bool
    help: str
    # Whether this decision is meaningful for a codebase with these observed characteristics.
    # `None` = always. Consulted only by `required_for()`, and only to *remove* a field from
    # the required set — never to add one. See that function for why.
    applies_when: Optional[Callable[[Dict[str, Any]], bool]] = None


def _has_data(observed: Dict[str, Any]) -> bool:
    return bool(observed.get("db_apis") or observed.get("datastores")
                or observed.get("tables"))


def _has_config(observed: Dict[str, Any]) -> bool:
    # A codebase that reaches a database or an external host, or that had a literal redacted,
    # has configuration whether or not it currently keeps it in a config file. One that does
    # none of those things genuinely may not.
    return bool(observed.get("config_keys") or observed.get("integrations")
                or observed.get("datastores") or observed.get("db_apis"))


# The required ones are required because leaving any of them to the model's discretion is what
# produces a codebase that disagrees with itself.
#
# Order is load-bearing: `render()` walks this list, and its output must be byte-identical
# across every call in a run for the prompt cache to hit. New fields are therefore **appended**,
# never inserted — appending cannot change the string a charter stored before they existed
# renders to, because `render()` skips empty values.
CHARTER_FIELDS: List[Field] = [
    Field("target_stack", "Target stack", True,
          "The stack being converted to, concretely — language, framework and major version."),
    Field("project_layout", "Project layout", True,
          "How the output is divided into projects, modules or packages, and what belongs in "
          "each one."),
    Field("data_access", "Data access", True,
          "One way of reaching the database, applied everywhere. This is the decision that "
          "most often ends up different in different files.",
          applies_when=_has_data),
    # No `applies_when`: every codebase has failures, so there is no evidence that could make
    # this decision irrelevant.
    Field("error_handling", "Error handling", True,
          "How failures are represented, and where they are caught and turned into a response."),
    Field("config_strategy", "Configuration & secrets", True,
          "Where configuration and secrets come from at runtime. Every literal redacted at "
          "ingest has to become a configuration key here.",
          applies_when=_has_config),
    Field("ui_strategy", "UI / presentation", False,
          "How the source's user interface becomes the target's, and how much of its structure "
          "is preserved. Only relevant if the source renders one."),
    Field("composition", "Composition & wiring", False,
          "How a piece of code gets hold of the things it depends on — the wiring convention, "
          "whether that is a container, constructor arguments, module imports or plain "
          "parameters."),
    Field("logging", "Logging", False,
          "How the code emits diagnostics, and what is not allowed to be used for it."),
    Field("testing", "Testing", False,
          "What happens to the tests that already exist, and whether new ones are written."),
    Field("naming_conventions", "Naming conventions", False,
          "Casing and naming rules, and whether original domain terms are kept even where they "
          "do not match the target's conventions."),
    Field("preserve_behaviour", "Behaviour preservation", False,
          "What may and may not change. Default is: nothing changes except what the target "
          "stack forces."),
    Field("constraints", "Other constraints", False,
          "Anything else binding — framework version floors, banned packages, compliance rules."),

    # ── Appended later. Each one binds every file, and before they existed the model
    # re-decided them per file with nothing comparing the answers.
    Field("target_language", "Target language", False,
          "The language itself, where the target stack supports more than one and the choice "
          "is not already obvious from the stack."),
    Field("datastore_fate", "Database & schema", False,
          "Whether the same database engine is kept or replaced, and whether the existing "
          "schema is reused as it stands or redesigned. Binding for every file that touches "
          "data.",
          applies_when=_has_data),
    Field("concurrency_model", "Concurrency & I/O", False,
          "Whether operations are asynchronous or synchronous. In most target stacks this "
          "decides the signature of nearly every function, so it cannot be settled per file."),
    Field("url_compatibility", "URL / API compatibility", False,
          "Whether the URLs and API paths the source answers on have to keep working "
          "unchanged, or whether callers can be moved to new ones."),
    Field("auth_model", "Authentication & sessions", False,
          "How a user is authenticated, and how per-user state is carried between requests."),
]

REQUIRED_KEYS = [f.key for f in CHARTER_FIELDS if f.required]
ALL_KEYS = [f.key for f in CHARTER_FIELDS]
FIELDS_BY_KEY = {f.key: f for f in CHARTER_FIELDS}

# Keys this module used to use, mapped to what replaced them. `dependency_injection` was
# renamed because dependency injection is not a stack-neutral concept — it is an OO/container
# idea that means nothing for a Go, Python or Elixir target — but a charter stored before the
# rename still carries the old key, and `render()` walks `ALL_KEYS`, so without this the value
# a human typed would silently vanish from the system prompt of an already-paused run.
LEGACY_KEYS = {"dependency_injection": "composition"}

DEFAULT_BEHAVIOUR = (
    "Preserve existing behaviour exactly. This is a migration, not a rewrite: do not add "
    "features, do not 'improve' logic that the target stack does not force you to change, "
    "and do not rename domain concepts."
)


class CharterIncomplete(Exception):
    def __init__(self, missing: List[str]):
        self.missing = missing
        labels = {f.key: f.label for f in CHARTER_FIELDS}
        super().__init__(
            "The conversion charter is incomplete. These decisions have to be made before "
            "any file is converted, because every file is converted against them: "
            + ", ".join(labels.get(m, m) for m in missing)
            + "."
        )


def required_for(observed: Dict[str, Any]) -> List[str]:
    """Which decisions this particular codebase actually needs, given what the parsers saw.

    **Subtractive only, by design.** This can drop a field from the required set — a batch
    program with no database should not be made to answer 'Data access' — but it can never add
    one. `/api/v1/modernization` is a public surface: a caller POSTing a charter that
    `validate()` accepts today must keep working, and evidence-driven tightening would turn
    their next request into a 400 for a reason they never asked about. Where evidence says a
    decision matters, the UI raises its *emphasis* instead (see `advice`).

    `observed` is the shape `advice.observe()` returns; an empty dict means 'we know nothing',
    which correctly yields the full static list.
    """
    observed = observed or {}
    out = []
    for key in REQUIRED_KEYS:
        field = FIELDS_BY_KEY[key]
        if field.applies_when is not None and observed:
            try:
                if not field.applies_when(observed):
                    continue
            except Exception:
                logger.exception("applies_when failed for charter field %s", key)
                # Keep it required. A predicate that raised has told us nothing, and dropping
                # a required decision on the strength of a bug is the worse failure.
        out.append(key)
    return out


def canonical(charter: Dict[str, Any]) -> Dict[str, Any]:
    """A charter with renamed keys folded onto their current names.

    Applied by both `validate()` and `render()` so a stored charter written before a rename
    keeps rendering the value a human typed, whichever path reaches it.
    """
    charter = dict(charter or {})
    for old, new in LEGACY_KEYS.items():
        if charter.get(old) and not charter.get(new):
            charter[new] = charter[old]
    return charter


def validate(charter: Dict[str, Any]) -> Dict[str, str]:
    """Return a cleaned charter, or raise `CharterIncomplete`.

    Unknown keys are dropped rather than passed through: the charter goes verbatim into
    every system prompt in the run, so it is not a place to let arbitrary caller-supplied
    text accumulate.

    Required-ness here is the static list, not `required_for()`. A caller that reaches this
    function may have no run and therefore no evidence, and the answer to 'is this charter
    acceptable' must not depend on which of two callers asked.
    """
    charter = canonical(charter)
    cleaned = {
        key: str(charter.get(key) or "").strip()
        for key in ALL_KEYS
    }
    missing = [key for key in REQUIRED_KEYS if not cleaned[key]]
    if missing:
        raise CharterIncomplete(missing)
    if not cleaned["preserve_behaviour"]:
        cleaned["preserve_behaviour"] = DEFAULT_BEHAVIOUR
    return cleaned


def render(charter: Dict[str, Any]) -> str:
    """The charter as it appears in the system prompt.

    Ordering is fixed and comes from `CHARTER_FIELDS`, not from dict order — the string has
    to be byte-identical across every call in the run for the prompt cache to hit, and a
    dict that was rebuilt from JSON in a different order would silently cost full price on
    every file.
    """
    charter = canonical(charter)
    labels = {f.key: f.label for f in CHARTER_FIELDS}
    lines = [
        "## Conversion Charter — binding for every file in this run",
        "",
        "These decisions were made once, by the team that owns this codebase, and apply to "
        "every file. They are not suggestions and they are not yours to revisit: a file "
        "converted against different rules than the file next to it is the failure this "
        "charter exists to prevent. If the charter does not cover something, follow the "
        "target stack's idiom and note the gap in `manual_followup` — do not invent a "
        "second convention.",
        "",
    ]
    for key in ALL_KEYS:
        value = str((charter or {}).get(key) or "").strip()
        if value:
            lines.append(f"- **{labels[key]}**: {value}")
    return "\n".join(lines)


def describe_fields() -> List[Dict[str, Any]]:
    """The form definition, served to the UI so the charter screen cannot drift from what
    `validate()` actually enforces.

    Run-independent on purpose: this is the public schema, and the frontend caches it across
    runs. Anything that depends on a specific codebase belongs on the per-run suggestions
    endpoint instead, or one run's answers would be served on another run's form.
    """
    return [
        {"key": f.key, "label": f.label, "required": f.required, "help": f.help,
         # Whether this decision can be dropped for a codebase that plainly does not have the
         # thing it is about. Exposed so a caller can tell 'always required' from 'required
         # unless your codebase has no database' without reading our source.
         "conditional": f.applies_when is not None}
        for f in CHARTER_FIELDS
    ]


# ── The charter, read against the codebase it binds ────────────────────────
#
# `validate()` enforces that the five required decisions are present. Nothing checked whether
# the answers make sense *for this codebase*, and the charter binds every conversion call in
# the run — so a decision that does not fit is a mistake repeated hundreds of times, at full
# price, in a shape no later stage can see.
#
# All of it is arithmetic over signals the analysis already computed, so it is free and it runs
# at the assessment gate: the last moment before anything is spent. Every finding is a warning.
# The customer may have a reason, and refusing a charter on a heuristic would be worse than the
# problem — the point is to put the mismatch in front of someone who can recognise it.

# Wording that indicates a promise to keep the source's URLs working unchanged.
_KEEPS_URLS = ("unchanged", "same url", "same path", "preserve", "keep", "compatib", "identical")


def _stated(charter: Dict[str, Any], key: str) -> str:
    return str((charter or {}).get(key) or "").strip()


def _has_data(signals: Dict[str, Any]) -> bool:
    return bool(signals.get("db_apis") or signals.get("datastores") or signals.get("tables"))


def lint(charter: Dict[str, Any], signals: Dict[str, Any]) -> List[Dict[str, str]]:
    """-> [{key, title, detail}]. Deterministic, free, and never raises on odd input.

    Two shapes of finding, and both matter:

      * a decision left blank that this codebase plainly needs — it will then be re-made,
        differently, by every call that runs into it;
      * a decision that was made about something the codebase does not appear to contain,
        which usually means the analysis missed that area rather than that the answer is wrong.
    """
    charter, signals = charter or {}, signals or {}
    findings: List[Dict[str, str]] = []
    data = _has_data(signals)

    if data and not _stated(charter, "datastore_fate"):
        findings.append({
            "key": "datastore_fate",
            "title": "The database's fate is unstated, and this codebase has one",
            "detail": (
                f"{len(signals.get('tables') or [])} table(s) and "
                f"{len(signals.get('db_apis') or [])} data-access API(s) were observed, but "
                f"nothing says whether the same engine is kept or replaced, or whether the "
                f"existing schema is reused as it stands. Every file that touches data has to "
                f"assume one, and without this they will not all assume the same one."),
        })

    if data and not _stated(charter, "concurrency_model"):
        findings.append({
            "key": "concurrency_model",
            "title": "Asynchronous or synchronous is unstated for a codebase that does I/O",
            "detail": (
                "In most target stacks this decides the signature of nearly every function "
                "that touches the database, so it cannot be settled per file — and left blank "
                "it will be, one call at a time."),
        })

    if _stated(charter, "data_access") and not data:
        findings.append({
            "key": "data_access",
            "title": "A data-access decision was made, and no data access was found",
            "detail": (
                "Nothing in this codebase parsed as a database call, a table or a datastore. "
                "Either this decision binds nothing, or the analysis could not see the data "
                "layer — and the second is worth checking before converting, because every "
                "file that reaches the database would then be converted with no guidance."),
        })

    routes = int(signals.get("routes") or 0)
    if routes and not _stated(charter, "url_compatibility"):
        findings.append({
            "key": "url_compatibility",
            "title": f"{routes} route(s) were found, and nothing says whether they must keep working",
            "detail": (
                "Whether callers can be moved to new paths, or the source's URLs have to answer "
                "unchanged, changes how nearly every entry point is converted."),
        })

    keeps_urls = any(w in _stated(charter, "url_compatibility").lower() for w in _KEEPS_URLS)
    if keeps_urls and not routes:
        findings.append({
            "key": "url_compatibility",
            "title": "The URLs are promised to keep working, and no routes were found",
            "detail": (
                "No route was observed anywhere in this codebase, so there is nothing to hold "
                "the conversion to and nothing to check it against afterwards. That usually "
                "means the routing is done in a way the analysis could not read."),
        })

    if signals.get("has_ui") and not _stated(charter, "ui_strategy"):
        findings.append({
            "key": "ui_strategy",
            "title": "This codebase renders a user interface, and nothing says what it becomes",
            "detail": (
                "How much of the existing screen structure survives is a decision every view "
                "conversion needs, and it is the one most likely to be answered differently "
                "by each of them."),
        })

    # Evidence first, the analyzer's flag second. `sessions` is a PHP-specific observation; an
    # `auth` evidence row is the language-neutral fact, and a codebase that checks a password
    # without ever calling `session_start` still has an authentication model to decide.
    auth_sites = int(((signals.get("evidence_counts") or {}).get("auth")) or 0)
    if (signals.get("sessions") or auth_sites) and not _stated(charter, "auth_model"):
        findings.append({
            "key": "auth_model",
            "title": "Per-user state was observed, and the authentication model is unstated",
            "detail": (
                "Authentication or per-user state was found in the source. How a user is "
                "authenticated, and how that state is carried between requests, binds every "
                "entry point that reads it, and it is the decision most expensive to get "
                "wrong: rehashing a password locks out every existing user."),
        })

    return findings


def describe_lint(findings: List[Dict[str, str]]) -> str:
    """The findings as gate prose. Empty when there is nothing to say."""
    if not findings:
        return ""
    one = len(findings) == 1
    head = ("\n\nOne decision in this charter does not fit what was found in the codebase. It "
            "does not block the run — you may have a reason — but the charter binds every "
            "file, so a mismatch here is repeated in every conversion call:"
            if one else
            f"\n\n{len(findings)} decisions in this charter do not fit what was found in the "
            f"codebase. They do not block the run — you may have reasons — but the charter "
            f"binds every file, so a mismatch here is repeated in every conversion call:")
    return head + "\n" + "\n".join(f"  · {f['title']}. {f['detail']}" for f in findings)
