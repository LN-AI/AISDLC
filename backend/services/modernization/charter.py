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
from typing import Any, Dict, List, Tuple

# (key, label, required, help). The required ones are required because leaving any of them
# to the model's discretion is what produces a codebase that disagrees with itself.
CHARTER_FIELDS: List[Tuple[str, str, bool, str]] = [
    ("target_stack", "Target stack", True,
     "The stack being converted to, concretely — e.g. '.NET 8 / ASP.NET Core Web API'."),
    ("project_layout", "Project layout", True,
     "How the output is organised — e.g. 'one project per layer: Api, Application, Domain, "
     "Infrastructure' or 'single project, folder-per-feature'."),
    ("data_access", "Data access", True,
     "One choice, applied everywhere — e.g. 'EF Core 8 with a repository per aggregate'. "
     "This is the decision that most often ends up different in different files."),
    ("error_handling", "Error handling", True,
     "e.g. 'exceptions for exceptional cases, Result<T> for expected failures; one global "
     "exception middleware, never a try/catch per method'."),
    ("config_strategy", "Configuration & secrets", True,
     "e.g. 'IOptions<T> bound from appsettings; secrets only from environment variables — "
     "never a literal in code'. Source values were redacted at ingest, so every one of "
     "them becomes a config key here."),
    ("ui_strategy", "UI / presentation", False,
     "Only if the source has UI — e.g. 'Delphi VCL forms become Blazor components; no "
     "attempt to preserve pixel layout'."),
    ("dependency_injection", "Dependency injection", False,
     "e.g. 'constructor injection via the built-in container; no service locator'."),
    ("logging", "Logging", False, "e.g. 'ILogger<T>, structured, no Console.WriteLine'."),
    ("testing", "Testing", False,
     "e.g. 'xUnit; convert existing test files, do not invent new tests'."),
    ("naming_conventions", "Naming conventions", False,
     "e.g. 'PascalCase for types and methods, _camelCase for private fields; keep original "
     "domain terms even where they are non-English'."),
    ("preserve_behaviour", "Behaviour preservation", False,
     "What may and may not change. Default is: nothing changes except what the target stack "
     "forces."),
    ("constraints", "Other constraints", False,
     "Anything else binding — target framework version, banned packages, compliance rules."),
]

REQUIRED_KEYS = [key for key, _, required, _ in CHARTER_FIELDS if required]
ALL_KEYS = [key for key, _, _, _ in CHARTER_FIELDS]

DEFAULT_BEHAVIOUR = (
    "Preserve existing behaviour exactly. This is a migration, not a rewrite: do not add "
    "features, do not 'improve' logic that the target stack does not force you to change, "
    "and do not rename domain concepts."
)


class CharterIncomplete(Exception):
    def __init__(self, missing: List[str]):
        self.missing = missing
        labels = {key: label for key, label, _, _ in CHARTER_FIELDS}
        super().__init__(
            "The conversion charter is incomplete. These decisions have to be made before "
            "any file is converted, because every file is converted against them: "
            + ", ".join(labels.get(m, m) for m in missing)
            + "."
        )


def validate(charter: Dict[str, Any]) -> Dict[str, str]:
    """Return a cleaned charter, or raise `CharterIncomplete`.

    Unknown keys are dropped rather than passed through: the charter goes verbatim into
    every system prompt in the run, so it is not a place to let arbitrary caller-supplied
    text accumulate.
    """
    charter = charter or {}
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
    labels = {key: label for key, label, _, _ in CHARTER_FIELDS}
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
    `validate()` actually enforces."""
    return [
        {"key": key, "label": label, "required": required, "help": help_text}
        for key, label, required, help_text in CHARTER_FIELDS
    ]
