"""Project files corrected from what the code actually does, once every file exists.

The scaffold has to write `.csproj` files *before* any conversion happens — it is what the
conversion lands inside. So its package list can only come from the charter (seven regexes
over free text) and its `ProjectReference` edges can only come from the architect's proposed
layout. Both are predictions, made before a single line of C# exists.

They are wrong in two specific, compile-breaking ways:

  * **Packages went to the entry project only.** A Domain project full of `[Table]` attributes
    and `DbContext` subclasses got a `.csproj` with no `PackageReference` at all.
  * **`ProjectReference` came from the architect's `depends_on`.** A controller that ended up
    calling a domain type the architect had not predicted produces CS0246 — with a message
    about a missing using, which is the wrong thing to go and fix.

This stage runs after `convert`, when the answer has stopped being a prediction: every
artifact exists and every symbol is in the registry with the namespace it lives in. It reads
those, corrects the project files, and makes no model call at all — the same argument as D-50
for the scaffold itself. A model asked which NuGet package `DbContext` comes from will
sometimes say `Microsoft.EntityFrameworkCore` and sometimes invent a version.

`store.complete_unit` versions artifacts rather than overwriting them, so the scaffold's
original `.csproj` stays readable next to the corrected one.
"""
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from backend.services.modernization import graph
from backend.services.modernization import usings
from backend.services.modernization.scaffold import dotnet as dotnet_scaffold

logger = logging.getLogger(__name__)

# A type or attribute that can only have come from a package, mapped to the package that
# supplies it. Versions are pinned here for the same reason they are pinned in the scaffold:
# a package version is a fact about a release, not something to derive.
#
# Deliberately narrow. Every entry has to be a name that appears in *generated* code and
# essentially nowhere else — a false positive adds a dependency the project does not need and
# a restore has to download, which is worse than the missing reference this exists to fix.
_PACKAGE_TYPES: Tuple[Tuple[str, str, str], ...] = (
    (r"\bDbContext\b|\bDbSet<", "Microsoft.EntityFrameworkCore", "8.0.10"),
    (r"\[Table\(|\[Column\(|\[ForeignKey\(", "Microsoft.EntityFrameworkCore.Abstractions", "8.0.10"),
    (r"\bUseSqlServer\(", "Microsoft.EntityFrameworkCore.SqlServer", "8.0.10"),
    (r"\bUseMySql\(|\bServerVersion\b", "Pomelo.EntityFrameworkCore.MySql", "8.0.2"),
    (r"\bUseNpgsql\(", "Npgsql.EntityFrameworkCore.PostgreSQL", "8.0.10"),
    # SQLite was simply missing, and it is the provider a small legacy application is most
    # often converted onto — a 45-file run whose charter said SQLite emitted a solution that
    # referenced the provider nowhere at all.
    (r"\bUseSqlite\(", "Microsoft.EntityFrameworkCore.Sqlite", "8.0.10"),
    # ── Packages a class library needs and a Web SDK project gets for free ──
    #
    # These are the ones a converted *library* project asks for and does not receive. The
    # entry project resolves them through `Microsoft.NET.Sdk.Web`'s framework reference, so
    # they never showed up until a run put a DbContext factory and a connection interceptor
    # in `Acme.Ems.Infrastructure` — a plain class library — and the build failed with four
    # errors that no rewrite of a `.cs` file could ever fix. That is the sharp edge: the
    # rework loop reopens the *unit*, the model rewrites correct C# again, and the missing
    # thing is a line in a `.csproj` only this module writes. The model had already written
    # `using Microsoft.EntityFrameworkCore.Diagnostics;` correctly both times.
    (r"\bDbConnectionInterceptor\b|\bDbCommandInterceptor\b|\bConnectionEndEventData\b"
     r"|\bCommandEventData\b|\bRelationalEventId\b",
     "Microsoft.EntityFrameworkCore.Relational", "8.0.10"),
    (r"\bIDesignTimeDbContextFactory<", "Microsoft.EntityFrameworkCore.Design", "8.0.10"),
    (r"\bIConfiguration\b|\bConfigurationBuilder\b",
     "Microsoft.Extensions.Configuration.Abstractions", "8.0.0"),
    (r"\.AddJsonFile\(", "Microsoft.Extensions.Configuration.Json", "8.0.1"),
    (r"\.AddEnvironmentVariables\(",
     "Microsoft.Extensions.Configuration.EnvironmentVariables", "8.0.0"),
    (r"\bIServiceCollection\b",
     "Microsoft.Extensions.DependencyInjection.Abstractions", "8.0.2"),
    (r"\bILogger<", "Microsoft.Extensions.Logging.Abstractions", "8.0.2"),
    (r"\bIOptions<|\bIOptionsMonitor<", "Microsoft.Extensions.Options", "8.0.2"),
    (r"\bMySqlConnection\b", "MySqlConnector", "2.3.7"),
    (r"\bSqlConnection\b", "Microsoft.Data.SqlClient", "5.2.2"),
    (r"\bNpgsqlConnection\b", "Npgsql", "8.0.5"),
    # Dapper is extension methods on IDbConnection, so the connection type alone is not
    # enough — require one of its own calls.
    (r"\.Query<|\.QueryAsync<|\.QueryFirstOrDefault|\.ExecuteAsync\(", "Dapper", "2.1.35"),
    (r"\bLog\.Logger\b|\bLoggerConfiguration\b", "Serilog.AspNetCore", "8.0.3"),
    (r"\bJsonConvert\b|\bJObject\b", "Newtonsoft.Json", "13.0.3"),
    (r"\bIMapper\b|\bCreateMap<", "AutoMapper", "13.0.1"),
    (r"\bAbstractValidator<", "FluentValidation", "11.10.0"),
)

_PACKAGE_REF = re.compile(r'<PackageReference\s+Include="([^"]+)"', re.IGNORECASE)
_PROJECT_REF = re.compile(r'<ProjectReference\s+Include="[^"]*?([^\\/"]+)\.csproj"', re.IGNORECASE)

