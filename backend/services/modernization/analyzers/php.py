"""PHP: dependency edges, declarations, and the facts a .NET conversion actually needs.

Until this existed, `.php` fell through to `generic.analyze`, which returns no `provides` and
no `requires`. The consequences compounded: every PHP file landed at `topo_level 0`, the
conversion order was path-alphabetical, the run's symbol registry stayed empty because no
file was ever another's declared dependency, and every single prompt rendered *"This unit has
no already-converted dependencies — it is at the root."* File 200 was converted knowing
nothing about file 4. That is most of why a PHP conversion came out badly, and none of it
was visible in any individual file.

Two engines behind one `analyze()`
----------------------------------
tree-sitter when the grammar imports, a real regex analyzer when it does not. The fallback is
not a stub: it is written on the assumption that it *will* be used, because two binary wheels
are exactly the dependency most likely to be missing on somebody's machine (the litellm pin
in `requirements.txt` exists for the same reason). What it must never do is fall through to
`generic`, which would silently restore the zero-edge defect this module exists to remove —
so a parse failure degrades precision and records that it did, via `source` and `confidence`.

Why `provides` carries paths as well as names
---------------------------------------------
Every other analyzer here resolves by *name*: a Python module path, a Java FQCN, a Pascal
unit. PHP resolves by **path** — `require_once 'lib/db.php'` names a file, not a symbol. So a
PHP file provides its own path (and every suffix of it, since an include may be written
relative to any directory on the include path) *and* its namespace, FQCNs and global function
names. `graph.resolve_edges` matches on equality, so offering several spellings and letting
the unmatched ones find nothing is the same trick `python._module_names` already uses.

What is deliberately not an edge
--------------------------------
`include $_GET['page']` produces **no** requirement. A guessed edge reorders the conversion
and corrupts every file after it (D-04); a missing edge only converts a file early with
nothing assumed, which is the documented safe wrong answer. The dynamic include is recorded
as a hazard instead, where a human will see it.
"""
import logging
import os
import posixpath
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.services.modernization.analyzers import Symbol, UnitFacts, generic, register

logger = logging.getLogger(__name__)

# `.inc` is NOT here. It belongs to Delphi (`pascal.py` registers it), `register()` is
# last-write-wins over an alphabetical import order, and taking it would silently break every
# Delphi run in the deployment. A PHP codebase that uses `.inc` passes it as a per-run
# extension override, which the API already supports.
PHP_EXTENSIONS = {".php", ".php3", ".php4", ".php5", ".php7", ".phtml", ".tpl"}

_SUPERGLOBALS = ("$_GET", "$_POST", "$_REQUEST", "$_SESSION", "$_COOKIE", "$_SERVER",
                 "$_FILES", "$_ENV", "$GLOBALS")

# Persistence calls whose SQL we want to see. The label is the *source* API because that is
# what the charter's data-access decision has to replace.
_DB_FUNCTIONS = {
    "mysql_query": "mysql", "mysql_connect": "mysql", "mysql_fetch_assoc": "mysql",
    "mysql_fetch_array": "mysql", "mysql_fetch_row": "mysql", "mysql_real_escape_string": "mysql",
    "mysqli_query": "mysqli", "mysqli_connect": "mysqli", "mysqli_prepare": "mysqli",
    "mysqli_fetch_assoc": "mysqli", "mysqli_fetch_array": "mysqli",
    "mysqli_real_escape_string": "mysqli", "mysqli_num_rows": "mysqli",
    "pg_query": "postgres", "pg_connect": "postgres",
    "oci_parse": "oracle", "oci_connect": "oracle",
    "sqlsrv_query": "sqlsrv",
}

_SQL_START = re.compile(r"(?is)^\s*\(?\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|CALL|WITH)\b")
_SQL_TABLE = re.compile(
    r"(?is)\b(?:FROM|JOIN|INTO|UPDATE)\s+[`\"\[]?([A-Za-z_][A-Za-z0-9_$]{1,62})[`\"\]]?")

MAX_FACT_ROWS = 40          # per category, per file — see `evidence.MAX_PER_FILE_PER_KIND`


# ── The parser, loaded once and never allowed to raise at import ───────────

_PARSER = None
_LANGUAGE = None
_LOAD_ERROR = ""


