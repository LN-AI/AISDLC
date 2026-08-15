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
from typing import Any, Dict, List, Set, Tuple

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


def plan(artifacts: List[Dict[str, Any]], symbol_namespaces: Dict[str, str],
         namespace_to_project: Dict[str, str]) -> Dict[str, Any]:
    """Work out what each project file is missing. Pure — no database, no I/O.

    `namespace_to_project` maps a component's namespace to the project it lives in, which is
    how a cross-namespace type reference becomes a `ProjectReference` edge.
    """
    projects = sorted({p for p in namespace_to_project.values() if p})
    needed_packages: Dict[str, Set[Tuple[str, str]]] = {p: set() for p in projects}
    needed_projects: Dict[str, Set[str]] = {p: set() for p in projects}

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

    return {"packages": {p: sorted(v) for p, v in needed_packages.items() if v},
            "projects": {p: sorted(v) for p, v in needed_projects.items() if v}}


def apply_to_csproj(content: str, packages: List[Tuple[str, str]],
                    project_refs: List[str]) -> str:
    """Add whatever the `.csproj` is missing. Never removes anything.

    Removing an unused `PackageReference` would be the tidy thing to do and is not safe: a
    package can be needed at runtime by a type that never appears in source (a provider
    registered by configuration, an analyzer), and dropping it turns a working build into a
    runtime failure — the one direction this pipeline must not move in.
    """
    have_packages = set(_PACKAGE_REF.findall(content or ""))
    have_projects = set(_PROJECT_REF.findall(content or ""))
    add_packages = [(p, v) for p, v in packages if p not in have_packages]
    add_projects = [r for r in project_refs if r not in have_projects]
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