# ── The database the charter chose, and the services with no interface ─────────────────
#
# `host_plan` registered an interface with exactly one implementation and nothing else, which
# left the two shapes a converted application is most often missing. Measured on run
# 38ee8e01's export, whose `Program.cs` ends up with **no registrations at all**:
#
#   * `EmsDbContext(DbContextOptions<EmsDbContext>)` — never registered, no EF provider
#     package anywhere in the solution, and no connection string in `appsettings.json`,
#     while the charter's own `data_access` decision reads "EF Core 8 with
#     Microsoft.EntityFrameworkCore.Sqlite against the existing SQLite database". The
#     information was in the run the whole time and nothing acted on it.
#   * `AuthService`, `EmployeeService`, `PayrollService` — concrete classes with no
#     interface, taken as constructor parameters by controllers, and never candidates for
#     registration because the only rule was about interfaces.
#
# Emitting the registration is also what adds the provider package: `_PACKAGE_TYPES` keys on
# `UseSqlite(` appearing in generated code, so writing the line closes the missing-package gap
# without a second mechanism.

_DBCONTEXT_CLASS = re.compile(
    r"\b(?:public|internal)\s+(?:sealed\s+|partial\s+)*class\s+(\w+)\s*:\s*"
    r"[^\{\r\n]*\bDbContext\b")

# A provider is named, never guessed. Order matters only for the charter scan: `sqlserver`
# has to be tested before `sql`, so the longest spellings come first within each entry.
_PROVIDERS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("Microsoft.EntityFrameworkCore.SqlServer", "UseSqlServer",
     ("microsoft.entityframeworkcore.sqlserver", "sql server", "sqlserver", "mssql")),
    ("Microsoft.EntityFrameworkCore.Sqlite", "UseSqlite",
     ("microsoft.entityframeworkcore.sqlite", "sqlite")),
    ("Npgsql.EntityFrameworkCore.PostgreSQL", "UseNpgsql",
     ("npgsql.entityframeworkcore.postgresql", "postgresql", "postgres", "npgsql")),
    ("Pomelo.EntityFrameworkCore.MySql", "UseMySql",
     ("pomelo.entityframeworkcore.mysql", "mariadb", "mysql")),
)

# The configuration key the registration reads. A *name*, never a value: the charter this
# product ships says "secrets come only from environment variables or a secret store — never
# a literal in source, never a committed default in appsettings", and a connection string is
# a secret. `ConnectionStrings__DefaultConnection` in the environment fills it.
CONNECTION_NAME = "DefaultConnection"


def db_provider(charter: Optional[Dict[str, Any]],
                artifacts: Iterable[Dict[str, Any]]) -> Tuple[str, str]:
    """-> (package, `Use…` call) for the database this run decided on, or ("", "").

    Read from what the run already holds, in order of authority: a provider the converted
    code is already calling, then the charter's own data-access decision, then a provider
    package some project already references. Never inferred from entity mappings or a
    comment — a wrong provider is a solution that restores, builds, and cannot open a
    connection, which is harder to diagnose than no registration at all.
    """
    text = " ".join(str((a.get("content") or "")) for a in artifacts or ()
                    if str(a.get("path") or "").lower().endswith((".cs", ".csproj")))
    for package, call, _spellings in _PROVIDERS:
        if call + "(" in text or package.lower() in text.lower():
            return package, call
    haystack = " ".join(str(v) for v in (charter or {}).values()).lower()
    for package, call, spellings in _PROVIDERS:
        if any(word in haystack for word in spellings):
            return package, call
    return "", ""


