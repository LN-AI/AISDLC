"""PHP → .NET: charter answers derived from what the PHP analyzer saw.

The generic advisor names the shape of each decision. This one names the technology, because
here we know both ends. Where the generic option says "one mapping layer over the database",
this one says EF Core with a MySQL provider, and cites the four `mysqli_*` call sites that make
it the right answer.

Every `why` string quotes observations only — call counts, table names, file counts, hazard
classes. Never a redacted value: `lib/db.php` in the test fixture contains a real-looking
password, and the redaction map records that a `db_connect_password` was removed at line 2
without keeping the secret. A `why` that quoted it would put it back into a screen, an export
report and a prompt.

Content, not process: `advice/__init__.py` explains the registry, why this is deterministic, and
what `Option` fields mean.
"""
from typing import Any, Dict, List

from backend.services.modernization.advice import Advisor, Option, blocking_hazards, register
from backend.services.modernization.playbooks import DOTNET_ALIASES

# Which .NET data provider a source database implies. Keys are matched against the analyzer's
# `db_apis` labels and the evidence `datastore` values, both lowercased.
_PROVIDERS = (
    ("mysqli", "MySQL", "Pomelo.EntityFrameworkCore.MySql"),
    ("mysql", "MySQL", "Pomelo.EntityFrameworkCore.MySql"),
    ("pgsql", "PostgreSQL", "Npgsql.EntityFrameworkCore.PostgreSQL"),
    ("postgres", "PostgreSQL", "Npgsql.EntityFrameworkCore.PostgreSQL"),
    ("sqlsrv", "SQL Server", "Microsoft.EntityFrameworkCore.SqlServer"),
    ("mssql", "SQL Server", "Microsoft.EntityFrameworkCore.SqlServer"),
    ("sqlite", "SQLite", "Microsoft.EntityFrameworkCore.Sqlite"),
    ("oci", "Oracle", "Oracle.EntityFrameworkCore"),
    ("oracle", "Oracle", "Oracle.EntityFrameworkCore"),
    # PDO last: it is a wrapper, so a file using PDO over MySQL usually also names mysql
    # somewhere, and the more specific match should win.
    ("pdo", "the source's engine", "the matching EF Core provider"),
)


def _engine(observed: Dict[str, Any]) -> tuple:
    """(engine name, EF provider package) for whatever database was observed."""
    haystack = " ".join(str(x).lower() for x in
                        (list(observed.get("db_apis") or [])
                         + list(observed.get("datastores") or [])))
    for needle, engine, package in _PROVIDERS:
        if needle in haystack:
            return engine, package
    return "the source's engine", "the matching EF Core provider"


def _has_data(observed: Dict[str, Any]) -> bool:
    return bool(observed.get("db_apis") or observed.get("datastores")
                or observed.get("tables"))


def _db_why(observed: Dict[str, Any]) -> str:
    apis = observed.get("db_apis") or []
    tables = observed.get("tables") or []
    bits = []
    if apis:
        bits.append(f"{', '.join(str(a) for a in apis[:4])} in use")
    if tables:
        shown = ", ".join(str(t) for t in tables[:5])
        more = f" (+{len(tables) - 5} more)" if len(tables) > 5 else ""
        bits.append(f"{len(tables)} table(s): {shown}{more}")
    return "; ".join(bits) or "a database was observed"


# ── Rules ──────────────────────────────────────────────────────────────────

def _target_stack(observed: Dict[str, Any]) -> List[Option]:
    ui = bool(observed.get("has_ui"))
    api = bool(observed.get("json")) and not ui
    mvc = Option(
        value=".NET 8 / ASP.NET Core MVC with Razor views",
        label="ASP.NET Core MVC + Razor",
        why="The source renders server-side HTML, and MVC is the closest .NET model to a "
            "page-script application: one action plus one view per page.",
        recommended=ui,
    )
    web_api = Option(
        value=".NET 8 / ASP.NET Core Web API, controllers returning typed DTOs",
        label="ASP.NET Core Web API",
        why="The source emits JSON rather than markup, so there is no view layer to convert.",
        recommended=api,
    )
    razor_pages = Option(
        value=".NET 8 / ASP.NET Core Razor Pages",
        label="ASP.NET Core Razor Pages",
        why="A page-per-file model, which maps almost one-to-one onto PHP page-scripts. Less "
            "ceremony than MVC; fewer options once the app grows past simple pages.",
    )
    if api:
        return [web_api, razor_pages, mvc]
    return [mvc, razor_pages, web_api]


