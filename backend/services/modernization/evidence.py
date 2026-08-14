"""What the parsers observed about the source, and where.

Evidence is the deterministic half of comprehension. Before any model is asked what an
application *does*, this module records what is provably *in* it: which tables the SQL
literals name, which hosts it talks to, which files nothing else includes (so they are
reachable from outside), which frameworks the imports betray, and which constructs cannot be
translated at all.

Two rules, both load-bearing:

* **Never the LLM.** D-04 makes this argument for dependency edges and it applies here
  unchanged, only worse: an invented entry point sends the architect down a decomposition
  that nothing downstream can recover from, and it looks exactly like a real one. A regex
  that misses a table is a bug you can see.
* **Every row carries `path` and `line`.** Evidence without a citation is an assertion, and
  the whole point of feeding this to a model is that the model can be held to it. The line
  is 1-based into the *redacted* content — analyzers run after `repo_ingest.redact`, so
  there is no original text to be relative to and nothing here can carry a secret.

The output feeds three consumers: the architect's prompt (what the system is), the
deterministic comprehension digest that goes into every converter's system block, and
reconciliation's entry-point coverage check — "500 files converted and not one of them is a
route" is otherwise invisible until someone runs the thing.
"""
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Sequence

logger = logging.getLogger(__name__)

# A closed vocabulary. Consumers switch on these, so a typo that silently invents a new kind
# would produce evidence nothing ever reads.
EVIDENCE_KINDS = (
    "framework",     # a library/framework the source depends on
    "entrypoint",    # reachable from outside: a page, a main(), a handler
    "route",         # a URL path the application answers on
    "datastore",     # a database/driver/persistence API in use
    "table",         # a table name appearing in SQL
    "integration",   # an external host or service
    "config_key",    # a value that was redacted and must be supplied as configuration
    "output",        # a site that writes to the response
    "hazard",        # something that must not be silently translated
    "dispatch",      # dynamic/indirect control flow a parser cannot resolve
)

MAX_SNIPPET = 200
# Per file, per kind. A generated file with 4,000 INSERTs should contribute a table list,
# not 4,000 rows; the cap keeps one pathological file from dominating the run's evidence.
MAX_PER_FILE_PER_KIND = 40

# ── Patterns ───────────────────────────────────────────────────────────────

# SQL table references. Deliberately narrow: a bare identifier after FROM/JOIN/INTO/UPDATE,
# no subqueries, no aliases resolved. Being incomplete is fine — this feeds a schema sketch a
# human confirms, not a migration.
_SQL_TABLE = re.compile(
    r"(?is)\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+[`\"\[]?([A-Za-z_][A-Za-z0-9_$]{1,62})[`\"\]]?")
_SQL_HINT = re.compile(r"(?i)\b(SELECT|INSERT|UPDATE|DELETE)\b.{0,80}?\b(FROM|INTO|SET|VALUES)\b",
                       re.DOTALL)

# Anything that looks like it leaves the process.
_URL = re.compile(r"""(?i)\b(https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]{4,200})""")

# Words that are URLs but are not integrations — schema/namespace declarations, licences,
# and the doc links every generated file carries.
_URL_NOISE = re.compile(
    r"(?i)(w3\.org|xmlsoap\.org|schemas\.|example\.(com|org|net)|localhost|127\.0\.0\.1|"
    r"purl\.org|apache\.org/licenses|gnu\.org|opensource\.org|microsoft\.com/[a-z-]*schema)")