def db_plan(artifacts: List[Dict[str, Any]],
            charter: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The DbContext registration this solution is missing, or an explained absence.

    `context` is the type's fully-qualified name, `registration` the line to add (empty if it
    cannot be written honestly), and `note` says why when it cannot.
    """
    context = ""
    takes_options = False
    for artifact in artifacts or ():
        path = str(artifact.get("path") or "").replace("\\", "/")
        if not path.lower().endswith(".cs"):
            continue
        code = usings.strip_non_code(artifact.get("content") or "")
        found = _DBCONTEXT_CLASS.search(code)
        if not found:
            continue
        name = found.group(1)
        namespace = _namespace_of(artifact.get("content") or "")
        context = f"{namespace}.{name}" if namespace else name
        takes_options = bool(re.search(
            r"\bpublic\s+" + re.escape(name) + r"\s*\([^)]*\bDbContextOptions\b", code))
        break
    if not context:
        return {"context": "", "registration": "", "note": "", "package": ""}

    # A context that configures itself in `OnConfiguring` needs no options and no provider
    # call — registering it as a scoped service is the whole job.
    if not takes_options:
        return {"context": context, "package": "",
                "registration": f"builder.Services.AddScoped<{context}>();",
                "note": ""}

    package, call = db_provider(charter, artifacts)
    if not call:
        return {
            "context": context, "registration": "", "package": "",
            "note": (f"`{context}` takes `DbContextOptions`, so it can only be registered "
                     f"with a provider — and nothing in the charter, the converted code or "
                     f"the project files names one. Add the EF provider package for the "
                     f"target database and register it in `Program.cs`."),
        }
    connection = f'builder.Configuration.GetConnectionString("{CONNECTION_NAME}")'
    # MySql cannot be configured from a connection string alone; the server version is a
    # required second argument and `AutoDetect` is the only value derivable here.
    inner = (f"options.{call}({connection}, ServerVersion.AutoDetect({connection}))"
             if call == "UseMySql" else f"options.{call}({connection})")
    # One line, and the connection string inlined rather than held in a local. A local named
    # `connection` would be a compile error the second time `apply_to_program_cs` ran over the
    # same file, and this stage is explicitly idempotent — the build loop re-runs it.
    return {
        "context": context, "package": package,
        "registration": (
            f"builder.Services.AddDbContext<{context}>(options => {inner});"),
        "note": "",
    }


def _container_constructed(constructors: Dict[str, List[str]], seeds: Set[str],
                           implementations: Optional[Dict[str, List[str]]] = None) -> Set[str]:
    """Types the container itself builds: the seeds, and whatever they ask for, transitively.

    The reason this closure exists rather than "every constructor parameter in the run": a
    view model or an entity with a public constructor is not a service, and treating
    `Employee` as a service because `PayslipResult(Employee, decimal)` exists would put an EF
    entity in the container — or, in `unregistered_dependencies`, report a **failure** over
    one. A parameter is only a dependency if something the container constructs asks for it,
    and that starts at the controllers and the registered implementations.

    Interfaces are followed to their implementations, or the walk stops one step short of
    everything interesting: a controller takes `IEmployeeRepository`, and it is
    `EmployeeRepository`'s constructor — not the interface's — that needs a `DbContext`.
    """
    reached = set(seeds)
    frontier = list(seeds)
    while frontier:
        owner = frontier.pop()
        for name in list(constructors.get(owner) or ()) + list(
                (implementations or {}).get(owner) or ()):
            if name not in reached:
                reached.add(name)
                frontier.append(name)
    return reached

# ── Host wiring ────────────────────────────────────────────────────────────────────────
#
# The scaffold writes `Program.cs` before any C# exists, so it can only leave a placeholder
# where the registrations go. Nothing used to replace it, and the result is the worst kind
# of output this pipeline can produce: a project that **compiles clean and 500s on every
# route** (`Unable to resolve service for type 'IEmployeeRepository'`). A build gate cannot
# catch that, which is why this is computed here and not left to the build loop.
#
# Same rule as the rest of this module — read the converted code, make no model call. Which
# concrete type implements an interface is a fact about the code, and a model asked to guess
# it will sometimes register a type that does not exist.
MARKER = "// Registrations for converted services are added here as components are converted."

_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)\s*[;{]", re.MULTILINE)
_INTERFACE = re.compile(r"\b(?:public|internal)\s+(?:partial\s+)?interface\s+(I\w+)")
# `abstract` is excluded deliberately: registering an abstract type as an implementation is a
# runtime failure, and it is never the thing the container should construct.
_CLASS = re.compile(
    r"\b(?:public|internal)\s+(?:sealed\s+|partial\s+|static\s+)*class\s+(\w+)\s*:\s*([^\{\r\n]+)")
_CONTROLLER = re.compile(r"\b(?:public|internal)\s+(?:sealed\s+|partial\s+)*class\s+(\w+)Controller\b")


def _namespace_of(content: str) -> str:
    m = _NAMESPACE.search(content or "")
    return m.group(1) if m else ""


# `public SomeClass(IFoo foo, Bar bar)` — the constructor the container has to satisfy. Only
# the first parameter list after a class-shaped name, and only `public`, because a private or
# a copy constructor is not what dependency injection calls.
_CONSTRUCTOR = re.compile(
    r"\bpublic\s+([A-Z]\w*)\s*\(([^)]*)\)\s*(?::\s*(?:base|this)\s*\([^)]*\)\s*)?\{",
    re.DOTALL)

# Every class this run declares, with or without a base list. `_CLASS` above requires
# `: Bases` because its job is finding *implementations* — reusing it here silently skipped
# every plain class, which is precisely the population this check exists for: `UserRepository`
# and `EmsDbContext` derive from nothing and are exactly what goes unregistered.
_ANY_CLASS = re.compile(
    r"\b(?:public|internal)\s+(?:sealed\s+|partial\s+|static\s+|abstract\s+)*class\s+(\w+)")

# A class no `AddScoped<T>()` can name. `static` and `abstract` are not legal type arguments
# at all, and an open generic needs its arguments supplied — so registering any of the three
# is not an over-eager registration, it is a **compile error in generated code**, which is the
# one thing a wiring stage must never produce. `_ANY_CLASS` accepts all three modifiers by
# design, because `unregistered_dependencies` wants the full declared set; the registration
# pass needs the subset it can actually construct.
_UNCONSTRUCTIBLE_CLASS = re.compile(
    r"\b(?:public|internal)\s+(?:sealed\s+|partial\s+)*(?:static|abstract)\s+"
    r"(?:sealed\s+|partial\s+)*class\s+(\w+)")
_GENERIC_CLASS = re.compile(
    r"\b(?:public|internal)\s+(?:sealed\s+|partial\s+|static\s+|abstract\s+)*"
    r"class\s+(\w+)\s*<")

# One parameter: optional attributes, then a type, then a name. The type may be generic or
# nullable; the last identifier before the parameter name is what has to be registered.
_PARAM_TYPE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:params\s+|in\s+|out\s+|ref\s+)?"
                         r"([A-Za-z_][\w.]*(?:<[^>]*>)?\??)\s+[A-Za-z_]\w*\s*(?:=.*)?$")

# Types the container supplies itself, or that are never injected. Everything else that this
# run declared and nothing registered is a service that will not resolve.
_FRAMEWORK_INJECTED = frozenset({
    "ILogger", "IConfiguration", "IHttpContextAccessor", "IWebHostEnvironment",
    "IHostEnvironment", "IServiceProvider", "IMemoryCache", "IOptions", "IOptionsMonitor",
    "IOptionsSnapshot", "DbContextOptions", "IUrlHelper", "IActionContextAccessor",
    "IAntiforgery", "IAuthorizationService", "IMapper", "HttpClient", "IHttpClientFactory",
})

MAX_UNREGISTERED = 20


def _parameter_types(argument_list: str) -> List[str]:
    """The bare type name of each parameter in one constructor signature."""
    found: List[str] = []
    depth = 0
    current = ""
    # Split on commas that are not inside a generic argument list: `IRepo<A, B> x, C y`.
    for char in argument_list:
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            found.append(current)
            current = ""
        else:
            current += char
    found.append(current)
    types: List[str] = []
    for chunk in found:
        match = _PARAM_TYPE.match(chunk)
        if not match:
            continue
        bare = match.group(1).split("<")[0].rstrip("?").rsplit(".", 1)[-1]
        if bare:
            types.append(bare)
    return types


_REGISTRATION_CALL = re.compile(
    r"\.Add(?:Scoped|Singleton|Transient|DbContext|DbContextFactory|HttpClient)\b")


def registrations_in(program_content: str) -> List[str]:
    """The service registrations `Program.cs` really performs.

    Comments are stripped **first**, and that is the whole reason this is a function rather
    than a line filter at the call site. Run 9c8cf4fe's `Program.cs` carries five registrations
    as commented-out suggestions —

        //   builder.Services.AddScoped<IUserRepository, UserRepository>();

    — and a filter that merely looked for `AddScoped` in the text counted every one of them,
    so the check for unresolvable services reported none on a host that registers nothing for
    those types. A false negative in the check whose entire job is catching a false negative.
    """
    code = usings.strip_non_code(program_content or "")
    return [line for line in code.splitlines() if _REGISTRATION_CALL.search(line)]


def unregistered_dependencies(artifacts: List[Dict[str, Any]],
                             registrations: Iterable[str]) -> List[Dict[str, Any]]:
    """Constructor parameters this run declared and `Program.cs` will not resolve.

    The remaining half of C-59, which is the one failure a build gate cannot catch: the
    solution compiles, the host starts, and the first request that needs the service throws
    `Unable to resolve service for type '…'`. `host_plan` registers an interface only when
    exactly one class implements it, and deliberately says nothing about two cases that are
    just as fatal —

    * an interface with **no** implementation in the conversion, which `reconcile` reports as
      a *warning* while the run completes and ships;
    * a constructor taking a **concrete** class, which was never a candidate for registration
      at all. Run 9c8cf4fe's `AuthService(UserRepository)` is exactly this shape, and it is
      what a converter writes whenever the interface it should have depended on was never
      declared.

    Framework services are excluded because the container supplies them. A type this run
    never declared is excluded too: it comes from a package, and guessing at a NuGet type's
    lifetime is not this stage's business.
    """
    declared: Set[str] = set()
    constructors: List[Tuple[str, str, List[str]]] = []
    params_of: Dict[str, List[str]] = {}
    impls_of: Dict[str, List[str]] = {}
    controllers: Set[str] = set()
    for artifact in artifacts:
        path = (artifact.get("path") or "").replace("\\", "/")
        if not path.lower().endswith(".cs"):
            continue
        code = usings.strip_non_code(artifact.get("content") or "")
        declared.update(_INTERFACE.findall(code))
        declared.update(_ANY_CLASS.findall(code))
        controllers.update(f"{name}Controller" for name in _CONTROLLER.findall(code))
        for cls, bases in _CLASS.findall(code):
            for base in (b.split("<")[0].strip() for b in bases.split(",")):
                if base.startswith("I") and len(base) > 1 and base[1:2].isupper():
                    impls_of.setdefault(base, []).append(cls)
        for owner, arguments in ((m.group(1), m.group(2)) for m in _CONSTRUCTOR.finditer(code)):
            if arguments.strip():
                constructors.append((path, owner, _parameter_types(arguments)))
                params_of.setdefault(owner, []).extend(_parameter_types(arguments))

    registered: Set[str] = set()
    for line in registrations or ():
        for name in re.findall(r"[<,]\s*([A-Za-z_][\w.]*)", str(line)):
            registered.add(name.rsplit(".", 1)[-1])

    # A type is only a *dependency* if something the container constructs asks for it. Without
    # this the check reports a failure over any class with a public constructor — an entity
    # taken by a value object, say — which is a false `fail` on correct output, and the fastest
    # way to have a check switched off. Starts at the controllers and whatever is already
    # registered, and follows interfaces to their implementations.
    #
    # With **no** seeds the closure is empty and would clear every finding: a minimal-API or
    # worker-service target has no `*Controller` at all, and on the first pass nothing is
    # registered yet. Reporting nothing there is the "absence looks like success" failure this
    # product keeps closing, so no seeds means no narrowing — every constructor counts, which
    # is exactly what this check did before the closure existed.
    seeds = controllers | registered
    resolved_by_container = (_container_constructed(params_of, seeds, impls_of)
                             if seeds else None)

    missing: Dict[str, List[str]] = {}
    for path, owner, types in constructors:
        if resolved_by_container is not None and owner not in resolved_by_container:
            continue
        for name in types:
            if name in _FRAMEWORK_INJECTED or name not in declared or name in registered:
                continue
            if name == owner:
                continue
            missing.setdefault(name, []).append(f"{owner} ({path})")

    issues: List[Dict[str, Any]] = []
    for name in sorted(missing)[:MAX_UNREGISTERED]:
        wanted_by = sorted(set(missing[name]))
        issues.append({
            "kind": "unregistered_dependency",
            "severity": "fail",
            "type": name,
            "detail": (
                f"`{name}` is taken as a constructor parameter by "
                + ", ".join(f"`{w}`" for w in wanted_by[:6])
                + (f" and {len(wanted_by) - 6} more" if len(wanted_by) > 6 else "")
                + ", and nothing registers it with the container. This compiles and the "
                  "application starts; the first request that needs it fails with "
                  f"`Unable to resolve service for type '{name}'`."
                + ("" if name.startswith("I") else
                   " It is a concrete class — either register it, or declare an interface "
                   "for it and register that.")),
        })
    return issues


def host_plan(artifacts: List[Dict[str, Any]], entry_project: str,
              charter: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What `Program.cs` is missing, read off the converted code. Pure — no I/O.

    Returns `services` (registration lines), `view_formats` (Razor search paths for a layout
    the default view engine will not find), and the two feature flags the host needs.
    """
    interfaces: Dict[str, str] = {}          # IFoo -> namespace that declares it
    impls: Dict[str, List[Tuple[str, str]]] = {}   # IFoo -> [(class, namespace)]
    controllers: Set[str] = set()
    view_paths: List[str] = []
    needs_session = False
    needs_http_context = False
    # Every class this run declares, by name -> the namespaces declaring it. A name in two
    # namespaces is two types, and the container cannot be told which one to build; a name
    # twice in one namespace is a `partial` class, which is one type.
    class_namespaces: Dict[str, Set[str]] = {}
    constructors: Dict[str, List[str]] = {}
    unconstructible: Set[str] = set()

    for artifact in artifacts:
        path = (artifact.get("path") or "").replace("\\", "/")
        raw = artifact.get("content") or ""
        if path.lower().endswith(".cshtml"):
            view_paths.append(path)
            continue
        if not path.lower().endswith(".cs"):
            continue
        code = usings.strip_non_code(raw)
        namespace = _namespace_of(raw)
        for name in _INTERFACE.findall(code):
            interfaces[name] = namespace
        for cls, bases in _CLASS.findall(code):
            for base in (b.strip() for b in bases.split(",")):
                base = base.split("<")[0].strip()
                if base.startswith("I") and len(base) > 1 and base[1:2].isupper():
                    impls.setdefault(base, []).append((cls, namespace))
        for cls in _ANY_CLASS.findall(code):
            class_namespaces.setdefault(cls, set()).add(namespace)
        unconstructible.update(_UNCONSTRUCTIBLE_CLASS.findall(code))
        unconstructible.update(_GENERIC_CLASS.findall(code))
        for owner, arguments in ((m.group(1), m.group(2))
                                 for m in _CONSTRUCTOR.finditer(code)):
            if arguments.strip():
                constructors.setdefault(owner, []).extend(_parameter_types(arguments))
        controllers.update(_CONTROLLER.findall(code))
        if re.search(r"\bISession\b|\.Session\b", code):
            needs_session = True
        if "IHttpContextAccessor" in code:
            needs_http_context = True

    services: List[str] = []
    ambiguous: List[str] = []
    ambiguous_detail: List[Dict[str, Any]] = []
    chosen_notes: List[str] = []
    registered_names: Set[str] = set()
    db = db_plan(artifacts, charter)
    for interface in sorted(interfaces):
        found = impls.get(interface) or []
        if not found:
            # Implemented outside the conversion, or not at all — reconciliation already
            # reports the second.
            continue
        if len(found) > 1:
            # Registering neither is what this did, and it is the wrong default. Run 38ee8e01
            # declared and implemented four seams *correctly* and shipped an app that threw on
            # its first request, because four registrations were reported ambiguous and left
            # out. A decision the container cannot make is not the same as a decision nobody
            # can make: the conventional pairing is a fact about the names, and a discriminated
            # choice that says so beats a container with a hole in it.
            preferred = _preferred_implementation(interface, found)
            if preferred is None:
                ambiguous.append(f"{interface} ({', '.join(c for c, _ in found)})")
                ambiguous_detail.append({
                    "interface": interface,
                    "namespace": interfaces[interface],
                    "candidates": [{"class": c, "namespace": ns} for c, ns in found],
                })
                continue
            found = [preferred]
            chosen_notes.append(
                f"{interface} -> {preferred[0]} (chosen over "
                f"{', '.join(c for c, _ in impls[interface] if c != preferred[0])})")
        cls, impl_ns = found[0]
        iface_ns = interfaces[interface]
        lhs = f"{iface_ns}.{interface}" if iface_ns else interface
        rhs = f"{impl_ns}.{cls}" if impl_ns else cls
        services.append(f"builder.Services.AddScoped<{lhs}, {rhs}>();")
        registered_names.update({interface, cls})

    # ── Concrete services nothing could have registered ───────────────────
    #
    # A class with no interface is not a candidate for the pass above, and a converter writes
    # one whenever the source had no abstraction to preserve — `AuthService`,
    # `EmployeeService`, `PayrollService` on run 38ee8e01, all three taken by a controller and
    # all three unregistered. Only types the container actually constructs are followed, so an
    # entity or a view model with a public constructor is never registered as a service.
    seeds = {cls for pairs in impls.values() for cls, _ns in pairs}
    seeds |= {f"{name}Controller" for name in controllers}
    # `wanted - seeds`, not `wanted`. A seed is a starting point for the closure, not a service
    # to register: a controller is built by MVC, and an implementation of a declared interface
    # is either registered by the pass above or is one of two candidates that pass refused to
    # choose between. Registering the seeds too made that refusal meaningless — an ambiguous
    # interface got *both* its implementations registered as concrete types, so the interface
    # still would not resolve and two useless registrations appeared beside the checkpoint
    # asking a human to choose. Caught by the check that has asserted "two implementations
    # register neither" since this stage was written.
    wanted = _container_constructed(constructors, seeds) - seeds
    for name in sorted(wanted):
        if name in registered_names or name in _FRAMEWORK_INJECTED or name in interfaces:
            continue
        if name.endswith("Controller") or name == db.get("context", "").rsplit(".", 1)[-1]:
            continue
        if name in unconstructible:
            continue
        namespaces = class_namespaces.get(name) or set()
        # Two namespaces means two different types with one name, and the container cannot be
        # told which without a decision this stage is not entitled to make — the same rule the
        # interface pass follows. `ownership.foreign_declarations` is what stops it happening.
        if len(namespaces) != 1:
            if len(namespaces) > 1:
                ambiguous.append(f"{name} ({', '.join(sorted(namespaces))})")
            continue
        namespace = next(iter(namespaces))
        services.append(
            f"builder.Services.AddScoped<{namespace}.{name}>();" if namespace
            else f"builder.Services.AddScoped<{name}>();")
        registered_names.add(name)

    # ── The database ──────────────────────────────────────────────────────
    if db.get("registration"):
        services.append(db["registration"])

    return {
        "services": services,
        "ambiguous": ambiguous,
        # The same information with a shape a checkpoint can offer as options, so a genuine tie
        # becomes a decision a human makes once rather than a registration nobody makes at all.
        "ambiguous_detail": ambiguous_detail,
        # Choices this stage made on a discriminator, reported rather than silent — the D-90
        # and D-98 rule that a correction the reader is not told about is the actual problem.
        "chosen": chosen_notes,
        "db_context": db.get("context") or "",
        "db_note": db.get("note") or "",
        "view_formats": _view_formats(view_paths, controllers, entry_project),
        "needs_session": needs_session,
        "needs_http_context": needs_http_context,
    }


