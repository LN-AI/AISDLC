"""The .NET 8 skeleton: solution, projects, host, configuration.

Package versions are pinned constants rather than "latest", for the same reason the litellm
pin exists: a build that resolves differently on different days is not reproducible, and the
customer's CI is where this gets built. Every version here is a real published one; when
they age, they age visibly in one file rather than invisibly across five hundred generated
`.csproj` fragments.

What decides the package set is the **charter**, not a guess — `data_access` mentioning EF
Core is what puts EF Core in the file. That is the same argument the charter itself makes:
the decision was taken once, by the team that owns the codebase, and every artifact in the
run has to reflect the same one.
"""
import re
from typing import Any, Dict, List

from backend.services.modernization.scaffold import register

TARGET_FRAMEWORK = "net8.0"
LANG_VERSION = "latest"

# (charter field, regex over its value, package id, version)
_CONDITIONAL_PACKAGES = (
    ("data_access", r"(?i)\bef\s*core\b|entity\s*framework",
     "Microsoft.EntityFrameworkCore", "8.0.10"),
    ("data_access", r"(?i)\bsql\s*server\b|\bmssql\b",
     "Microsoft.EntityFrameworkCore.SqlServer", "8.0.10"),
    ("data_access", r"(?i)\bmysql\b|\bmariadb\b", "Pomelo.EntityFrameworkCore.MySql", "8.0.2"),
    ("data_access", r"(?i)\bpostgres", "Npgsql.EntityFrameworkCore.PostgreSQL", "8.0.10"),
    ("data_access", r"(?i)\bdapper\b", "Dapper", "2.1.35"),
    ("logging", r"(?i)\bserilog\b", "Serilog.AspNetCore", "8.0.3"),
    ("ui_strategy", r"(?i)\bblazor\b", "Microsoft.AspNetCore.Components.Web", "8.0.10"),
)

_TEST_PACKAGES = (
    ("xunit", "2.9.2"),
    ("xunit.runner.visualstudio", "2.8.2"),
    ("Microsoft.NET.Test.Sdk", "17.11.1"),
)

_WEB_HINT = re.compile(r"(?i)asp\.?net|web\s*api|mvc|razor|blazor|minimal\s*api")


def _is_web(charter: Dict[str, Any]) -> bool:
    blob = " ".join(str(charter.get(k) or "") for k in ("target_stack", "project_layout",
                                                        "ui_strategy"))
    return bool(_WEB_HINT.search(blob))


def _packages_for(charter: Dict[str, Any]) -> List[tuple]:
    out: List[tuple] = []
    seen = set()
    for field, pattern, package, version in _CONDITIONAL_PACKAGES:
        if package in seen:
            continue
        if re.search(pattern, str(charter.get(field) or "")):
            out.append((package, version))
            seen.add(package)
    return out


def _csproj(project: Dict[str, Any], charter: Dict[str, Any], is_entry: bool) -> str:
    sdk = "Microsoft.NET.Sdk.Web" if (is_entry and _is_web(charter)) else "Microsoft.NET.Sdk"
    lines = [
        f'<Project Sdk="{sdk}">',
        "",
        "  <PropertyGroup>",
        f"    <TargetFramework>{TARGET_FRAMEWORK}</TargetFramework>",
        f"    <LangVersion>{LANG_VERSION}</LangVersion>",
        "    <Nullable>enable</Nullable>",
        "    <ImplicitUsings>enable</ImplicitUsings>",
        f"    <RootNamespace>{project['name']}</RootNamespace>",
        # Warnings, not errors: the first build of a converted codebase should report every
        # problem at once, not stop at the first one.
        "    <TreatWarningsAsErrors>false</TreatWarningsAsErrors>",
        "  </PropertyGroup>",
        "",
    ]
    packages = _packages_for(charter) if is_entry else []
    if packages:
        lines.append("  <ItemGroup>")
        for package, version in packages:
            lines.append(f'    <PackageReference Include="{package}" Version="{version}" />')
        lines.append("  </ItemGroup>")
        lines.append("")
    references = [r for r in (project.get("depends_on") or []) if r]
    if references:
        lines.append("  <ItemGroup>")
        for reference in references:
            lines.append(f'    <ProjectReference Include="..\\{reference}\\{reference}.csproj" />')
        lines.append("  </ItemGroup>")
        lines.append("")
    lines.append("</Project>")
    return "\n".join(lines) + "\n"


