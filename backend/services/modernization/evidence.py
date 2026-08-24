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
    "auth",          # how a user is identified, and how that state is carried
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
    # A reserved global, not a variable anybody picks, so the name alone is exact. A plugin
    # reaches its database through this and nothing else, and reported no datastore at all.
    ("WordPress ($wpdb)", re.compile(r"\$wpdb\s*->")),
    # The two ORMs that account for most PHP written since 2015. Neither leaves a statement
    # anywhere for the SQL-shaped rules to find.
    ("Eloquent", re.compile(r"(?i)\bextends\s+\\?(?:Illuminate\\Database\\Eloquent\\)?Model\b"
                            r"|\bIlluminate\\Database\b")),
    ("Doctrine", re.compile(r"(?i)\bDoctrine\\ORM\b|\bextends\s+\\?ServiceEntityRepository\b")),
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

# How the application knows who is asking, and how it remembers between requests.
#
# Its own kind rather than a hazard or an integration, because it answers a charter question
# nothing else could: `auth_model` was `applies_when=None` and therefore never required, so a
# codebase with sessions and a password check could be converted with nobody having decided
# how either becomes the target's equivalent. Every one of these is also a thing that compiles
# perfectly while being wrong — a rehashed password locks out every existing user, and a
# session that silently becomes a cookie changes who can read it.
#
# Matched narrowly, on the call rather than on the word: `password` appears in a hundred
# variable names and only `password_verify(` tells you a credential is being checked.
_AUTH_MARKERS = (
    ("session", re.compile(r"\bsession_start\s*\(|\$_SESSION\s*\[")),
    ("password_check", re.compile(r"\bpassword_verify\s*\(")),
    ("password_hash", re.compile(r"\bpassword_hash\s*\(|\bcrypt\s*\(")),
    ("cookie", re.compile(r"\bsetcookie\s*\(")),
    ("http_auth", re.compile(r"\$_SERVER\s*\[\s*['\"]PHP_AUTH_")),
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
    (get|post|put|patch|delete|options|head|any|all|map|match|addroute|route|add)
    \s* \(\s*
    (?: \[?\s* ['"](?P<verb>GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)['"] \s*\]? \s*, \s* )?
    ['"](?P<path>[^'"\n]{1,200})['"]
    """)

# Methods whose name is not evidence of anything on its own. `->add('x')` and `->route('x')`
# appear on collections, containers and query builders far more often than on routers, so they
# count only when the call also names an HTTP verb first — `$r->add('GET', '/api/x', …)`.
# Without that requirement this pattern reports a route for every `$config->add('debug', …)`.
_GENERIC_ROUTE_METHODS = {"add", "route", "map", "match", "addroute"}

# PHP 8 attribute routing, which is how Symfony has declared routes since 5.2 and how a
# Laravel or Slim application increasingly does too. It is not a method call at all, so the
# shape above cannot see it — and a Symfony application reported **zero URLs**, which is the
# same failure D-83 fixed for route tables, arriving through a different door.
_ATTRIBUTE_ROUTE = re.compile(
    r"""(?ix)
    \#\[\s* (?:\\?[\w\\]*\\)? Route \s* \(\s*
    (?: (?:path\s*:\s*)? )?
    ['"](?P<path>[^'"\n]{1,200})['"]
    # To the end of the line, not to the first `]` — `methods: ['GET']` contains one, so
    # stopping there read every attribute route as answering any verb.
    (?P<tail>[^\n]{0,300})
    """)
_ATTRIBUTE_VERBS = re.compile(r"""(?ix) methods \s* : \s* \[ (?P<verbs>[^\]]{0,120}) \]""")
# The method that follows the attribute, which is the handler it decorates.
_ATTRIBUTE_HANDLER = re.compile(r"""(?ix) function \s+ (?P<method>\w+) \s* \(""")
_CLASS_DECL = re.compile(r"""(?ix) \b class \s+ (\w+)""")

# A second argument that looks like a request handler: `'Employee::index'`, `'Ctrl@show'`,
# `[Ctrl::class, 'show']`, or a closure. This is what makes it safe to accept a route path
# with no leading slash — CodeIgniter writes `$routes->get('employees', 'Employee::index')`,
# and refusing every relative path (the old rule) reported one route on a table of four,
# while accepting every relative path would report `->get('id')` on any object as a URL.
_ROUTE_HANDLER_ARG = re.compile(
    r"""(?ix) ^\s*,\s* (?: ['"][\w\\]+ (?:::|@) \w+[^'"\n]{0,40}['"] | \[ | function \s*\( |
                           fn \s*\( | \\? [\w\\]+ ::\s*class )""")

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
    # PHP/shell line comment — but `#[` is a PHP 8 *attribute*, not a comment, and blanking
    # it erased every `#[Route('/employees', methods: ['GET'])]` in a Symfony codebase before
    # anything could read it. A Symfony application reported zero URLs for this one character.
    r"|\#(?!\[)[^\n]*"
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
         confidence: float = 1.0, source: str = "parser",
         value_default: str = "") -> Dict[str, Any]:
    return {"path": path, "kind": kind, "value": value, "line": line,
            "snippet": snippet, "confidence": confidence, "source": source,
            # Only `config_key` uses this: the literal the source read, so the target's
            # settings file can carry the same value rather than a marker.
            "value_default": value_default}


# Settings a codebase states outright, which is where most of them live. `__REDACTED_*__` was
# the only source of `config_key`, so anything that was never a secret contributed nothing —
# and `appsettings.json` was written without one key the application actually reads (C-70).
#
# Narrow on purpose. Each pattern needs a quoted name *and* a scalar literal beside it, which is
# what separates a configuration entry from any other array in the file: a list of column names
# has no `=>` scalar, and `['users', 'orders']` has no key. A missed setting is a gap; an
# invented one puts a key in the target's settings file that nothing reads.
_CONFIG_SETTING_PATTERNS = (
    # `'timezone' => 'Asia/Kolkata'` — the shape of every `return [...]` config file in PHP.
    re.compile(r"""['"](?P<key>[A-Za-z_][\w.\- ]{1,60})['"]\s*=>\s*"""
               r"""(?P<value>'[^'\n]{0,180}'|"[^"\n]{0,180}"|true|false|null|-?\d+(?:\.\d+)?)""",
               re.IGNORECASE),
    # `define('SESSION_NAME', 'ems')`
    re.compile(r"""\bdefine\s*\(\s*['"](?P<key>[A-Za-z_]\w{1,60})['"]\s*,\s*"""
               r"""(?P<value>'[^'\n]{0,180}'|"[^"\n]{0,180}"|true|false|-?\d+(?:\.\d+)?)""",
               re.IGNORECASE),
    # `$_ENV['APP_DEBUG']`, `getenv('APP_URL')` — read with no literal to preserve.
    re.compile(r"""\$_(?:ENV|SERVER)\s*\[\s*['"](?P<key>[A-Za-z_]\w{1,60})['"]\s*\]"""),
    re.compile(r"""\bgetenv\s*\(\s*['"](?P<key>[A-Za-z_]\w{1,60})['"]\s*\)""", re.IGNORECASE),
)

# A configuration key, not a sentence and not a SQL fragment. Spaces are allowed because some
# codebases use them, but anything with punctuation beyond dot, dash and underscore is prose.
_CONFIG_NAME = re.compile(r"^[A-Za-z_][\w.\- ]*$")

# A path that is a *pattern* rather than a URL. A rewrite rule is a regex and a framework route
# may be templated; neither is something a smoke check can request.
_NOT_A_URL = re.compile(r"[\^\$\*\+\?\(\)\[\]\\|]")
_TEMPLATED = re.compile(r"[{}]|:[A-Za-z_]")

# A `route` row's `value`, which is written in exactly two shapes:
#   `GET /employees -> EmployeeController.index [auth]`   (declared in code)
#   `^employees/?$ -> employees.php`                      (a rewrite or alias)
_ROUTE_VALUE = re.compile(r"^\s*(?P<verb>[A-Z]+)\s+(?P<path>/\S*)")

# A rewrite target that is a script rather than a handler name. Extensions only — a bare word
# is a controller or an alias, and requesting it would invent a URL the source never had.
_SCRIPT_TARGET = re.compile(r"^[\w./-]+\.(?:php|phtml|asp|aspx|cgi|pl|jsp|do)$", re.IGNORECASE)


def route_urls(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Route evidence turned into things a startup check can actually request.

    The one reader of a `route` row's `value` for this purpose. `parity.invite` deliberately
    prints the raw values instead — a human deciding which journeys to record wants the handler
    and the middleware list, and stripping those to a bare URL would make the invitation worse.
    This answers a narrower question: which of them can be requested by a machine.

    Every row comes back, `probe` saying whether it can be requested. Nothing is silently
    dropped — an unprobed URL is a gap in the check's coverage, and a coverage figure that
    quietly excluded what it could not reach would be the absence-looks-like-evidence failure
    this subsystem keeps closing.

    Three things are deliberately not probed:

    * **Patterns.** `^employees/?$` is a regex from a rewrite rule, not a URL.
    * **Templates.** `/employees/{id}` needs a value nobody here knows; guessing `1` invents a
      test rather than reproducing one.
    * **Anything but GET and HEAD.** A POST with no body is a 400, which is the app working
      correctly and would read as a failure.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows or ():
        if (row.get("kind") or "route") != "route":
            continue
        value = str(row.get("value") or "").strip()
        if not value:
            continue
        method, path, reason = "", "", ""
        match = _ROUTE_VALUE.match(value)
        if match:
            method = match.group("verb").upper()
            path = match.group("path")
        else:
            # A rewrite or alias: the left side is the pattern, the right the target. Only the
            # target can be requested, and only when it is a path rather than a handler name.
            target = value.split("->", 1)[-1].strip() if "->" in value else value
            target = target.split()[0] if target else ""
            if target.startswith("/"):
                method, path = "GET", target
            elif _SCRIPT_TARGET.match(target):
                # `^employees/?$ -> employees.php` names the script the rewrite hands off to,
                # and in a PHP application that script is itself a URL. It is also the URL the
                # charter's `url_compatibility` decision promises still answers after
                # conversion, so it is the most valuable thing in the file to check.
                method, path = "GET", "/" + target.lstrip("/")
            else:
                out.append({"method": "GET", "path": target or value, "probe": False,
                            "reason": "not a requestable URL",
                            "source_path": row.get("path") or "", "line": row.get("line") or 0})
                continue
        path = path.split("#", 1)[0]
        entry = {"method": method or "GET", "path": path, "probe": True, "reason": "",
                 "source_path": row.get("path") or "", "line": row.get("line") or 0}
        if _NOT_A_URL.search(path):
            entry.update(probe=False, reason="the path is a pattern, not a URL")
        elif _TEMPLATED.search(path):
            entry.update(probe=False, reason="the path is templated and needs a value")
        elif entry["method"] == "ANY":
            entry["method"] = "GET"
        elif entry["method"] not in ("GET", "HEAD"):
            entry.update(probe=False,
                         reason=f"{entry['method']} with no body is a 400, not a defect")
        key = (entry["method"], entry["path"], entry["probe"])
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)

    # The application's own front door. `_program_cs` maps `{controller=Home}/{action=Index}`,
    # so `/` answers on every generated host whether or not the source declared it — and a run
    # whose every other URL is templated would otherwise have nothing to check at all.
    if not any(e["path"] == "/" and e["probe"] for e in out):
        out.insert(0, {"method": "GET", "path": "/", "probe": True, "reason": "",
                       "source_path": "", "line": 0})
    return out


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

    for label, pattern in _AUTH_MARKERS:
        match = pattern.search(content)
        if match:
            rows.append(_row(path, "auth", label, _line_of(content, match.start()),
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
            # Bounded by the statement, not by a character count. A fixed window ran past the
            # end of the call and into the next route, so `$router->post('/login', [...])`
            # picked up the `['auth']` belonging to a route two lines below it — and middleware
            # here is authorization. A converted app that requires authentication to reach its
            # login page is worse than one with no middleware list at all.
            tail = code[match.end():match.end() + 400].split(";", 1)[0]
            # A generic method name is not evidence on its own; the HTTP verb is what makes
            # `->add('GET', '/x', …)` a route and `->add('debug', true)` not one.
            if match.group(1).lower() in _GENERIC_ROUTE_METHODS and not match.group("verb"):
                continue
            # A path literal is a path — but only a leading `/` says so on its own. A relative
            # path counts when the call also names a handler, which is how CodeIgniter and
            # several micro-frameworks write it: `$routes->get('employees', 'Employee::index')`.
            # Refusing those outright reported one route on a table of four; accepting them all
            # would report `->get('id')` on any object as a URL. Normalised to a leading slash,
            # because it is the same URL and `url_compatibility` compares these as strings.
            if not url.startswith(("/", "^")):
                if not _ROUTE_HANDLER_ARG.match(tail):
                    continue
                url = "/" + url.lstrip("/")
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

        # Attribute routing, which is not a call and so cannot be found by the shape above.
        # A Symfony application declares every one of its URLs this way and reported none.
        for match in _ATTRIBUTE_ROUTE.finditer(code):
            if len(seen_routes) >= MAX_PER_FILE_PER_KIND:
                break
            url = match.group("path").strip()
            if not url.startswith("/"):
                continue
            verbs = _ATTRIBUTE_VERBS.search(match.group("tail") or "")
            listed = sorted({v.strip(" '\"").upper() for v in verbs.group("verbs").split(",")
                             if v.strip()}) if verbs else ["ANY"]
            # The handler is the method the attribute decorates, which is the next `function`
            # after it — the one thing about an attribute that is positional rather than named.
            # The class is the last one declared before it, so the row reads the same way a
            # route-table row does rather than naming a bare method.
            after = code[match.end():match.end() + 400]
            handler = _ATTRIBUTE_HANDLER.search(after)
            owner = _CLASS_DECL.findall(code[:match.start()])
            target = (f"{owner[-1] if owner else ''}.{handler.group('method')}".lstrip(".")
                      if handler else "")
            for verb in listed or ["ANY"]:
                value = f"{verb} {url}" + (f" -> {target}" if target else "")
                if value in seen_routes:
                    continue
                seen_routes.add(value)
                rows.append(_row(path, "route", value, _line_of(code, match.start()),
                                 _snippet(content, match.start(), match.end()),
                                 confidence=0.9))

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

    # Settings that were never secrets, which is most of them (C-70). Placeholders were the
    # only source of `config_key`, so a codebase keeping its configuration in a plain array —
    # `config/config.php` returning `timezone`, `session_name`, a database path — contributed
    # none at all. `appsettings.json` was then written without one key the source actually
    # reads, and every unit invented its own name for the same setting: exactly the per-file
    # divergence the charter's configuration decision exists to prevent. Worse for the startup
    # check, a key the host reads and cannot find is a null at boot.
    #
    # The literal default is captured with the name, and deliberately: a non-secret default read
    # out of the source is not a credential, and it is the only value that reproduces the old
    # behaviour. Anything that looks like a secret has already been replaced by a placeholder
    # above, so what reaches here is a timezone or a path.
    if is_code:
        for pattern in _CONFIG_SETTING_PATTERNS:
            for match in pattern.finditer(code):
                name = (match.group("key") or "").strip()
                if not name or len(name) > 80 or not _CONFIG_NAME.match(name):
                    continue
                default = (match.groupdict().get("value") or "").strip().strip("'\"")
                if "__REDACTED_" in default:
                    continue
                rows.append(_row(path, "config_key", name.lower(),
                                 _line_of(code, match.start()),
                                 _snippet(content, match.start(), match.end()),
                                 confidence=0.8,
                                 value_default=default[:200]))
                if sum(1 for r in rows if r["kind"] == "config_key") >= MAX_PER_FILE_PER_KIND:
                    break

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