def _view_formats(view_paths: List[str], controllers: Set[str], entry_project: str) -> List[str]:
    """Razor search paths for views the default engine cannot find.

    ASP.NET Core only looks in `Views/{Controller}` and `Views/Shared`. A folder-per-feature
    layout — which the architect chooses often, because it is what the charter asks for — puts
    them in `Features/{Controller}/Views`, and every action then throws
    `InvalidOperationException: The view 'Index' was not found`. The format is derived from
    where the views actually landed rather than assumed, so an architect that picks a
    different shape is still wired correctly.
    """
    formats: Set[str] = set()
    for path in view_paths:
        rel = path[len(entry_project) + 1:] if entry_project and path.startswith(
            entry_project + "/") else path
        parts = rel.split("/")
        if len(parts) < 2:
            continue
        name = parts[-1]
        if name.startswith("_"):
            continue                      # _Layout / _ViewStart live at their literal path
        directory = parts[:-1]
        if directory and directory[0] == "Views" and len(directory) == 2:
            continue                      # already the convention Razor searches
        rebuilt = ["{1}" if seg in controllers else seg for seg in directory]
        if "{1}" not in rebuilt:
            continue                      # nothing controller-shaped — cannot generalise
        formats.add("/" + "/".join(rebuilt) + "/{0}.cshtml")
    return sorted(formats)


