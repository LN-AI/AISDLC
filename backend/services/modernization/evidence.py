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
# `IF NOT EXISTS` is consumed rather than skipped. Excluding `IF` from the results instead
# meant `DROP TABLE IF EXISTS attendance` contributed no table at all — the keyword was
# suppressed and the name after it was never reached, so a schema's own DROP/CREATE pair could
# leave a table out of the assessment entirely.
_SQL_TABLE = re.compile(
    r"(?is)\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
    r"[`\"\[]?([A-Za-z_][A-Za-z0-9_$]{1,62})[`\"\]]?")
# DML *or* DDL. This guard only recognised DML, so a `schema.sql` holding nothing but
# `CREATE TABLE` statements produced no table evidence at all — and a schema file is the one
# place table names are stated authoritatively rather than inferred from a query. A run could
# ship the customer's whole schema in the export and list none of its tables in the assessment.
_SQL_HINT = re.compile(
    r"(?i)\b(SELECT|INSERT|UPDATE|DELETE)\b.{0,80}?\b(FROM|INTO|SET|VALUES)\b"
    r"|\b(CREATE|ALTER|DROP|TRUNCATE)\s+(?:TEMPORARY\s+|TEMP\s+)?TABLE\b",
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

# The engine name, from the two places a PHP codebase states it outright.
_DATASTORE_ENGINES = (
    ("dsn", re.compile(r"""(?i)\bnew\s+\\?PDO\s*\(\s*['"]([a-z0-9]{2,12})\s*:""")),
    ("composer", re.compile(r"""(?i)["']ext-(?:pdo_)?([a-z0-9]{2,12})["']\s*:""")),
    ("dsn_config", re.compile(r"""(?i)['"]([a-z0-9]{2,12}):(?:host=|dbname=|//|/)""")),
)

# Canonical spelling, so `advice._PROVIDERS` matches on one form rather than several.
_ENGINE_NAMES = {
    "sqlite": "SQLite", "sqlite3": "SQLite",
    "mysql": "MySQL", "mysqli": "MySQL",
    "pgsql": "PostgreSQL", "postgres": "PostgreSQL", "postgresql": "PostgreSQL",
    "sqlsrv": "SQL Server", "mssql": "SQL Server", "dblib": "SQL Server",
    "oci": "Oracle", "oracle": "Oracle",
    "firebird": "Firebird", "informix": "Informix", "ibm": "DB2",
}

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
    # Free-function form only — see `analyzers/php._SHELL_EXEC` for what `\b` alone let
    # through and what it cost. Kept in step with that copy deliberately; the two tables are
    # separate because they feed different consumers, and this bug had to be fixed twice.
    ("shell_exec", re.compile(r"(?<![>:$\w])\b(exec|system|passthru|shell_exec|proc_open|popen)\s*\(")),
    ("deserialize_input", re.compile(r"\bunserialize\s*\(\s*\$_(GET|POST|REQUEST|COOKIE)")),
    ("symbol_table_write", re.compile(r"\bextract\s*\(|\bimport_request_variables\s*\(")),
    ("variable_variable", re.compile(r"\$\$[A-Za-z_]|\$\{\s*\$")),
    ("weak_password_hash", re.compile(r"(?i)\b(md5|sha1)\s*\(\s*\$?(pass|pwd|password)")),
    # Two shapes, and only two, because this is reported as an injection finding and the
    # converter is told to fix every occurrence — so a false positive asks for a rewrite of
    # code that was already correct.
    #
    # The old second alternative was `\b(WHERE|VALUES)\b[^;\n]{0,80}?\$\{?[A-Za-z_]`, which
    # cannot tell a bound parameter from an interpolated one: in
    # `selectOne('SELECT * FROM employees WHERE id = :id', ['id' => $id])` the `$id` it finds
    # is the *bound value*, eighty characters of params array away from a statement that is
    # already parameterised. Five of the seven findings on a fully-parameterised repository
    # layer were that.
    #
    #   1. concatenation — a quote, a dot, a variable. Unambiguous in any quoting style.
    #   2. interpolation — a variable inside a **double-quoted** literal that holds SQL.
    #      Single-quoted strings do not interpolate in PHP, so a `$` inside one is not a
    #      value being spliced into the statement, and a params array is never inside the
    #      literal at all.
    ("sql_string_building", re.compile(
        r"""\b(SELECT|INSERT|UPDATE|DELETE)\b[^;\n]{0,200}?["']\s*\.\s*\$"""
        r'''|"[^"\n]{0,200}?\b(SELECT|INSERT|UPDATE|DELETE|WHERE|VALUES)\b'''
        r'''[^"\n]{0,200}?\$\{?[A-Za-z_]''', re.I)),
)