# Framework/driver fingerprints that hold across languages: the import or call is the
# evidence, and the label is what a human reads on the assessment.
_FRAMEWORK_MARKERS = (
    ("Laravel", re.compile(r"(?i)\b(Illuminate\\\\|use\s+Illuminate|artisan)\b")),
    ("Symfony", re.compile(r"(?i)\b(Symfony\\\\|use\s+Symfony)\b")),
    ("CodeIgniter", re.compile(r"(?i)\b(CI_Controller|system/core/CodeIgniter)\b")),
    ("WordPress", re.compile(r"(?i)\b(wp_enqueue_script|add_action\s*\(|WP_Query)\b")),
    ("Zend/Laminas", re.compile(r"(?i)\b(Zend_|Laminas\\\\)")),
    ("Spring", re.compile(r"(?i)\borg\.springframework\b")),
    ("Struts", re.compile(r"(?i)\borg\.apache\.struts\b")),
    ("Hibernate", re.compile(r"(?i)\borg\.hibernate\b")),
    ("Django", re.compile(r"(?i)\bfrom\s+django\b|\bdjango\.db\b")),
    ("Flask", re.compile(r"(?i)\bfrom\s+flask\s+import\b")),
    ("ASP.NET MVC", re.compile(r"(?i)\bSystem\.Web\.Mvc\b")),
    ("Entity Framework", re.compile(r"(?i)\b(System\.Data\.Entity|Microsoft\.EntityFrameworkCore)\b")),
    ("Delphi VCL", re.compile(r"(?i)\buses\b[^;]{0,400}\b(Vcl\.|Forms|Controls)\b")),
    ("Delphi FireDAC", re.compile(r"(?i)\bFireDAC\.")),
    ("Delphi BDE", re.compile(r"(?i)\b(BDE|TTable|TQuery|DBTables)\b")),
    ("jQuery", re.compile(r"(?i)\bjquery(\.min)?\.js\b")),
)

# Persistence APIs. The label is the *source* API, because that is what the charter's
# data-access decision has to replace.
_DATASTORE_MARKERS = (
    ("mysql (ext/mysql)", re.compile(r"\bmysql_(connect|query|fetch_(assoc|array|row)|real_escape_string)\s*\(")),
    ("mysqli", re.compile(r"\bmysqli_[a-z_]+\s*\(|\bnew\s+mysqli\b")),
    ("PDO", re.compile(r"\bnew\s+PDO\b|->\s*prepare\s*\(")),
    ("ADOdb", re.compile(r"(?i)\bADONewConnection\b|\badodb\b")),
    ("JDBC", re.compile(r"\bjava\.sql\.|DriverManager\.getConnection")),
    ("ADO.NET", re.compile(r"\b(SqlConnection|OleDbConnection|OdbcConnection)\b")),
    ("Oracle", re.compile(r"(?i)\b(oci_connect|OracleConnection|OCI8)\b")),
    ("MongoDB", re.compile(r"(?i)\bmongo(db|client)\b")),
    ("Redis", re.compile(r"(?i)\bnew\s+Redis\b|\bpredis\b")),
)

# Where a response is produced. A file that writes output is a view or a controller; one
# that never does is a library, and that distinction is what makes a component a `.cshtml`
# or a `.cs`.
_OUTPUT_MARKERS = (
    ("redirect", re.compile(r"""(?i)\bheader\s*\(\s*['"]\s*Location\s*:""")),
    ("json", re.compile(r"(?i)\b(json_encode|JsonResult|application/json)\b")),
    ("html", re.compile(r"(?i)<(html|body|table|div|form)\b")),
    ("download", re.compile(r"""(?i)\bheader\s*\(\s*['"]\s*Content-(Disposition|Type)\s*:""")),
)

# Constructs that must reach a human rather than a translation. `dispatch` is separate
# because it is not dangerous, merely unresolvable — and confusing the two would either
# raise a gate on every legacy codebase or let an `eval` through silently.
_HAZARD_MARKERS = (
    ("eval", re.compile(r"\beval\s*\(|\bcreate_function\s*\(|\bassert\s*\(\s*['\"]")),
    ("shell_exec", re.compile(r"\b(exec|system|passthru|shell_exec|proc_open|popen)\s*\(")),
    ("deserialize_input", re.compile(r"\bunserialize\s*\(\s*\$_(GET|POST|REQUEST|COOKIE)")),
    ("symbol_table_write", re.compile(r"\bextract\s*\(|\bimport_request_variables\s*\(")),
    ("variable_variable", re.compile(r"\$\$[A-Za-z_]|\$\{\s*\$")),
    ("weak_password_hash", re.compile(r"(?i)\b(md5|sha1)\s*\(\s*\$?(pass|pwd|password)")),
    ("sql_string_building", re.compile(
        r"""\b(SELECT|INSERT|UPDATE|DELETE)\b[^;\n]{0,200}?["']\s*\.\s*\$"""
        r"""|\b(WHERE|VALUES)\b[^;\n]{0,80}?\$\{?[A-Za-z_]""", re.I)),
)