def _registers_pair(text: str, lhs: str, rhs: str) -> bool:
    """True when `text` already registers this interface against this implementation,
    whether it named them fully-qualified or relied on a `using`."""
    left, right = lhs.rsplit(".", 1)[-1], rhs.rsplit(".", 1)[-1]
    for match in re.finditer(r"Add(?:Scoped|Singleton|Transient)\s*<([^>]+)>", text):
        parts = [p.strip().rsplit(".", 1)[-1] for p in match.group(1).split(",")]
        if len(parts) == 2 and parts[0] == left and parts[1] == right:
            return True
    return False


def apply_to_program_cs(content: str, plan: Dict[str, Any]) -> str:
    """Insert the registrations. Idempotent, and never removes what is already there."""
    text = content or ""
    if not text.strip():
        return text
    additions: List[str] = []

    # Match the *call*, not one spelling of it. A converter that writes the configured form,
    # `AddSession(options => { … })`, does not contain the literal `AddSession()` — testing
    # for that re-registered session on top of the converter's own configured call, and the
    # duplicate `AddDistributedMemoryCache()` beside it, in a file that already worked.
    if plan.get("needs_http_context") and "AddHttpContextAccessor" not in text:
        additions.append("builder.Services.AddHttpContextAccessor();")
    if plan.get("needs_session"):
        if "AddDistributedMemoryCache" not in text:
            additions.append("builder.Services.AddDistributedMemoryCache();")
        if "AddSession" not in text:
            additions.append("builder.Services.AddSession();")
    # Compared on the type *pair*, not the rendered line. This stage emits fully-qualified
    # names; a converter that emitted `AddScoped<IFoo, Foo>()` plus a `using` registers the
    # same pair in text that shares no substring with it, and an exact match would register
    # it a second time.
    for line in plan.get("services") or []:
        pair = re.search(r"<\s*([^,>]+?)\s*,\s*([^>]+?)\s*>", line)
        if pair and _registers_pair(text, pair.group(1), pair.group(2)):
            continue
        # A one-type-argument registration — `AddScoped<Foo>()`, `AddDbContext<Ctx>(…)` — is
        # compared on the *call plus the short type name*, for the same reason the pair is
        # compared structurally: a converter that already registered it will have written the
        # unqualified name and a `using`, which shares no substring with the qualified form.
        # Harmless for `AddScoped`, where a duplicate is merely redundant; not harmless for
        # `AddDbContext`, where two calls configure the same context twice.
        single = re.match(r"(builder\.Services\.(\w+))<\s*([\w.]+)\s*>", line)
        if not pair and single and re.search(
                re.escape(single.group(2)) + r"<\s*(?:[\w.]+\.)?"
                + re.escape(single.group(3).rsplit(".", 1)[-1]) + r"\s*[>,]", text):
            continue
        if line not in text:
            additions.append(line)

    formats = [f for f in (plan.get("view_formats") or [])]
    if formats and "ViewLocationFormats" not in text:
        additions.append(
            "builder.Services.Configure<Microsoft.AspNetCore.Mvc.Razor."
            "RazorViewEngineOptions>(options =>")
        additions.append("{")
        for fmt in formats:
            additions.append(f'    options.ViewLocationFormats.Add("{fmt}");')
        additions.append('    options.ViewLocationFormats.Add("/Views/Shared/{0}.cshtml");')
        additions.append("});")

    if additions:
        block = "\n".join(additions)
        if MARKER in text:
            text = text.replace(MARKER, block)
        else:
            # A Program.cs the scaffold did not write (or one already corrected once). Put the
            # registrations immediately before the build call rather than giving up — after
            # `Build()` they would throw, and dropping them silently is the bug this fixes.
            anchor = "var app = builder.Build();"
            if anchor not in text:
                logger.warning("Program.cs has no registration point — host wiring skipped")
                return content
            text = text.replace(anchor, block + "\n\n" + anchor, 1)

    # `UseSession` is ordering-sensitive: it has to sit after UseRouting and before the
    # endpoints, so it is placed by anchor rather than appended with the registrations.
    if plan.get("needs_session") and "UseSession()" not in text and "app.UseRouting();" in text:
        text = text.replace("app.UseRouting();", "app.UseRouting();\napp.UseSession();", 1)
    return text