def _target_language(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value="C#",
            label="C#",
            why="The default for new ASP.NET Core work; every package, template and example "
                "assumes it.",
            recommended=True,
        ),
        Option(
            value="VB.NET",
            label="VB.NET",
            why="Only worth choosing if the team already maintains VB.NET. Tooling and examples "
                "for ASP.NET Core are thinner.",
        ),
    ]


def _project_layout(observed: Dict[str, Any]) -> List[Option]:
    count = observed.get("file_count") or 0
    small = count <= 40
    feature = Option(
        value="A single ASP.NET Core project, organised folder-per-feature: each feature folder "
              "holds its controller, its view models and its views together.",
        label="Single project, folder-per-feature",
        why=f"{count} source file(s). At this size layered projects add build ceremony without "
            f"buying separation the codebase needs. One project also cannot have a dependency "
            f"pointing the wrong way, so the layering conflicts that stop a run at the plan "
            f"gate cannot arise at all.",
        recommended=small,
    )
    layered = Option(
        value="One project per layer — Web, Application, Domain, Infrastructure — referencing "
              "inward only, with the Web project holding no data access.",
        label="Project per layer",
        why=f"{count} source file(s). Enforces a dependency direction at compile time, which is "
            f"worth the extra projects once a codebase is large enough to drift. The cost, "
            f"which is real: any dependency the source already has that points the wrong way "
            f"through these layers has no legal expression and needs an interface seam. The "
            f"plan gate names those before conversion and the run stops until each one is "
            f"closed or accepted.",
        recommended=not small,
    )
    return [feature, layered] if small else [layered, feature]


def _no_database(observed: Dict[str, Any]) -> List[Option]:
    """What `data_access` means for a codebase that never reaches a database.

    `data_access` is required by `charter.validate()` and always will be — that list is
    static on purpose (see its docstring). So returning `[]` here left the form asking a
    required question with no answer on offer, and the save was refused with "…before any
    file is converted: Data access". This is the second half of the bug fixed in
    `describe()`: a form may ask for more than is enforced, but it must never ask a
    question it gives no way to answer.

    Saying "there is no database" out loud is also worth more than it looks. The charter
    goes into every conversion call, and a converter handed `$_SESSION['employees']` with
    nothing stated about persistence is free to decide a DbContext was always implied.
    """
    where = ("session state" if observed.get("sessions") else "process memory")
    return [
        Option(
            value=("No database. This codebase does not reach one, and the conversion does "
                   "not introduce one: state the source keeps in the session or in memory "
                   "becomes the ASP.NET Core equivalent — session state, or a service "
                   "registered with the same lifetime — and nothing is persisted that the "
                   "source did not persist. A file that appears to need a database is a "
                   "`manual_followup`, not a licence to add one."),
            label="No database — keep the source's in-memory state",
            why=(f"No database API, table name or datastore was found anywhere in the "
                 f"{observed.get('file_count') or 0} file(s) analysed; the application "
                 f"holds its data in {where}. Converting that into a real database is a "
                 f"behaviour change, so it is a decision rather than a default."),
            recommended=True,
        ),
        Option(
            value=("EF Core 8 with Microsoft.EntityFrameworkCore.Sqlite, replacing the "
                   "source's in-memory state with a real database. Entities modelled on "
                   "the shapes the source holds in memory, table and property names taken "
                   "from the source's array keys."),
            label="Introduce a database (EF Core 8 + SQLite)",
            why="Only if replacing the in-memory state is a decision already taken. It is "
                "a behaviour change rather than a conversion: data would then survive a "
                "restart and be shared between users, and the source's state is neither.",
        ),
    ]