_DISPATCH_MARKERS = (
    ("variable_include", re.compile(r"\b(include|require)(_once)?\s*\(?\s*[\$\"']?\s*\$")),
    ("variable_function_call", re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\)|call_user_func")),
    ("reflection", re.compile(r"(?i)\b(ReflectionClass|Class\.forName|Activator\.CreateInstance)\b")),
)

# Apache rewrite rules — a legacy PHP app's routing table, and the crawl seed later.
_REWRITE = re.compile(r"(?im)^\s*RewriteRule\s+(\S+)\s+(\S+)")
_ALIAS = re.compile(r"(?im)^\s*(?:Alias|Redirect(?:Match)?)\s+(?:\d{3}\s+)?(\S+)\s+(\S+)")
_DIRECTORY_INDEX = re.compile(r"(?im)^\s*DirectoryIndex\s+(.+)$")

# Files that answer a request directly, by name rather than by graph position.
_ENTRYPOINT_STEMS = {"index", "main", "program", "app", "default", "startup", "server",
                     "login", "admin", "api", "gateway"}


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _snippet(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end].strip()[:MAX_SNIPPET]


def _row(path: str, kind: str, value: str, line: int = 0, snippet: str = "",
         confidence: float = 1.0, source: str = "parser") -> Dict[str, Any]:
    return {"path": path, "kind": kind, "value": value, "line": line,
            "snippet": snippet, "confidence": confidence, "source": source}


def _scan(pattern: re.Pattern, content: str, path: str, kind: str, label: str,
          out: List[Dict[str, Any]], cap: int) -> None:
    seen = 0
    for match in pattern.finditer(content):
        if seen >= cap:
            break
        seen += 1
        out.append(_row(path, kind, label, _line_of(content, match.start()),
                        _snippet(content, match.start(), match.end())))