def apply_to_appsettings(content: str, name: str = CONNECTION_NAME) -> str:
    """Give the connection string a declared home, with **no value** in it.

    `GetConnectionString("DefaultConnection")` returns null against an `appsettings.json` that
    has never heard of it, and EF then throws a message about the provider rather than about
    the missing setting — a false trail for whoever has to run this. Declaring the key empty
    makes the gap name itself, and `ConnectionStrings__DefaultConnection` in the environment
    overrides it without editing a committed file.

    The value stays empty on purpose. The charter this product ships says secrets come only
    from the environment or a secret store, "never a committed default in appsettings", and a
    connection string is a secret — so writing a plausible one would be the pipeline breaking
    the customer's own stated rule to make a demo start.
    """
    text = content or ""
    if not text.strip() or "ConnectionStrings" in text:
        return text
    try:
        settings = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("appsettings.json is not valid JSON — connection string not declared")
        return text
    if not isinstance(settings, dict):
        return text
    # First key, so a reader opening the file sees the one setting they have to supply.
    merged = {"ConnectionStrings": {name: ""}}
    merged.update(settings)
    return json.dumps(merged, indent=2) + "\n"


def packages_for_code(content: str) -> Set[Tuple[str, str]]:
    """Packages the code in `content` proves it needs. Comments and strings are stripped
    first — a `// TODO: use Dapper here` must not add a dependency."""
    code = usings.strip_non_code(content or "")
    if not code.strip():
        return set()
    return {(package, version) for pattern, package, version in _PACKAGE_TYPES
            if re.search(pattern, code)}