def _data_access(observed: Dict[str, Any]) -> List[Option]:
    if not _has_data(observed):
        return _no_database(observed)
    engine, package = _engine(observed)
    why = _db_why(observed)
    return [
        Option(
            value=(f"EF Core 8 with {package} against the existing {engine} database. One "
                   f"DbContext for the application, entities mapped to the existing tables. All "
                   f"queries through LINQ or parameterised SQL — no string concatenation, ever."),
            label=f"EF Core 8 + {engine}",
            why=(f"{why}. Interpolated SQL in the source is both the mapping target and an "
                 f"injection finding, so the charter has to forbid carrying it across."),
            recommended=True,
        ),
        Option(
            value=(f"Dapper against the existing {engine} database, with the source's SQL "
                   f"statements preserved and every value bound as a parameter rather than "
                   f"interpolated."),
            label=f"Dapper + {engine}",
            why="Closest to the source: each mysqli call becomes one Dapper call, so the "
                "conversion is reviewable query by query. No change-tracking, no migrations, and "
                "the old data model comes across unchanged.",
        ),
        Option(
            value=("EF Core 8 with Microsoft.EntityFrameworkCore.SqlServer, migrating the schema "
                   "from the source database to SQL Server. Table and column names preserved."),
            label="EF Core 8 + migrate to SQL Server",
            why="Only if moving engines is already a decision. It adds a data migration and a "
                "second correctness question on top of the conversion itself.",
        ),
    ]


def _datastore_fate(observed: Dict[str, Any]) -> List[Option]:
    if not _has_data(observed):
        return []
    engine, _ = _engine(observed)
    tables = observed.get("tables") or []
    return [
        Option(
            value=(f"Keep {engine} and keep the existing schema exactly as it is. Reverse-engineer "
                   f"the EF model from the live database rather than writing entities by hand, and "
                   f"generate no migrations during the conversion."),
            label=f"Keep {engine}, keep the schema",
            why=(f"{len(tables) or 'The observed'} table(s) can then be validated against real "
                 f"data, and the conversion can be rolled back without moving anything."),
            recommended=True,
        ),
        Option(
            value=("Keep the schema's table and column names, but move to a different engine. "
                   "Types and reserved words are translated; names are not."),
            label="Keep the schema, change the engine",
            why="Two changes in flight at once. Every defect afterwards could be the conversion "
                "or the migration.",
        ),
    ]


def _error_handling(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value=("Exceptions for genuinely exceptional conditions; an explicit result type for "
                   "expected failures. One global exception-handling middleware turns anything "
                   "unhandled into a ProblemDetails response. Never a try/catch per method, and "
                   "never `catch { }`."),
            label="Global middleware, exceptions for exceptional",
            why="PHP code typically mixes `die()`, `header('Location: …')` and silent `@` "
                "suppression as error handling; each maps to something different in .NET, so the "
                "rule has to be decided once.",
            recommended=True,
        ),
        Option(
            value=("Every fallible operation returns Result<T>. Exceptions are reserved for "
                   "programmer error and are never used for control flow."),
            label="Result<T> everywhere",
            why="Makes every failure path visible in the signature, at the cost of changing the "
                "shape of almost every method.",
        ),
    ]


def _config_strategy(observed: Dict[str, Any]) -> List[Option]:
    categories = observed.get("redaction_categories") or []
    keys = observed.get("config_keys") or []
    if categories:
        why = (f"{len(categories)} kind(s) of secret were redacted out of the source at ingest "
               f"({', '.join(categories)}). Each one has to arrive as configuration, because the "
               f"value is deliberately not stored anywhere in this run.")
    elif keys:
        why = (f"{len(keys)} configuration value(s) were found inline in the source, which is "
               f"where they must stop being.")
    else:
        why = "No literal credential may survive the conversion."
    return [
        Option(
            value=("IOptions<T> bound from appsettings.json at start-up and injected where "
                   "needed. Secrets come only from environment variables or a secret store — "
                   "never a literal in source, never a committed default in appsettings."),
            label="IOptions<T> + secrets from the environment",
            why=why,
            recommended=True,
        ),
        Option(
            value=("IConfiguration read directly at the point of use, with no bound options "
                   "objects."),
            label="IConfiguration read directly",
            why="Fewer types to define; loses the one place that can validate at start-up that "
                "everything required was actually supplied.",
        ),
    ]