def from_file(path: str, content: str, language: str = "",
              module_type: str = "") -> List[Dict[str, Any]]:
    """Everything observable in one file. Deterministic: same bytes, same rows, same order."""
    rows: List[Dict[str, Any]] = []
    if not content:
        return rows

    for label, pattern in _FRAMEWORK_MARKERS:
        match = pattern.search(content)
        if match:
            rows.append(_row(path, "framework", label, _line_of(content, match.start()),
                             _snippet(content, match.start(), match.end())))

    for label, pattern in _DATASTORE_MARKERS:
        match = pattern.search(content)
        if match:
            rows.append(_row(path, "datastore", label, _line_of(content, match.start()),
                             _snippet(content, match.start(), match.end())))

    for label, pattern in _OUTPUT_MARKERS:
        match = pattern.search(content)
        if match:
            rows.append(_row(path, "output", label, _line_of(content, match.start()),
                             _snippet(content, match.start(), match.end())))

    for label, pattern in _HAZARD_MARKERS:
        _scan(pattern, content, path, "hazard", label, rows, MAX_PER_FILE_PER_KIND)

    for label, pattern in _DISPATCH_MARKERS:
        _scan(pattern, content, path, "dispatch", label, rows, MAX_PER_FILE_PER_KIND)

    # Tables, but only where the surrounding text actually looks like SQL. Without that
    # guard `FROM` in prose and `UPDATE` in a comment both produce phantom tables.
    if _SQL_HINT.search(content):
        tables, seen = [], set()
        for match in _SQL_TABLE.finditer(content):
            name = match.group(1)
            if name.lower() in ("select", "where", "set", "values", "dual", "table"):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            tables.append((name, match.start(), match.end()))
            if len(tables) >= MAX_PER_FILE_PER_KIND:
                break
        for name, start, end in tables:
            rows.append(_row(path, "table", name, _line_of(content, start),
                             _snippet(content, start, end), confidence=0.8))

    hosts = set()
    for match in _URL.finditer(content):
        url = match.group(1).rstrip(".,;:)\"'")
        if _URL_NOISE.search(url):
            continue
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        if not host or host in hosts:
            continue
        hosts.add(host)
        rows.append(_row(path, "integration", host, _line_of(content, match.start()),
                         _snippet(content, match.start(), match.end()), confidence=0.7))
        if len(hosts) >= MAX_PER_FILE_PER_KIND:
            break

    basename = path.rsplit("/", 1)[-1].lower()
    if basename in (".htaccess", "web.config", "httpd.conf"):
        for match in _REWRITE.finditer(content):
            rows.append(_row(path, "route", f"{match.group(1)} -> {match.group(2)}",
                             _line_of(content, match.start()),
                             _snippet(content, match.start(), match.end())))
        for match in _ALIAS.finditer(content):
            rows.append(_row(path, "route", f"{match.group(1)} -> {match.group(2)}",
                             _line_of(content, match.start()),
                             _snippet(content, match.start(), match.end())))
        for match in _DIRECTORY_INDEX.finditer(content):
            rows.append(_row(path, "route", f"(index) -> {match.group(1).strip()}",
                             _line_of(content, match.start()),
                             _snippet(content, match.start(), match.end())))

    stem = os.path.splitext(basename)[0]
    if stem in _ENTRYPOINT_STEMS or module_type == "entrypoint":
        rows.append(_row(path, "entrypoint", path, 1, "", confidence=0.6, source="heuristic"))

    # A redacted placeholder is not a secret, but it *is* a promise: whoever runs the
    # converted code has to supply that value, and the export report lists them.
    for match in re.finditer(r"__REDACTED_([A-Z_]+)__", content):
        rows.append(_row(path, "config_key", match.group(1).lower(),
                         _line_of(content, match.start()),
                         _snippet(content, match.start(), match.end())))

    return rows


def from_graph(units: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Evidence that only exists once the whole graph does.

    A source file nothing else includes is reachable from outside or it is dead — and which
    of the two it is decides whether it becomes a controller or gets dropped. This is free:
    `graph.build` already computed `dependents`.
    """
    rows: List[Dict[str, Any]] = []
    for unit in units:
        path = unit.get("path", "")
        if unit.get("module_type") in ("config", "test"):
            continue
        if unit.get("dependents"):
            continue
        rows.append(_row(path, "entrypoint", path, 1,
                         "nothing in the codebase includes or imports this file",
                         confidence=0.75, source="parser"))
    return rows


def collect(files: Sequence[Dict[str, Any]], units: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every row for a run, deduplicated and in a stable order."""
    by_path = {u.get("path"): u for u in units}
    rows: List[Dict[str, Any]] = []
    for entry in files:
        path = entry.get("path", "")
        unit = by_path.get(path) or {}
        try:
            rows.extend(from_file(path, entry.get("content") or "",
                                  unit.get("language", ""), unit.get("module_type", "")))
        except Exception:
            # One unparseable file must not cost the run its whole evidence set.
            logger.exception("Evidence extraction failed for %s", path)
    rows.extend(from_graph(units))

    deduped: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (row["kind"], row["path"], row["value"], row["line"])
        deduped.setdefault(key, row)
    return [deduped[k] for k in sorted(deduped)]


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Distinct values per kind, most-frequent first, for the assessment view."""
    counts: Dict[str, Dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row["kind"], {})
        counts[row["kind"]][row["value"]] = counts[row["kind"]].get(row["value"], 0) + 1
    out: Dict[str, List[str]] = {}
    for kind, values in counts.items():
        out[kind] = [v for v, _ in sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))]
    return out