def _load_parser():
    """Probe rather than assume. Both packages have moved their API within living memory:
    `Language()` took `(path, name)` before tree-sitter 0.22 and takes a PyCapsule after, and
    `tree_sitter_php` exposes `language_php()` now where older releases had `language()`.
    Any failure here means "no parser" and a WARNING — never an exception, because this runs
    inside `database.init_db()` at application boot and an optional dependency must not be
    able to take the API host down."""
    global _PARSER, _LANGUAGE, _LOAD_ERROR
    try:
        import tree_sitter
        import tree_sitter_php
    except Exception as exc:                                    # pragma: no cover - env
        _LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("tree-sitter PHP grammar unavailable (%s) — using the regex analyzer, "
                       "which finds fewer edges but still finds them", _LOAD_ERROR)
        return
    try:
        getter = (getattr(tree_sitter_php, "language_php", None)
                  or getattr(tree_sitter_php, "language", None))
        if getter is None:
            raise AttributeError("tree_sitter_php exposes no language getter")
        _LANGUAGE = tree_sitter.Language(getter())
        _PARSER = tree_sitter.Parser(_LANGUAGE)
    except Exception as exc:                                    # pragma: no cover - env
        _PARSER = _LANGUAGE = None
        _LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("tree-sitter PHP grammar failed to load (%s) — using the regex "
                       "analyzer", _LOAD_ERROR)


_load_parser()


def parser_available() -> bool:
    return _PARSER is not None


# ── Path resolution ────────────────────────────────────────────────────────

_INCLUDE_LITERAL = re.compile(
    r"""(?x)
    \b(include|include_once|require|require_once)\b \s* \(? \s*
    (?:
        (?: __DIR__ | dirname \s* \( \s* __FILE__ \s* \) ) \s* \. \s*
    )?
    (['"])(?P<target>[^'"\n]{1,300})\2
    """)

# A genuinely dynamic include: the target starts with a variable, or with a constant that is
# not one of the two path builders every legacy codebase uses. `__DIR__ . '/x.php'` and
# `dirname(__FILE__) . '/x.php'` are literal includes wearing a prefix and must not be
# flagged — they are the dominant idiom, so treating them as hazards would raise a blocking
# gate on essentially every PHP file and train people to click through it.
_INCLUDE_DYNAMIC = re.compile(
    r"""\b(include|include_once|require|require_once)\b\s*\(?\s*"""
    r"""(?!__DIR__|dirname\s*\(\s*__FILE__)(?:\$|[A-Z_]{2,}\s*\.)""")


def _resolve_include(target: str, from_path: str) -> Optional[str]:
    """Turn an include literal into a repo-relative path, or None if it escapes.

    Escaping the root yields None rather than a repaired path — the same rule
    `repo_ingest.safe_relpath` applies to archive members (D-22), and for the same reason:
    output paths are derived from input paths, so a traversal here is a real primitive.
    """
    target = target.strip().replace("\\", "/").lstrip("/")
    if not target:
        return None
    base = posixpath.dirname(from_path)
    joined = posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)
    if joined.startswith("..") or joined.startswith("/"):
        return None
    return joined


def _path_spellings(path: str) -> List[str]:
    """Every way another file might name this one.

    `require 'db.php'` from a sibling directory has to resolve to `app/lib/db.php`, and the
    only mechanism `graph.resolve_edges` has is string equality — so the file offers its full
    path and every suffix of it. A spelling nothing requires simply never matches, which is
    free; a spelling missing is an edge lost.
    """
    parts = path.split("/")
    return [ "/".join(parts[i:]) for i in range(len(parts)) ]


# ── tree-sitter engine ─────────────────────────────────────────────────────

_QUERY_SOURCE = """
(namespace_definition name: (namespace_name) @namespace)
(namespace_use_clause (qualified_name) @use)
(class_declaration name: (name) @class)
(interface_declaration name: (name) @interface)
(trait_declaration name: (name) @trait)
(enum_declaration name: (name) @enum)
(function_definition name: (name) @function)
(const_element (name) @const)
(base_clause (name) @extends)
(base_clause (qualified_name) @extends)
(class_interface_clause (name) @implements)
(class_interface_clause (qualified_name) @implements)
(object_creation_expression (name) @new)
(object_creation_expression (qualified_name) @new)
(scoped_call_expression scope: (name) @static)
(scoped_call_expression scope: (qualified_name) @static)
(class_constant_access_expression (name) @static)
(function_call_expression function: (name) @call)
(subscript_expression (variable_name) @sg_var (string) @sg_key)
(text) @html
"""