_DISPATCH_MARKERS = (
    ("variable_include", re.compile(r"\b(include|require)(_once)?\s*\(?\s*[\$\"']?\s*\$")),
    ("variable_function_call", re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\)|call_user_func")),
    ("reflection", re.compile(r"(?i)\b(ReflectionClass|Class\.forName|Activator\.CreateInstance)\b")),
)

# A route table declared in code, which is how anything written against a framework — or a
# hand-rolled front controller — states its URLs.
#
# Route extraction used to be gated on the *basename* being `.htaccess`, `web.config` or
# `httpd.conf`, so an application whose entire routing lived in `routes/web.php` reported one
# route: the catch-all rewrite that hands everything to the front controller. That is the
# least informative sentence available about a web application. On a 45-file layered app,
# fifteen routes — each with a verb, a path template carrying `{id}`, a controller, an action
# and a middleware list — were invisible to the architect that had to reproduce them, while
# the charter promised "existing URLs keep working unchanged" against no list of URLs.
#
# Matched on the *shape* rather than on any one framework, because the shape is what they
# share: a verb-named call taking a path literal first. Covers `$router->get('/x', ...)`,
# `Route::post('/x', ...)`, `$app->any('/x', ...)`, `$r->addRoute('GET', '/x', ...)`.
_CODE_ROUTE = re.compile(
    r"""(?ix)
    (?: \$\w+ \s* -> \s* | [A-Z]\w* \s* :: \s* )      # $router->  |  Route::
    (get|post|put|patch|delete|options|head|any|all|map|match|addroute|route)
    \s* \(\s*
    (?: \[?\s* ['"](?P<verb>GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)['"] \s*\]? \s*, \s* )?
    ['"](?P<path>[^'"\n]{1,200})['"]
    """)

# The handler and middleware that follow the path, read from the rest of the call so a route
# row can say what answers it. Deliberately tolerant: a route whose handler is a closure is
# still a route, and reporting the URL without the handler beats reporting neither.
_ROUTE_HANDLER = re.compile(
    r"""(?ix) \[\s* (?P<class>[\w\\]+) \s* :: \s* class \s* , \s* ['"](?P<method>\w+)['"] """)
_ROUTE_MIDDLEWARE = re.compile(r"""(?ix) \[\s* ((?: \s* ['"]\w+['"] \s* ,? )+ ) \]\s*\)""")

# Apache rewrite rules — a legacy PHP app's routing table, and the crawl seed later.
_REWRITE = re.compile(r"(?im)^\s*RewriteRule\s+(\S+)\s+(\S+)")
_ALIAS = re.compile(r"(?im)^\s*(?:Alias|Redirect(?:Match)?)\s+(?:\d{3}\s+)?(\S+)\s+(\S+)")
_DIRECTORY_INDEX = re.compile(r"(?im)^\s*DirectoryIndex\s+(.+)$")

# Files that answer a request directly, by name rather than by graph position.
_ENTRYPOINT_STEMS = {"index", "main", "program", "app", "default", "startup", "server",
                     "login", "admin", "api", "gateway"}

# Directories whose contents are rendered, not requested.
_TEMPLATE_DIRS = {"views", "view", "templates", "template", "partials", "layouts", "layout",
                  "_partials", "includes", "inc", "tpl", "twig", "blade", "resources"}


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# Extensions with no executable semantics. Running the code-hazard and dispatch patterns over
# these produced findings out of prose: `"description": "…management system (sample app for
# migration testing)."` in a composer.json matched `system\s*\(` and was reported as a shell
# escape — and `shell_exec` is a *blocking* hazard, so a manifest file could refuse a
# component. Data files still contribute tables, integrations and routes; they just cannot
# contain a PHP construct.
_NON_CODE_EXTENSIONS = {".json", ".md", ".markdown", ".txt", ".csv", ".yml", ".yaml",
                        ".xml", ".lock", ".ini", ".cfg", ".toml", ".css", ".scss",
                        ".rst", ".log"}

_COMMENT_SPANS = re.compile(
    r"/\*.*?\*/"                 # C-style, PHP and SQL block comments
    r"|//[^\n]*"                 # PHP/C line comment
    r"|\#[^\n]*"                 # PHP/shell line comment
    r"|--[^\n]*",                # SQL line comment
    re.S)


