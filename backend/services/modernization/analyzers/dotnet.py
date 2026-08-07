""".NET (C# / VB.NET) — `namespace` + `using`.

Weaker than the JVM case, and knowingly so: C# resolves types through the namespace, not
through the file, so a `using` names a *namespace* that many files contribute to. The
resolver handles that correctly — one `using` produces an edge to every file declaring
that namespace — which over-constrains the order slightly (a file may wait on a namespace
sibling it never touches) and never under-constrains it. Waiting too long is a scheduling
inefficiency; converting too early is a correctness bug.
"""
import re
from typing import List

from backend.services.modernization.analyzers import Symbol, UnitFacts, register
from backend.services.modernization.analyzers.generic import classify, language_of
from backend.services.modernization.analyzers.java import strip_comments

_NAMESPACE = re.compile(r"^\s*(?:Namespace\s+|namespace\s+)([\w.]+)", re.M)
_USING = re.compile(r"^\s*(?:using|Imports)\s+(?:static\s+)?([\w.]+)\s*;?\s*$", re.M)
# `using X = Y;` is an alias, not a dependency on X — the real dependency is Y.
_USING_ALIAS = re.compile(r"^\s*using\s+\w+\s*=\s*([\w.]+)\s*;", re.M)
# See the note on java._TYPE: `namespace X { public class Y { } }` on one line is ordinary
# C#, and anchoring only to the line start loses Y entirely.
_TYPE = re.compile(
    r"(?:^|[{};])\s*(?:public\s+|internal\s+|private\s+|protected\s+|static\s+|sealed\s+|"
    r"abstract\s+|partial\s+|Public\s+|Friend\s+|NotInheritable\s+|MustInherit\s+)*"
    r"(class|interface|struct|enum|record|Class|Interface|Structure|Enum|Module)\s+(\w+)([^\{\r\n]*)",
    re.M,
)


def analyze(path: str, content: str) -> UnitFacts:
    if path.lower().endswith((".csproj", ".vbproj", ".sln")):
        # Project files are carried across as configuration, never converted as code, and
        # their <ProjectReference> edges describe assemblies rather than source files.
        return UnitFacts(path=path, language="MSBuild", module_type="config",
                         source="parser", confidence=1.0)

    code = strip_comments(content)
    namespaces = _NAMESPACE.findall(code)
    namespace = namespaces[0] if namespaces else ""

    exports: List[Symbol] = []
    provides: List[str] = list(dict.fromkeys(namespaces))
    for kind, name, tail in _TYPE.findall(code):
        exports.append(Symbol(name, kind.lower(), f"{kind} {name}{tail.strip()}".strip()))
        provides.append(f"{namespace}.{name}" if namespace else name)

    requires = set(_USING.findall(code)) | set(_USING_ALIAS.findall(code))
    # BCL namespaces are not in this repository and would only add noise to the graph.
    requires = {r for r in requires if not r.split(".")[0] in ("System", "Microsoft")}

    return UnitFacts(
        path=path,
        language=language_of(path) or "C#",
        module_type=classify(path),
        provides=sorted(set(provides)),
        requires=sorted(requires),
        exports=exports,
        source="parser",
        confidence=0.8,
    )


register({".cs", ".vb", ".csproj", ".vbproj", ".sln"}, analyze)