def _captures(tree) -> Dict[str, List[Any]]:
    import tree_sitter

    query = tree_sitter.Query(_LANGUAGE, _QUERY_SOURCE)
    try:
        cursor = tree_sitter.QueryCursor(query)
        result = cursor.captures(tree.root_node)
    except AttributeError:                                      # pragma: no cover - old API
        result = query.captures(tree.root_node)
    if isinstance(result, dict):
        return result
    grouped: Dict[str, List[Any]] = {}
    for node, name in result:
        grouped.setdefault(name, []).append(node)
    return grouped


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _line(node) -> int:
    return node.start_point[0] + 1


def _analyze_treesitter(path: str, content: str) -> UnitFacts:
    tree = _PARSER.parse(content.encode("utf-8"))
    caps = _captures(tree)

    namespaces = [_text(n) for n in caps.get("namespace", [])]
    namespace = namespaces[0] if namespaces else ""

    provides: List[str] = list(_path_spellings(path))
    requires: List[str] = []
    exports: List[Symbol] = []

    if namespace:
        provides.append(namespace)

    for capture, kind in (("class", "class"), ("interface", "interface"),
                          ("trait", "trait"), ("enum", "enum")):
        for node in caps.get(capture, []):
            name = _text(node)
            provides.append(name)
            if namespace:
                provides.append(f"{namespace}\\{name}")
            exports.append(Symbol(name=name, kind=kind,
                                  signature=_declaration_line(content, node)))

    for node in caps.get("function", []):
        name = _text(node)
        provides.append(name)
        if namespace:
            provides.append(f"{namespace}\\{name}")
        exports.append(Symbol(name=name, kind="function",
                              signature=_declaration_line(content, node)))

    for node in caps.get("const", []):
        name = _text(node)
        provides.append(name)
        exports.append(Symbol(name=name, kind="constant", signature=f"const {name}"))

    for capture in ("use", "extends", "implements", "new", "static"):
        for node in caps.get(capture, []):
            requires.extend(_name_spellings(_text(node), namespace))

    # `define('X', ...)` — a constant, and the pre-namespace idiom for one.
    for node in caps.get("call", []):
        if _text(node) == "define":
            parent = node.parent
            literal = _first_string_argument(parent)
            if literal:
                provides.append(literal)
                exports.append(Symbol(name=literal, kind="constant",
                                      signature=f"define('{literal}', ...)"))

    facts = _php_facts(path, content, caps)
    requires.extend(facts.pop("_include_edges", []))

    return UnitFacts(
        path=path,
        language="PHP",
        module_type=_classify(path, content, facts,
                              declares_type=any(s.kind in ("class", "interface", "trait", "enum")
                                                for s in exports)),
        provides=sorted(set(p for p in provides if p)),
        requires=sorted(set(r for r in requires if r)),
        exports=exports,
        confidence=1.0,
        source="parser",
        meta={"php": facts},
    )


def _declaration_line(content: str, name_node) -> str:
    """The declaration as written, without the body — what a later unit is shown."""
    start = name_node.parent.start_byte if name_node.parent else name_node.start_byte
    encoded = content.encode("utf-8")
    tail = encoded[start:start + 400].decode("utf-8", "replace")
    for terminator in ("{", ";", "\n"):
        index = tail.find(terminator)
        if index > 0:
            tail = tail[:index]
            break
    return " ".join(tail.split())[:300]


def _first_string_argument(call_node) -> str:
    if call_node is None:
        return ""
    for child in call_node.children:
        if child.type == "arguments":
            for arg in child.children:
                text = _text(arg)
                if len(text) > 1 and text[0] in "'\"":
                    return text[1:-1]
            break
    return ""


def _name_spellings(raw: str, namespace: str) -> List[str]:
    """A referenced name, in every form that might match a `provides`."""
    name = raw.lstrip("\\").strip()
    if not name or name.lower() in ("self", "static", "parent", "$this"):
        return []
    out = [name]
    leaf = name.rsplit("\\", 1)[-1]
    if leaf != name:
        out.append(leaf)
    elif namespace:
        # An unqualified name inside a namespace could be either this namespace's or a
        # global. Offer both; the one that matches nothing costs nothing.
        out.append(f"{namespace}\\{name}")
    return out


# ── Facts ──────────────────────────────────────────────────────────────────