def _ui_strategy(observed: Dict[str, Any]) -> List[Option]:
    if not observed.get("has_ui"):
        return []
    return [
        Option(
            value=("Each PHP page-script becomes one controller action, one view model and one "
                   "Razor view. Shared include fragments (header/footer) collapse into a single "
                   "_Layout.cshtml used by every view — not one layout per page. No business "
                   "logic in a .cshtml file, and no SQL in a controller."),
            label="Action + view model + view per page; one shared layout",
            why="PHP page-scripts interleave markup, queries and control flow in one file. This "
                "is the decomposition rule; without it each file converts one-to-one and the "
                "output is not an ASP.NET Core application.",
            recommended=True,
        ),
        Option(
            value=("Each page becomes a Razor Page (.cshtml plus its PageModel), matching the "
                   "source's one-file-per-page structure. Shared includes become a single "
                   "_Layout.cshtml."),
            label="Razor Pages, one per source page",
            why="Structurally the closest thing .NET has to a page-script, so the mapping is the "
                "most obvious to review.",
        ),
        Option(
            value="Convert to Blazor components, preserving structure but not markup.",
            label="Blazor components",
            why="A rewrite of the presentation model rather than a conversion of it, and the "
                "hosting model is a separate decision. Rarely the right first step.",
        ),
    ]


def _composition(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value=("Constructor injection through the built-in service container, registered in "
                   "Program.cs. No service locator, no static mutable state, and nothing "
                   "equivalent to PHP's `global $conn` — the connection is a scoped service."),
            label="Constructor injection, registered in Program.cs",
            why="`global $conn` and function-level `global` are the source's wiring mechanism; "
                "naming the replacement once is what stops half the output reintroducing statics.",
            recommended=True,
        ),
    ]


def _concurrency(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value=("Async/await throughout: every action, service method and data-access call "
                   "that performs I/O is async and awaited to the top. No .Result, no .Wait(), no "
                   "GetAwaiter().GetResult()."),
            label="Async/await throughout",
            why="PHP's database calls are synchronous, so there is no async in the source to "
                "carry across — meaning this is decided here or decided differently in every "
                "file. Mixing the two is what produces thread-pool starvation under load.",
            recommended=True,
        ),
        Option(
            value="Synchronous throughout, matching the source's execution model.",
            label="Synchronous, as PHP was",
            why="Simplest and the most literal conversion. Caps throughput on a request-serving "
                "app, and going back later means touching every signature.",
        ),
    ]


def _url_compatibility(observed: Dict[str, Any]) -> List[Option]:
    routes = observed.get("routes") or []
    entries = observed.get("entrypoints") or []
    if not (routes or entries):
        return []
    n = len(routes) or len(entries)
    detail = ""
    if routes:
        shown = ", ".join(str(r) for r in routes[:3])
        detail = f" Observed: {shown}{' …' if len(routes) > 3 else ''}."
    return [
        Option(
            value=("Existing URLs keep working unchanged, including .php extensions and "
                   "query-string parameter names, via explicit [Route] attributes."
                   + (" Rewrite rules found in .htaccess are reproduced in routing, not dropped."
                      if routes else "")),
            label="Preserve existing URLs exactly",
            why=(f"{n} externally reachable path(s) were extracted from the source.{detail} "
                 f"Anything bookmarked, linked or called by another system breaks if these "
                 f"change."),
            recommended=True,
        ),
        Option(
            value=("Preserve the URLs the .htaccess rewrites already expose as clean paths, and "
                   "permanently redirect the raw .php forms to them."),
            label="Keep clean paths, redirect the .php forms",
            why="Ends up with idiomatic routes while keeping old links alive. Needs one redirect "
                "per legacy path.",
        ),
        Option(
            value="Redesign routes to ASP.NET Core conventions and accept that old links break.",
            label="Redesign routes",
            why="Cleanest result, only safe if every caller is known — which for a legacy PHP app "
                "usually means it is not.",
        ),
    ]


def _auth_model(observed: Dict[str, Any]) -> List[Option]:
    if not observed.get("sessions"):
        return []
    supers = observed.get("superglobals") or []
    why = "`session_start()` and session state were observed in the source."
    if "$_SESSION" in supers:
        why = ("`$_SESSION` is read or written in the source, and `session_start()` was "
               "observed — so per-user state exists and has to land somewhere explicit.")
    return [
        Option(
            value=("Cookie authentication with ASP.NET Core Identity. What the source kept in "
                   "$_SESSION becomes either a claim on the authenticated principal (identity, "
                   "roles) or typed session state (transient things like a cart) — never an "
                   "untyped dictionary."),
            label="Cookie auth + Identity; claims for identity, session for transient state",
            why=why,
            recommended=True,
        ),
        Option(
            value=("Cookie authentication with a hand-written user store against the existing "
                   "users table, rather than adopting the Identity schema."),
            label="Cookie auth, existing users table",
            why="Avoids migrating the existing password hashes into Identity's schema — which "
                "matters, because legacy PHP hashes are often md5 or unsalted sha1 and cannot be "
                "rehashed without the plaintext.",
        ),
        Option(
            value="JWT bearer tokens, no server-side session.",
            label="JWT bearer tokens",
            why="Suits an API. Wrong fit for server-rendered pages, and immediate sign-out stops "
                "being possible.",
        ),
    ]


