"""Java / Kotlin / Scala — `package` + `import` is the whole dependency story.

No grammar needed: a JVM file declares exactly one package, imports by fully-qualified
name, and the compiler already forces those names to be accurate. Comments are stripped
first so a commented-out import does not create an edge that reorders the run.
"""
import re
from typing import List

from backend.services.modernization.analyzers import Symbol, UnitFacts, register
from backend.services.modernization.analyzers.generic import classify, language_of

_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;?", re.M)
_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;?", re.M)
# Anchored on a line start OR a brace/semicolon rather than `^` alone: a type declared
# inside a namespace/class body on the same line as its opening brace is common enough
# that anchoring only to the line start silently loses it — and a lost `provides` entry
# means a lost edge, which means a wrong conversion order.
_TYPE = re.compile(
    r"(?:^|[{};])\s*(?:public\s+|final\s+|abstract\s+|sealed\s+|open\s+|data\s+|case\s+|"
    r"static\s+|private\s+|protected\s+)*"
    r"(class|interface|enum|record|object|trait)\s+(\w+)([^\{\n]*)",
    re.M,
)

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def strip_comments(text: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def analyze(path: str, content: str) -> UnitFacts:
    code = strip_comments(content)
    package_match = _PACKAGE.search(code)
    package = package_match.group(1) if package_match else ""

    exports: List[Symbol] = []
    provides: List[str] = [package] if package else []
    for kind, name, tail in _TYPE.findall(code):
        exports.append(Symbol(name, kind, f"{kind} {name}{tail.strip()}".strip()))
        provides.append(f"{package}.{name}" if package else name)

    # A wildcard import (`import a.b.*`) is captured as the package itself, which the
    # resolver matches against every file declaring that package — exactly the set of
    # files the wildcard actually pulls in.
    requires = sorted({name for name in _IMPORT.findall(code)})

    return UnitFacts(
        path=path,
        language=language_of(path) or "Java",
        module_type=classify(path),
        provides=sorted(set(provides)),
        requires=requires,
        exports=exports,
        source="parser",
        confidence=0.9,
    )


register({".java", ".kt", ".scala"}, analyze)