def _php_facts(path: str, content: str, caps: Optional[Dict[str, List[Any]]] = None) -> Dict[str, Any]:
    """Everything a .NET conversion needs that is not an edge and not a declaration.

    These reach the converter in the **message** for the file that owns them, never the
    system block: they differ per unit, and anything that differs per unit sitting in the
    cached prefix costs full price on every call in the run (D-06).
    """
    facts: Dict[str, Any] = {}
    include_edges: List[str] = []

    # Includes, literal only.
    for match in _INCLUDE_LITERAL.finditer(content):
        resolved = _resolve_include(match.group("target"), path)
        if resolved:
            include_edges.extend(_path_spellings(resolved)[:1] or [resolved])
    facts["_include_edges"] = include_edges

    # Superglobals, *with their keys* — the keys are the payload. `$_GET['id']` is a query
    # parameter named id, which is a bindable action argument; knowing only that the file
    # "uses $_GET" tells the converter nothing it can act on.
    superglobals: Dict[str, Dict[str, Any]] = {}
    for name in _SUPERGLOBALS:
        escaped = re.escape(name)
        keys = re.findall(escaped + r"\s*\[\s*['\"]([A-Za-z0-9_.\-]{1,64})['\"]\s*\]", content)
        count = len(re.findall(escaped + r"\b", content))
        if count:
            superglobals[name] = {
                "count": count,
                "keys": sorted(set(keys))[:MAX_FACT_ROWS],
            }
    if superglobals:
        facts["superglobals"] = superglobals

    # Database call sites, with the SQL and whether it was concatenated. `interpolated` is
    # simultaneously an EF-mapping signal and an injection finding.
    db_sites = []
    tables: Set[str] = set()
    for function, api in _DB_FUNCTIONS.items():
        for match in re.finditer(r"\b" + re.escape(function) + r"\s*\(", content):
            line = content.count("\n", 0, match.start()) + 1
            sql, interpolated = _sql_near(content, match.start())
            site = {"api": api, "fn": function, "line": line}
            if sql:
                site["sql"] = sql[:300]
                site["interpolated"] = interpolated
                tables.update(t.lower() for t in _SQL_TABLE.findall(sql))
            db_sites.append(site)
            if len(db_sites) >= MAX_FACT_ROWS:
                break
    if db_sites:
        # Two calls on one line — `mysqli_fetch_assoc(mysqli_query(...))` is the classic —
        # resolve to the same SQL, and reporting the same statement twice reads as two
        # queries in the prompt.
        seen_sql = set()
        deduped = []
        for site in sorted(db_sites, key=lambda s: (s["line"], s["fn"])):
            key = (site["line"], site.get("sql", ""))
            if site.get("sql") and key in seen_sql:
                continue
            seen_sql.add(key)
            deduped.append(site)
        facts["db_sites"] = deduped[:MAX_FACT_ROWS]
        facts["db_apis"] = sorted({s["api"] for s in db_sites})
    if tables:
        facts["tables"] = sorted(tables)[:MAX_FACT_ROWS]

    # Presentation shape. What decides whether a component becomes a .cs or a .cshtml.
    html_chars = 0
    if caps is not None:
        html_chars = sum(node.end_byte - node.start_byte for node in caps.get("html", []))
    else:
        html_chars = sum(len(m.group(0)) for m in
                         re.finditer(r"\?>(.*?)(?:<\?php|<\?=|\Z)", content, re.S))
    total = max(len(content), 1)
    facts["html_ratio"] = round(min(html_chars / total, 1.0), 3)
    facts["has_html_document"] = bool(re.search(r"(?i)<(!doctype|html|head)\b", content))
    facts["echo_sites"] = len(re.findall(r"\b(echo|print)\b|<\?=", content))

    headers = re.findall(r"""(?i)\bheader\s*\(\s*['"]([^'"\n]{1,120})""", content)
    if headers:
        facts["headers"] = sorted(set(headers))[:MAX_FACT_ROWS]
        facts["redirects"] = sum(1 for h in headers if h.lower().startswith("location"))
        facts["emits_json"] = any("application/json" in h.lower() for h in headers)

    if re.search(r"\bsession_start\s*\(", content):
        facts["starts_session"] = True
    if re.search(r"\b(setcookie|setrawcookie)\s*\(", content):
        facts["sets_cookies"] = True
    if re.search(r"\bmove_uploaded_file\s*\(", content):
        facts["handles_uploads"] = True

    # Hazards. Recorded, never silently translated — three layers enforce that: this row, a
    # rule in the conversion prompt, and a Tier-0 check that fails an artifact whose source
    # had a blocked hazard and whose output has no marker.
    hazards = []
    for label, pattern in (
        ("eval", r"\beval\s*\(|\bcreate_function\s*\("),
        ("variable_variable", r"\$\$[A-Za-z_]|\$\{\s*\$"),
        ("variable_include", None),
        ("extract", r"\bextract\s*\(|\bimport_request_variables\s*\("),
        ("unserialize_request", r"\bunserialize\s*\(\s*\$_(GET|POST|REQUEST|COOKIE)"),
        ("shell_exec", r"\b(exec|system|passthru|shell_exec|proc_open|popen)\s*\("),
        ("globals_write", r"\$GLOBALS\s*\[[^\]]+\]\s*="),
        ("removed_mysql_ext", r"\bmysql_(connect|query|fetch_assoc|fetch_array)\s*\("),
        ("weak_password_hash", r"(?i)\b(md5|sha1)\s*\(\s*\$?(pass|pwd|password)"),
    ):
        matches = (list(_INCLUDE_DYNAMIC.finditer(content)) if label == "variable_include"
                   else list(re.finditer(pattern, content)))
        for match in matches[:10]:
            hazards.append({"kind": label,
                            "line": content.count("\n", 0, match.start()) + 1})
    if hazards:
        facts["hazards"] = hazards[:MAX_FACT_ROWS]
        facts["hazard_kinds"] = sorted({h["kind"] for h in hazards})

    return facts