def _constraints(observed: Dict[str, Any]) -> List[Option]:
    # Blocking hazards only. Interpolated SQL is a hazard in the reporting sense but it has a
    # correct .NET answer (a parameterised query, which the data-access option already mandates)
    # — telling the converter to leave it unimplemented would refuse nearly every data-access
    # line in a legacy PHP codebase.
    hazards = blocking_hazards(observed)
    dispatch = list(observed.get("dispatch") or [])
    if not (hazards or dispatch):
        return []
    parts = []
    if hazards:
        parts.append(
            f"The source uses constructs that have no safe mechanical equivalent "
            f"({', '.join(str(h) for h in hazards[:6])}). Each occurrence is to be left "
            f"unimplemented with a // MIGRATION-BLOCKED comment naming it, and listed for human "
            f"review. Do not substitute a switch, a reflection call, a dynamic assembly or any "
            f"other 'equivalent'.")
    if dispatch:
        parts.append(
            f"{len(dispatch)} dynamic dispatch site(s) could not be resolved statically; the "
            f"target must not invent a resolution for them.")
    why = (f"{len(hazards)} hazard class(es) and {len(dispatch)} unresolved dispatch site(s) "
           f"observed. These are the constructs where a faithful-looking translation quietly "
           f"changes what the program is able to do.")
    return [
        Option(
            value=" ".join(parts),
            label="Block unsafe constructs; no invented equivalents",
            why=why,
            recommended=True,
        ),
    ]


def _testing(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value=("xUnit. Convert any test files that exist and keep them passing; do not invent "
                   "new tests during the conversion — an invented test encodes the converter's "
                   "guess about intent as if it were a requirement."),
            label="xUnit; convert existing, invent none",
            why="Keeps a failing test meaning 'the conversion is wrong' rather than 'the test is "
                "new'.",
            recommended=True,
        ),
    ]


def _logging(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value=("ILogger<T> via injection, structured (message templates, not interpolated "
                   "strings). No Console.WriteLine, no echo-as-diagnostics, and never log a "
                   "credential, connection string or personal identifier."),
            label="ILogger<T>, structured",
            why="PHP code commonly uses `echo` and `print_r` for both diagnostics and user "
                "output; the two have different destinations in .NET.",
            recommended=True,
        ),
    ]


def _naming(observed: Dict[str, Any]) -> List[Option]:
    return [
        Option(
            value=("PascalCase for types and methods, camelCase for locals and parameters, "
                   "_camelCase for private fields. Keep the source's domain terms exactly — "
                   "including abbreviations and non-English words — even where they read oddly in "
                   "C#. Snake_case table and column names stay as they are in the database and "
                   "are mapped, not renamed."),
            label="C# conventions; source domain terms preserved",
            why="Renaming during conversion is what makes a later line-by-line review against the "
                "PHP impossible.",
            recommended=True,
        ),
    ]


_RULES = {
        "target_stack": _target_stack,
        "target_language": _target_language,
        "project_layout": _project_layout,
        "data_access": _data_access,
        "datastore_fate": _datastore_fate,
        "error_handling": _error_handling,
        "config_strategy": _config_strategy,
        "ui_strategy": _ui_strategy,
        "composition": _composition,
        "concurrency_model": _concurrency,
        "url_compatibility": _url_compatibility,
        "auth_model": _auth_model,
        "constraints": _constraints,
        "testing": _testing,
        "logging": _logging,
        "naming_conventions": _naming,
}

# Registered once per .NET spelling. `lookup` matches a fragment and '.NET 8' normalises to
# `net8`, which does not contain `dotnet` — so without the alias set a charter written the way
# a human writes it would silently fall through to the generic advisor. The list is shared with
# the playbook registry for exactly that reason.
for _alias in DOTNET_ALIASES:
    register(Advisor(source="php", target=_alias, rules=_RULES))