def _solution(projects: List[Dict[str, Any]]) -> str:
    """A format-12 .sln. GUIDs are derived from the project name, not random — a scaffold
    regenerated after a re-run must not produce a different solution."""
    import hashlib

    type_guid = "9A19103F-16F7-4668-BE54-9A1E7A4F7556"     # SDK-style C# project
    lines = [
        "Microsoft Visual Studio Solution File, Format Version 12.00",
        "# Visual Studio Version 17",
    ]
    entries = []
    for project in projects:
        name = project["name"]
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest().upper()
        guid = f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
        entries.append((name, guid))
        lines.append(
            f'Project("{{{type_guid}}}") = "{name}", "{name}\\{name}.csproj", "{{{guid}}}"')
        lines.append("EndProject")
    lines.append("Global")
    lines.append("\tGlobalSection(SolutionConfigurationPlatforms) = preSolution")
    lines.append("\t\tDebug|Any CPU = Debug|Any CPU")
    lines.append("\t\tRelease|Any CPU = Release|Any CPU")
    lines.append("\tEndGlobalSection")
    lines.append("\tGlobalSection(ProjectConfigurationPlatforms) = postSolution")
    for _, guid in entries:
        for configuration in ("Debug", "Release"):
            lines.append(f"\t\t{{{guid}}}.{configuration}|Any CPU.ActiveCfg = "
                         f"{configuration}|Any CPU")
            lines.append(f"\t\t{{{guid}}}.{configuration}|Any CPU.Build.0 = "
                         f"{configuration}|Any CPU")
    lines.append("\tEndGlobalSection")
    lines.append("EndGlobal")
    return "\n".join(lines) + "\n"


def _program_cs(entry: Dict[str, Any], charter: Dict[str, Any]) -> str:
    web = _is_web(charter)
    error_handling = str(charter.get("error_handling") or "")
    wants_middleware = bool(re.search(r"(?i)middleware|global\s+exception", error_handling))
    if not web:
        return (
            f"namespace {entry['name']};\n"
            "\n"
            "// Entry point. The converted code is registered here; nothing in this file was\n"
            "// generated by a model.\n"
            "public static class Program\n"
            "{\n"
            "    public static void Main(string[] args)\n"
            "    {\n"
            "        // TODO: wire up the converted entry point.\n"
            "    }\n"
            "}\n"
        )
    lines = [
        "var builder = WebApplication.CreateBuilder(args);",
        "",
        "builder.Services.AddControllersWithViews();",
    ]
    # Every charter value, not a hand-picked three. The old list was `config_strategy`,
    # `constraints`, `ui_strategy` — and a PHP run that answered "records live in $_SESSION"
    # under `data_access`/`datastore_fate`/`auth_model` scaffolded no session at all, so the
    # app threw on its first request. Which field someone writes it in is not a signal.
    # `wireup` re-checks this against the converted code afterwards, so a miss here is now
    # corrected rather than fatal; scanning everything makes the scaffold right more often.
    if re.search(r"(?i)\bsessions?\b", " ".join(str(v or "") for v in charter.values())):
        # Session is registered exactly once, here. A per-file converter emits
        # `AddSession()` in every file that touched `$_SESSION`, which is the single most
        # common way a converted PHP app fails to start.
        lines += ["builder.Services.AddDistributedMemoryCache();",
                  "builder.Services.AddSession();"]
    lines += [
        "",
        "// Registrations for converted services are added here as components are converted.",
        "",
        "var app = builder.Build();",
        "",
    ]
    if wants_middleware:
        lines += ["app.UseExceptionHandler(\"/Home/Error\");", ""]
    lines += [
        "app.UseStaticFiles();",
        "app.UseRouting();",
    ]
    if "AddSession();" in "\n".join(lines):
        lines.append("app.UseSession();")
    lines += [
        "app.MapControllerRoute(name: \"default\", pattern: "
        "\"{controller=Home}/{action=Index}/{id?}\");",
        "",
        "app.Run();",
        "",
    ]
    return "\n".join(lines)


def _appsettings(config_keys: List[str]) -> str:
    import json

    settings: Dict[str, Any] = {
        "Logging": {"LogLevel": {"Default": "Information",
                                 "Microsoft.AspNetCore": "Warning"}},
        "AllowedHosts": "*",
    }
    if config_keys:
        # One key per redaction category. The value is a marker, never a plausible-looking
        # placeholder — a fake connection string that looks real is how one gets committed.
        settings["ConvertedSecrets"] = {
            key: f"SET-ME: supplied at ingest as __REDACTED_{key.upper()}__"
            for key in sorted(set(config_keys))
        }
    return json.dumps(settings, indent=2) + "\n"