def _sql_near(content: str, call_start: int) -> Tuple[str, bool]:
    """The SQL literal belonging to a database call, and whether it was concatenated.

    Looks **both ways**. `mysqli_query($conn, "SELECT ...")` puts the literal after the call;
    the far more common legacy shape puts it before —

        $sql = "SELECT * FROM products WHERE id = " . $_GET['id'];
        $row = mysqli_fetch_assoc(mysqli_query($conn, $sql));

    — and that shape is exactly the one worth finding, because a query assembled into a
    variable first is usually assembled by concatenation. Searching forward only would have
    missed every injection in a typical codebase.
    """
    window_start = max(0, call_start - 700)
    window = content[window_start:call_start + 400]
    pivot = call_start - window_start

    best, interpolated, distance = "", False, None
    for match in re.finditer(r"""(['"])(.{6,600}?)\1""", window, re.S):
        literal = match.group(2)
        if not _SQL_START.search(literal):
            continue
        # Nearest literal to the call wins, in either direction.
        gap = 0 if match.start() <= pivot <= match.end() else min(
            abs(pivot - match.start()), abs(pivot - match.end()))
        if distance is not None and gap >= distance:
            continue
        after = window[match.end():match.end() + 80]
        distance = gap
        best = " ".join(literal.split())
        interpolated = bool(re.match(r"\s*\.", after)) or bool(
            re.search(r"\$[A-Za-z_{]", literal))
    return best, interpolated


# ── Classification ─────────────────────────────────────────────────────────

_TEST_PATH = re.compile(r"(?i)(^|/)(tests?|spec)s?(/|$)|(test|spec)\.php$|^test")


def _classify(path: str, content: str, facts: Dict[str, Any],
              declares_type: bool = False) -> str:
    """Content first, path second. PHP directory conventions are far weaker than Java's or
    .NET's, and a wrong role here reaches the architect as a hint about what a file *is*."""
    basename = path.rsplit("/", 1)[-1].lower()
    if _TEST_PATH.search(path) or basename.startswith("test"):
        return "test"
    if basename in ("config.php", "configuration.php", "settings.php", "constants.php") \
            and facts.get("html_ratio", 0) < 0.05:
        return "config"
    if basename in ("db.php", "database.php") and facts.get("html_ratio", 0) < 0.05:
        return "data"
    # A file that emits a whole HTML document is a page, whatever else it also does. Legacy
    # PHP pages query the database *and* render — deciding "data" because there is SQL in it
    # would send every page in the application to the wrong half of the architecture.
    if facts.get("has_html_document"):
        return "ui"
    if facts.get("html_ratio", 0) > 0.3 or (
            facts.get("echo_sites") and facts.get("html_ratio", 0) > 0.05):
        return "ui"
    if facts.get("db_sites"):
        return "data"
    if declares_type:
        return "business"
    if facts.get("redirects") or facts.get("superglobals"):
        return "business"
    return generic.classify(path)


