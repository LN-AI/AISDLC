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
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

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


def host_plan(artifacts: List[Dict[str, Any]], entry_project: str) -> Dict[str, Any]:
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
        controllers.update(_CONTROLLER.findall(code))
        if re.search(r"\bISession\b|\.Session\b", code):
            needs_session = True
        if "IHttpContextAccessor" in code:
            needs_http_context = True

    services: List[str] = []
    ambiguous: List[str] = []
    for interface in sorted(interfaces):
        found = impls.get(interface) or []
        if len(found) != 1:
            # Zero means the interface is implemented outside the conversion (or not at all —
            # reconciliation already reports that). More than one means the container cannot
            # be told which to use without a decision this stage is not entitled to make.
            if len(found) > 1:
                ambiguous.append(f"{interface} ({', '.join(c for c, _ in found)})")
            continue
        cls, impl_ns = found[0]
        iface_ns = interfaces[interface]
        lhs = f"{iface_ns}.{interface}" if iface_ns else interface
        rhs = f"{impl_ns}.{cls}" if impl_ns else cls
        services.append(f"builder.Services.AddScoped<{lhs}, {rhs}>();")

    return {
        "services": services,
        "ambiguous": ambiguous,
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