def _project_of(path: str, projects: List[str]) -> str:
    """Which project an artifact belongs to, by path prefix. Longest match wins so that
    `Shop.Web.Admin/x.cs` is not claimed by `Shop.Web`."""
    normalised = (path or "").replace("\\", "/")
    best = ""
    for project in projects:
        prefix = f"{project}/"
        if normalised.startswith(prefix) and len(project) > len(best):
            best = project
    return best


def existing_references(artifacts: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """project -> the projects its shipped `.csproj` already references.

    The same parse `reconcile._project_reference_cycles` does, because these two must agree
    about what the build graph is: one of them decides the references and the other reports
    when they cannot build, and a disagreement between them would be a check that passes on a
    graph the compiler refuses.
    """
    edges: Dict[str, Set[str]] = {}
    for artifact in artifacts:
        path = (artifact.get("path") or "").replace("\\", "/")
        if not path.lower().endswith(".csproj"):
            continue
        name = path.rsplit("/", 1)[-1][: -len(".csproj")]
        targets = {t for t in _PROJECT_REF.findall(artifact.get("content") or "") if t != name}
        edges[name] = targets
    return edges


# Names that mark an implementation as one nothing should be wired to by default. A converter
# writes these when the source had a test double or a stub in it, and preferring one over the
# real implementation would produce an application that runs and does nothing.
_NOT_FOR_PRODUCTION = re.compile(r"(?i)\b|^(?:fake|stub|mock|dummy|null|noop|inmemory|test)",
                                 re.IGNORECASE)
_DOUBLE_MARKERS = ("fake", "stub", "mock", "dummy", "noop", "inmemory", "test")


def _preferred_implementation(interface: str, found: List[Tuple[str, str]]):
    """-> the one implementation to register, or None when the choice is a genuine tie.

    Two discriminators, both facts about the names rather than judgements:

    1. **The conventional pairing.** `IEmployeeRepository` is implemented by
       `EmployeeRepository` in every codebase this product converts into; a
       `SqlEmployeeRepository` beside it is the variant, not the default.
    2. **Test doubles are not implementations.** A `FakeEmployeeRepository` in the export is
       something the source had, and registering it produces an application that starts,
       serves, and does nothing — the worst outcome available, because it looks like success.

    Returning `None` is the honest answer when neither applies: two equally plausible classes
    are a decision, and the caller turns that into one question rather than a silent omission.
    """
    real = [(cls, ns) for cls, ns in found
            if not any(marker in cls.lower() for marker in _DOUBLE_MARKERS)]
    # Only when it *discriminates*. If everything looks like a double, the source is a test
    # project and dropping them all would leave the interface unregistered for a worse reason.
    candidates = real if real and len(real) < len(found) else found
    if len(candidates) == 1:
        return candidates[0]

    conventional = interface[1:] if interface[:1] == "I" and len(interface) > 1 else ""
    exact = [(cls, ns) for cls, ns in candidates if cls == conventional]
    if len(exact) == 1:
        return exact[0]
    return None


def resolve_reference_cycles(existing: Dict[str, Set[str]], needed: Dict[str, Set[str]],
                             entry_project: str = "",
                             weights: Optional[Dict[Tuple[str, str], int]] = None
                             ) -> Tuple[Dict[str, Set[str]], List[str]]:
    """Keep the shipped project graph acyclic. -> ({project: references to drop}, problems).

    `architecture.normalize_projects` (D-93) makes the *proposed* graph acyclic before the
    scaffold writes a single `.csproj`. This is the other end of the same problem, and it was
    missing: `plan` derives references from the types the converted code actually names, and
    adding those on top of the architect's predicted edges can close a cycle that was not
    there before. MSBuild refuses a reference cycle outright, so `dotnet build` fails during
    *restore* with MSB4006 — before compiling a line, and naming a target rather than a file.
    Observed on a 45-file run: the architect predicted `Infrastructure -> Application`, the
    converted services genuinely used Infrastructure types, and the export could not restore.

    **An unjustified edge is cut first, and freely.** It is a prediction the code did not bear
    out, so dropping it costs nothing.

    **A justified edge is cut only as a last resort, and then the thinnest one.** D-93 warns
    that cutting a real edge turns one build-graph error into a CS0246 per use, which is worse
    and further from its cause — and that is right when the edge carries a layer's worth of
    traffic. It is wrong when the edge carries one file. Measured on a real run: `Application`
    and `Infrastructure` referenced each other, and the whole of the second direction was one
    seeder calling `IAuthService`. Refusing to choose left a solution that could not `restore`,
    and MSB4006 names a *project*, so `build_results` had nothing to reopen and the fix loop
    could not even start. Cutting the thin edge yields one CS0246 in one file — which names a
    `.cs`, reopens exactly that unit, and arrives in its next prompt. A compile error the loop
    can consume beats a restore failure that stops everything.

    `weights` carries how many files justify each edge, so "thinnest" is measured rather than
    guessed. An edge with no entry is unjustified and goes first.
    """
    graph_edges: Dict[str, Set[str]] = {}
    for project in set(existing) | set(needed):
        graph_edges[project] = set(existing.get(project) or ()) | set(needed.get(project) or ())
    # Only edges between projects we actually have; a reference to something outside the
    # export cannot take part in a cycle inside it.
    known = set(graph_edges)
    graph_edges = {p: (t & known) for p, t in graph_edges.items()}

    removed: Dict[str, Set[str]] = {}
    problems: List[str] = []
    # One edge per pass, re-running the SCC search each time: cutting greedily inside a group
    # removes edges a later pass would have found unnecessary to touch (D-93).
    for _ in range(len(known) + 1):
        groups = [g for g in graph.strongly_connected(graph_edges) if len(g) > 1]
        if not groups:
            break
        group = sorted(groups, key=lambda g: (len(g), sorted(g)))[0]
        members = set(group)
        edges_here = [(source, target)
                      for source in sorted(members)
                      for target in sorted(graph_edges[source] & members)]

        def _weight(edge: Tuple[str, str]) -> int:
            """How much real code an edge carries. 0 means none — a prediction, free to cut.

            Derived from `needed` rather than from `weights` alone, so a caller that supplies
            no weights still gets the distinction that matters most: `needed` already says
            which edges the code justified, and losing that would cut a real edge in
            preference to a phantom one.
            """
            source, target = edge
            if target not in (needed.get(source) or set()):
                return 0
            return max(1, (weights or {}).get(edge, 1))

        # Unjustified first; then the edge the fewest files depend on; then the one pointing at
        # the host, since a lower layer referencing the application that hosts it is the shape
        # that is always wrong; then by name, so the answer is deterministic.
        source, target = sorted(
            edges_here, key=lambda e: (_weight(e), e[1] != entry_project, e[0], e[1]))[0]
        carried = _weight((source, target))
        graph_edges[source].discard(target)
        removed.setdefault(source, set()).add(target)
        if not carried:
            problems.append(
                f"project '{source}' referenced '{target}', which formed a reference cycle "
                f"MSBuild refuses to build; removed it, since no converted file in '{source}' "
                f"names a type from '{target}'")
        else:
            problems.append(
                f"projects {' and '.join(sorted(members))} reference each other, which MSBuild "
                f"will not build. Every reference in that cycle is backed by real code, so this "
                f"is a layering problem in how the components were split — but a solution that "
                f"cannot restore blocks everything, so the thinnest edge was cut: "
                f"'{source}' -> '{target}', which {carried} file(s) rely on. Expect CS0246 in "
                f"those file(s); each names a file, so the build loop reopens and re-converts "
                f"it. The durable fix is to move the shared type down a layer.")
    return removed, problems


def plan(artifacts: List[Dict[str, Any]], symbol_namespaces: Dict[str, str],
         namespace_to_project: Dict[str, str], entry_project: str = "") -> Dict[str, Any]:
    """Work out what each project file is missing. Pure — no database, no I/O.

    `namespace_to_project` maps a component's namespace to the project it lives in, which is
    how a cross-namespace type reference becomes a `ProjectReference` edge.
    """
    projects = sorted({p for p in namespace_to_project.values() if p})
    needed_packages: Dict[str, Set[Tuple[str, str]]] = {p: set() for p in projects}
    needed_projects: Dict[str, Set[str]] = {p: set() for p in projects}
    # How many *files* justify each edge. Counted rather than inferred, because it is what
    # decides which edge to cut when a cycle is real on both sides: an edge carrying one file
    # and an edge carrying a layer are not the same thing to break.
    carried: Dict[Tuple[str, str], Set[str]] = {}

    for artifact in artifacts:
        path = artifact.get("path") or ""
        if not path.lower().endswith(".cs"):
            continue
        owner = _project_of(path, projects)
        if not owner:
            continue
        content = artifact.get("content") or ""
        needed_packages[owner] |= packages_for_code(content)

        # Every generated type this file names, resolved to the project that declares it.
        # `referenced_types` reads the code rather than the model's `references` list, which
        # is the same reason `usings` does: the list is optional and often incomplete.
        for name in usings.referenced_types(content, symbol_namespaces.keys()):
            namespace = symbol_namespaces.get(name) or ""
            target = namespace_to_project.get(namespace) or ""
            if target and target != owner:
                needed_projects[owner].add(target)
                carried.setdefault((owner, target), set()).add(path)

    removed, problems = resolve_reference_cycles(
        existing_references(artifacts), needed_projects, entry_project,
        {edge: len(paths) for edge, paths in carried.items()})
    return {"packages": {p: sorted(v) for p, v in needed_packages.items() if v},
            "projects": {p: sorted(v) for p, v in needed_projects.items() if v},
            "remove": {p: sorted(v) for p, v in removed.items() if v},
            "problems": problems}


def apply_to_csproj(content: str, packages: List[Tuple[str, str]],
                    project_refs: List[str], remove: Sequence[str] = ()) -> str:
    """Add whatever the `.csproj` is missing, and drop only what `remove` names.

    Removing an unused `PackageReference` would be the tidy thing to do and is not safe: a
    package can be needed at runtime by a type that never appears in source (a provider
    registered by configuration, an analyzer), and dropping it turns a working build into a
    runtime failure — the one direction this pipeline must not move in. The same argument
    holds for a `ProjectReference`, which is why nothing is removed on the strength of being
    unused either.

    `remove` is the one exception, and it is narrow: `resolve_reference_cycles` names an
    edge only when keeping it means MSBuild will not build the solution at all. A reference
    that might be unnecessary is a smaller problem than a solution that cannot restore.
    """
    drop = {r for r in remove or ()}
    if drop:
        content = "".join(
            line for line in (content or "").splitlines(True)
            if not ("<ProjectReference" in line
                    and set(_PROJECT_REF.findall(line)) & drop))
    have_packages = set(_PACKAGE_REF.findall(content or ""))
    have_projects = set(_PROJECT_REF.findall(content or ""))
    add_packages = [(p, v) for p, v in packages if p not in have_packages]
    add_projects = [r for r in project_refs if r not in have_projects and r not in drop]
    if not add_packages and not add_projects:
        return content

    block: List[str] = []
    if add_packages:
        block.append("  <ItemGroup>")
        block.extend(f'    <PackageReference Include="{p}" Version="{v}" />'
                     for p, v in add_packages)
        block.append("  </ItemGroup>")
    if add_projects:
        block.append("  <ItemGroup>")
        block.extend(f'    <ProjectReference Include="..\\{r}\\{r}.csproj" />'
                     for r in add_projects)
        block.append("  </ItemGroup>")

    closing = content.rfind("</Project>")
    if closing < 0:
        # Not a project file we recognise. Returning it untouched is right: an appended
        # ItemGroup in something that is not a `.csproj` would corrupt it.
        logger.warning("Skipped a project file with no closing </Project> element")
        return content
    return content[:closing] + "\n".join(block) + "\n\n" + content[closing:]


def csproj_for(project_name: str, charter: Dict[str, Any], is_entry: bool,
               depends_on: List[str]) -> str:
    """A project file from scratch, for a project the scaffold never wrote one for.

    Delegates to the scaffold's own emitter so there is exactly one `.csproj` shape in the
    codebase — two would drift the first time a property was added to either.
    """
    return dotnet_scaffold._csproj(
        {"name": project_name, "depends_on": depends_on}, charter, is_entry)