# ── Regex engine ───────────────────────────────────────────────────────────

_RX_NAMESPACE = re.compile(r"(?m)^\s*namespace\s+([\w\\]+)\s*[;{]")
_RX_USE = re.compile(r"(?m)^\s*use\s+(?:function\s+|const\s+)?([\w\\]+)")
_RX_USE_GROUP = re.compile(r"(?m)^\s*use\s+([\w\\]+)\\\{([^}]+)\}")
_RX_TYPE = re.compile(r"(?m)^\s*(?:final\s+|abstract\s+|readonly\s+)*"
                      r"(class|interface|trait|enum)\s+(\w+)([^\n{]*)")
_RX_FUNCTION = re.compile(r"(?m)^\s*function\s+(\w+)\s*\(([^)]*)\)")
_RX_EXTENDS = re.compile(r"\bextends\s+([\w\\]+)")
_RX_IMPLEMENTS = re.compile(r"\bimplements\s+([\w\\,\s]+?)(?:\{|$)", re.M)
_RX_NEW = re.compile(r"\bnew\s+([\w\\]+)\s*\(")
_RX_STATIC = re.compile(r"\b([A-Z][\w\\]*)::")
_RX_DEFINE = re.compile(r"""\bdefine\s*\(\s*['"](\w+)['"]""")
_RX_CONST = re.compile(r"(?m)^\s*const\s+(\w+)\s*=")


def _analyze_regex(path: str, content: str, confidence: float = 0.6) -> UnitFacts:
    namespace_match = _RX_NAMESPACE.search(content)
    namespace = namespace_match.group(1) if namespace_match else ""

    provides = list(_path_spellings(path))
    requires: List[str] = []
    exports: List[Symbol] = []

    if namespace:
        provides.append(namespace)

    for kind, name, tail in _RX_TYPE.findall(content):
        provides.append(name)
        if namespace:
            provides.append(f"{namespace}\\{name}")
        exports.append(Symbol(name=name, kind=kind,
                              signature=f"{kind} {name}{tail}".strip()[:300]))

    for name, params in _RX_FUNCTION.findall(content):
        provides.append(name)
        if namespace:
            provides.append(f"{namespace}\\{name}")
        exports.append(Symbol(name=name, kind="function",
                              signature=f"function {name}({params})"[:300]))

    for name in _RX_DEFINE.findall(content) + _RX_CONST.findall(content):
        provides.append(name)
        exports.append(Symbol(name=name, kind="constant", signature=f"const {name}"))

    for prefix, group in _RX_USE_GROUP.findall(content):
        for member in group.split(","):
            member = member.strip()
            if member:
                requires.extend(_name_spellings(f"{prefix}\\{member}", namespace))
    for name in _RX_USE.findall(content):
        requires.extend(_name_spellings(name, namespace))
    for name in _RX_EXTENDS.findall(content):
        requires.extend(_name_spellings(name, namespace))
    for group in _RX_IMPLEMENTS.findall(content):
        for name in group.split(","):
            requires.extend(_name_spellings(name.strip(), namespace))
    for name in _RX_NEW.findall(content) + _RX_STATIC.findall(content):
        requires.extend(_name_spellings(name, namespace))

    facts = _php_facts(path, content, caps=None)
    requires.extend(facts.pop("_include_edges", []))

    return UnitFacts(
        path=path,
        language="PHP",
        module_type=_classify(path, content, facts,
                              declares_type=any(s.kind in ("class", "interface", "trait", "enum")
                                                for s in exports)),
        provides=sorted(set(p for p in provides if p)),
        requires=sorted(set(r for r in requires if r)),
        exports=exports,
        confidence=confidence,
        source="heuristic",
        meta={"php": facts},
    )


# ── Entry point ────────────────────────────────────────────────────────────

def analyze(path: str, content: str) -> UnitFacts:
    if _PARSER is not None:
        try:
            return _analyze_treesitter(path, content)
        except Exception:
            # Precision degrades, availability does not — and `source`/`confidence` record
            # which engine actually ran, so a systematic grammar problem shows up as a
            # suspiciously heuristic run rather than as a suspiciously flat graph.
            logger.exception("tree-sitter failed on %s — falling back to the regex analyzer",
                             path)
            return _analyze_regex(path, content, confidence=0.5)
    return _analyze_regex(path, content)


register(PHP_EXTENSIONS, analyze)