def _blank_comments(content: str) -> str:
    """Comments replaced by spaces of the same length — same offsets, same line numbers.

    The table scanner reads prose as SQL otherwise. `/** Normalise input array into bound SQL
    parameters. */` yielded a table called `bound`, because `INTO\\s+(\\w+)` does not care that
    it is inside a doc block, and the `_SQL_HINT` guard is per *file*: one real statement
    anywhere licenses every phantom in the comments. Blanking rather than deleting keeps
    `_line_of` and `_snippet` honest, which is the whole promise of this module — a citation
    that points at the wrong line is worse than no citation.
    """
    return _COMMENT_SPANS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), content)


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

    # The engine behind the API, which the API label alone does not give. `PDO` is a wrapper:
    # recording only "PDO" left `advice._engine` with nothing to match, so the charter — the
    # document every unit is converted against — said "EF Core 8 with the matching EF Core
    # provider against the existing the source's engine database". The engine was stated
    # twice in plain text: the DSN scheme in `new PDO('sqlite:' . $path)` and `ext-pdo_sqlite`
    # in composer.json. Both are read here.
    for label, pattern in _DATASTORE_ENGINES:
        match = pattern.search(content)
        if match:
            engine = (match.group(1) or "").lower()
            if engine in _ENGINE_NAMES:
                rows.append(_row(path, "datastore", _ENGINE_NAMES[engine],
                                 _line_of(content, match.start()),
                                 _snippet(content, match.start(), match.end()),
                                 confidence=0.95))
                break

    for label, pattern in _OUTPUT_MARKERS:
        match = pattern.search(content)
        if match:
            rows.append(_row(path, "output", label, _line_of(content, match.start()),
                             _snippet(content, match.start(), match.end())))

    is_code = os.path.splitext(path)[1].lower() not in _NON_CODE_EXTENSIONS
    if is_code:
        for label, pattern in _HAZARD_MARKERS:
            _scan(pattern, content, path, "hazard", label, rows, MAX_PER_FILE_PER_KIND)

        for label, pattern in _DISPATCH_MARKERS:
            _scan(pattern, content, path, "dispatch", label, rows, MAX_PER_FILE_PER_KIND)

    # Tables, but only where the surrounding text actually looks like SQL, and only outside
    # comments — the per-file hint alone let prose in a commented file through.
    code = _blank_comments(content)
    if _SQL_HINT.search(code):
        tables, seen = [], set()
        for match in _SQL_TABLE.finditer(code):
            name = match.group(1)
            # `DROP TABLE IF EXISTS attendance` is why `if` is here: the old list stopped at
            # SQL nouns and let the keywords of the statement itself become table names.
            if name.lower() in ("select", "where", "set", "values", "dual", "table",
                                "if", "not", "exists", "only", "temporary", "temp",
                                "into", "from", "join", "update", "duplicate"):
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

    # Routes declared in code. Comments blanked first: a commented-out route is not a route,
    # and a routing file is exactly the kind of file that accumulates them.
    if is_code:
        seen_routes = set()
        for match in _CODE_ROUTE.finditer(code):
            verb = (match.group("verb") or match.group(1)).upper()
            if verb in ("MAP", "MATCH", "ADDROUTE", "ROUTE", "ALL"):
                verb = "ANY"
            url = match.group("path").strip()
            # A path literal is a path. Filters out `->get('id')` on some unrelated object,
            # which is otherwise the same shape.
            if not url.startswith("/") and not url.startswith("^"):
                continue
            # Bounded by the statement, not by a character count. A fixed window ran past the
            # end of the call and into the next route, so `$router->post('/login', [...])`
            # picked up the `['auth']` belonging to a route two lines below it — and middleware
            # here is authorization. A converted app that requires authentication to reach its
            # login page is worse than one with no middleware list at all.
            tail = code[match.end():match.end() + 400].split(";", 1)[0]
            handler = _ROUTE_HANDLER.search(tail)
            target = (f"{handler.group('class').rsplit(chr(92), 1)[-1]}."
                      f"{handler.group('method')}" if handler else "")
            guards = _ROUTE_MIDDLEWARE.search(tail)
            middleware = (" [" + ", ".join(
                sorted({g.strip(" '\"") for g in guards.group(1).split(",") if g.strip()})
            ) + "]") if guards else ""
            value = f"{verb} {url}" + (f" -> {target}" if target else "") + middleware
            if value in seen_routes:
                continue
            seen_routes.add(value)
            rows.append(_row(path, "route", value, _line_of(code, match.start()),
                             _snippet(content, match.start(), match.end()),
                             confidence=0.9))
            if len(seen_routes) >= MAX_PER_FILE_PER_KIND:
                break

    stem = os.path.splitext(basename)[0]
    # A template is not an entry point, however it is named. The stem heuristic was written for
    # procedural PHP, where `index.php` in any directory really is a page you can request; in
    # an app with a front controller, `views/employees/index.php` is a fragment that only ever
    # runs because a controller included it. Counting all six of them inflated "entrypoints"
    # from one to thirteen, which feeds the entry-point coverage check in `reconcile` and the
    # crawl seed for parity recording — a seed list of unrequestable paths.
    in_template_dir = any(part in _TEMPLATE_DIRS for part in path.lower().split("/")[:-1])
    if (stem in _ENTRYPOINT_STEMS and not in_template_dir) or module_type == "entrypoint":
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