def _readme(charter: Dict[str, Any], projects: List[Dict[str, Any]],
            config_keys: List[str]) -> str:
    lines = [
        "# Converted output",
        "",
        f"Target stack: **{charter.get('target_stack') or 'unspecified'}**",
        "",
        "This skeleton was generated deterministically — no model wrote the solution, the "
        "project files, the host or the configuration. The converted source that fills it "
        "was generated by a model and **has not been compiled here**: this platform runs no "
        "builds. Build it in your own CI before treating any of it as done.",
        "",
        "## Projects",
        "",
    ]
    for project in projects:
        lines.append(f"- `{project['name']}` — {project.get('purpose') or 'converted code'}")
    if config_keys:
        lines += [
            "",
            "## Configuration you must supply",
            "",
            "Each of these was a credential in the source. The value was removed before any "
            "model saw the file and was never stored; only the fact that one was there is "
            "recorded.",
            "",
        ]
        for key in sorted(set(config_keys)):
            lines.append(f"- `ConvertedSecrets:{key}`")
    lines += ["", "## Building", "", "```", "dotnet restore", "dotnet build", "```", ""]
    return "\n".join(lines)


def build_dotnet(charter: Dict[str, Any], projects: List[Dict[str, Any]],
                 config_keys: List[str]) -> List[Dict[str, str]]:
    if not projects:
        projects = [{"name": "Converted", "purpose": "converted code"}]
    entry = projects[0]
    files: List[Dict[str, str]] = []

    files.append({"path": f"{entry['name']}.sln", "content": _solution(projects),
                  "purpose": "Solution file referencing every generated project."})
    for project in projects:
        files.append({
            "path": f"{project['name']}/{project['name']}.csproj",
            "content": _csproj(project, charter, is_entry=(project is entry)),
            "purpose": f"Project file for {project['name']}.",
        })
    files.append({"path": f"{entry['name']}/Program.cs",
                  "content": _program_cs(entry, charter),
                  "purpose": "Application host and service registration."})
    files.append({"path": f"{entry['name']}/appsettings.json",
                  "content": _appsettings(config_keys),
                  "purpose": "Configuration, with one key per redacted value."})
    files.append({
        "path": "Directory.Build.props",
        "content": ('<Project>\n'
                    '  <PropertyGroup>\n'
                    f'    <TargetFramework>{TARGET_FRAMEWORK}</TargetFramework>\n'
                    '    <Nullable>enable</Nullable>\n'
                    '    <ImplicitUsings>enable</ImplicitUsings>\n'
                    '    <GenerateDocumentationFile>false</GenerateDocumentationFile>\n'
                    '  </PropertyGroup>\n'
                    '</Project>\n'),
        "purpose": "Settings shared by every project, so they cannot drift apart.",
    })
    files.append({"path": "global.json",
                  # `latestMajor`, not `latestFeature`. The stricter-looking value is the worse
                  # one: `latestFeature` rolls forward only *within* 8.0.x, so a machine with
                  # any newer SDK and no 8.0 band installed fails at SDK resolution — before a
                  # single line of generated C# is read — with "A compatible .NET SDK was not
                  # found". That reads as "the conversion produced broken code" when nothing
                  # has been compiled at all. `latestMajor` keeps 8.0.400 as a floor and lets
                  # any later SDK satisfy it; a net8.0 target builds fine on SDK 9 and 10.
                  "content": '{\n  "sdk": {\n    "version": "8.0.400",\n'
                             '    "rollForward": "latestMajor"\n  }\n}\n',
                  "purpose": "Sets an SDK floor without excluding newer SDKs."})
    files.append({"path": ".gitignore",
                  "content": "bin/\nobj/\n.vs/\n*.user\n*.suo\nTestResults/\n",
                  "purpose": "Standard .NET ignores."})
    files.append({"path": "README.md",
                  "content": _readme(charter, projects, config_keys),
                  "purpose": "What this is, and what still has to be done to it."})
    return files


# Keys are matched as substrings of the normalised stack string, longest first — so they
# must be specific enough not to appear inside an unrelated stack name. Nothing shorter than
# four characters: `"c"` would have matched `cobol`, `oracle` and most of the others.
register(("aspnetcore", "aspnet", "dotnet8", "dotnet9", "dotnet", "netcore",
          "net80", "net8", "net90", "net9", "csharp", "vbnet"), build_dotnet)
