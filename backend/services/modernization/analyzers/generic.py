"""Fallback analyzer, plus the path heuristics every analyzer shares.

A file with no analyzer produces no edges. That is the safe wrong answer: it lands in
level 0 and converts first, with nothing assumed about what it can see. The dangerous
wrong answer would be inventing an edge that puts it late and promises it context that
never arrives.
"""
import os
import re
from typing import Dict

from backend.services.modernization.analyzers import UnitFacts, register

# Extension -> the language label shown in the UI and used to pick charter defaults.
LANGUAGE_BY_EXT: Dict[str, str] = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".jsx": "JavaScript",
    ".tsx": "TypeScript", ".java": "Java", ".go": "Go", ".rb": "Ruby", ".php": "PHP",
    ".cs": "C#", ".vb": "VB.NET", ".kt": "Kotlin", ".swift": "Swift", ".scala": "Scala",
    ".rs": "Rust", ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cc": "C++",
    ".pas": "Delphi/Pascal", ".dpr": "Delphi/Pascal", ".dfm": "Delphi Form",
    ".dpk": "Delphi/Pascal", ".inc": "Delphi/Pascal",
    ".cob": "COBOL", ".cbl": "COBOL", ".cpy": "COBOL Copybook", ".jcl": "JCL",
    ".pl": "Perl", ".pm": "Perl", ".f": "Fortran", ".for": "Fortran", ".f90": "Fortran",
    ".asp": "Classic ASP", ".cfm": "ColdFusion", ".jsp": "JSP", ".vbs": "VBScript",
    ".bas": "Visual Basic", ".cls": "Visual Basic",
    ".pls": "PL/SQL", ".pks": "PL/SQL", ".pkb": "PL/SQL", ".prc": "PL/SQL", ".sql": "SQL",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".xml": "XML", ".ini": "INI",
    ".cfg": "Config", ".conf": "Config", ".toml": "TOML", ".properties": "Properties",
    ".csproj": "MSBuild", ".vbproj": "MSBuild", ".sln": "MSBuild",
    ".gradle": "Gradle", ".pom": "Maven",
}

_CONFIG_EXTS = {".json", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".conf", ".toml",
                ".properties", ".csproj", ".vbproj", ".sln", ".gradle", ".pom"}

# Classified from the path, in priority order — first match wins. Path is a weaker signal
# than syntax, so an analyzer that can tell from the code should say so instead of relying
# on this; `module_type` drives what the charter's conventions apply to, not the ordering.
_PATH_RULES = [
    ("test", re.compile(r"(^|/)(tests?|spec|specs|__tests__|fixtures?|mocks?)(/|$)"
                        r"|(^|/)test_[^/]*$|_(test|spec)\.[^/.]+$", re.I)),
    ("ui", re.compile(r"(^|/)(ui|views?|forms?|pages?|components?|screens?|templates?|"
                      r"controllers?|www|web|frontend)(/|$)", re.I)),
    ("data", re.compile(r"(^|/)(models?|entities|entity|dao|repositor(y|ies)|schema|db|"
                        r"database|migrations?|persistence)(/|$)", re.I)),
    ("business", re.compile(r"(^|/)(services?|domain|business|core|logic|handlers?|"
                            r"usecases?)(/|$)", re.I)),
    ("config", re.compile(r"(^|/)(config|configuration|settings|conf|deploy|infra)(/|$)", re.I)),
]

_ENTRYPOINT_NAMES = {"main", "program", "app", "index", "startup", "__main__", "server"}


def classify(path: str) -> str:
    """Best guess at what role this file plays. Never authoritative — it is a label a human
    reads and a hint the charter can key conventions off, not an input to ordering."""
    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    for module_type, pattern in _PATH_RULES:
        if pattern.search(path):
            return module_type
    if ext in _CONFIG_EXTS:
        return "config"
    if stem in _ENTRYPOINT_NAMES:
        return "entrypoint"
    return "unknown"


def language_of(path: str) -> str:
    return LANGUAGE_BY_EXT.get(os.path.splitext(path)[1].lower(), "")


def analyze(path: str, content: str) -> UnitFacts:
    return UnitFacts(
        path=path,
        language=language_of(path),
        module_type=classify(path),
        provides=[],
        requires=[],
        exports=[],
        confidence=0.3,
        source="heuristic",
    )


# Every extension that has no dedicated analyzer routes here. Registered explicitly rather
# than as a wildcard so `analyze_file`'s fallback stays the single documented default.
register(_CONFIG_EXTS | {".sql", ".cob", ".cbl", ".cpy", ".jcl", ".pl", ".pm",
                         ".f", ".for", ".f90", ".asp", ".cfm", ".jsp", ".vbs",
                         ".bas", ".cls", ".pls", ".pks", ".pkb", ".prc"}, analyze)
