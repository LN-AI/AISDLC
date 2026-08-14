"""PHP → .NET: the transformations that are wrong in a way that compiles.

Every entry here was chosen by one test: does translating the syntax faithfully produce C#
that builds, runs, and does something *different*? A `strpos` mapped to `IndexOf` compiles
perfectly and inverts a condition. That is the failure this file exists to prevent — not the
ones a compiler catches.

Content, not process: `playbooks/__init__.py` explains why this is Python constants and how
sections are selected and rendered.
"""
from backend.services.modernization.playbooks import Playbook, Section, register

CORE = Section(
    key="core",
    title="Semantics that differ silently",
    body="""
**`==` is not `==`.** PHP's `==` coerces; C#'s does not. `"1" == "01"` is `true` in PHP and
`false` in C#; `"abc" == 0` is `true` before PHP 8 and `false` from PHP 8. Map `===` to `==`
and, where the source used `==` across different types, call the shared `PhpCompat.LooseEquals`
helper rather than re-deriving the rule per file. `switch` in PHP compares loosely; `in_array`
without a third argument compares loosely; `if ($x)` is false for `0`, `"0"`, `""`, `[]` and
`null` — `"0"` in particular is *not* falsy in C#.

**`null` is not the failure value.** PHP functions signal failure with `false`, not `null`:
`strpos`, `array_search`, `fopen`, `mysqli_query`, `DateTime::createFromFormat`. Never map a
PHP failure-`false` to a C# `false` — map it to the target's failure idiom per the charter's
error handling. `json_decode` is the exception and does return `null`.

**`strpos` returns `false`, not `-1`.** `if (strpos($a, $b))` is a *bug* in the source when
the match is at offset 0, and `if (a.IndexOf(b) != 0)` faithfully reproduces the bug while
looking correct. Use `Contains` where the intent is presence, and say so in `changes`.

**Length is three different things.** `strlen` counts bytes, `mb_strlen` counts characters,
`.Length` counts UTF-16 code units. Where the source did byte arithmetic, use
`Encoding.UTF8.GetByteCount`. `substr($s, -3)` is `s[^3..]`.

**Arrays are one type doing three jobs.** Integer keys `0..n-1` with `[]=` → `List<T>`.
String keys → `Dictionary<string, T>` — but **PHP arrays preserve insertion order and
`Dictionary<K,V>` does not**, so where iteration order is observable use
`List<KeyValuePair<K,V>>` or an ordered structure. A fixed set of string keys (`$row['id']`,
`$row['name']`) is a record, not a dictionary — that is the `mysql_fetch_assoc` shape and it
maps to the entity. `count(null)` throws from PHP 8; `array_map`/`array_filter`/`usort` → LINQ.

**`exit` and `die` are returns.** `header('Location: /x'); exit;` is
`return Redirect("/x");` — never `Environment.Exit`, which kills the web server.
""".strip(),
)

SUPERGLOBALS = Section(
    key="superglobals",
    title="Superglobals become bound parameters, not a global bag",
    applies=lambda f: bool(f.get("superglobals")),
    body="""
| PHP | .NET |
|---|---|
| `$_GET['id']` | an action parameter — `[FromQuery] int id`, or a route value |
| `$_POST['x']` | `[FromForm]`, or a bound view-model with validation attributes |
| `$_REQUEST['x']` | pick one deliberately; the ambiguity is a defect worth naming in `manual_followup` |
| `$_SERVER['HTTP_*']` | `HttpContext.Request.Headers` — behind a proxy these are not trustworthy |
| `$_SERVER['REQUEST_URI']` | `HttpContext.Request.Path` + `QueryString` |
| `$_COOKIE['x']` | `Request.Cookies["x"]` |
| `$_FILES['f']` | `IFormFile` |
| `$_ENV` / `getenv()` | `IConfiguration`, per the charter's configuration strategy |
| `$GLOBALS` | a registered service. If it is genuinely mutable global state, say so — do not hide it in a static |

The *keys* are the contract. You are shown which keys each file reads; infer the type from
how the value is used (`intval($_GET['id'])` is an `int`) and bind it, rather than reading a
string out of a dictionary and re-parsing it in every action.
""".strip(),
)

SESSIONS = Section(
    key="sessions",
    title="Sessions and authentication",
    applies=lambda f: bool(f.get("sessions")),
    body="""
**`session_start()` is not per-file.** It appears at the top of every page in a legacy app
because PHP has no startup file. In ASP.NET Core, `AddSession()` and `UseSession()` are called
**once, in `Program.cs`**, which the scaffold already contains. Emitting `AddSession()` in
forty converted files is the single most common way a converted PHP application fails to
start — do not do it, and do not add `HttpContext.Session` plumbing to a file whose only
session use was `session_start()`.

`$_SESSION['x']` → `HttpContext.Session.GetString("x")` / `SetString`. Note the shape change:
PHP session values are arbitrary serialised PHP; ASP.NET session values are byte arrays, so
anything non-scalar needs an explicit serialisation you should name in `changes`.

`$_SESSION['user_id']` guarding a page is **authentication**, not storage. The right target is
cookie authentication plus `[Authorize]`, not a session key read at the top of every action —
but that is an architectural decision. If the charter has not made it, convert it faithfully
and raise it in `manual_followup`; do not choose silently.
""".strip(),
)

DATA = Section(
    key="data",
    title="mysqli / PDO → the charter's data access",
    applies=lambda f: bool(f.get("db_apis")),
    body="""
| PHP | .NET |
|---|---|
| `mysqli_connect(...)` | the `DbContext` (or connection) registered in DI — **never constructed per file** |
| `mysqli_query($c, $sql)` | `FromSql`/`ExecuteSqlInterpolatedAsync`, or a LINQ query |
| `mysqli_fetch_assoc()` in a `while` | `await ... .ToListAsync()`, iterated |
| `mysqli_real_escape_string` + concatenation | **parameters** — and note the injection in `changes` |
| `mysqli_insert_id()` | the entity's key after `SaveChangesAsync()` |
| `mysqli_num_rows` / `affected_rows` | `Count` / the `SaveChangesAsync` return |
| `mysql_pconnect` | delete it. Pooling is automatic and a persistent connection is a bug here |

**Concatenated SQL is both an injection and a mapping signal.** You are told which call sites
built their SQL by concatenation. Every one becomes a parameterised query — there is no
variant of this where the concatenation is preserved — and each one is a `changes` entry
saying so.

`mysql_*` (no `i`) was removed in PHP 7. Its presence dates the code and means nothing about
it has been maintained recently; treat surrounding assumptions with suspicion.
""".strip(),
)

PRESENTATION = Section(
    key="presentation",
    title="Pages, includes and layout",
    applies=lambda f: bool(f.get("html")),
    body="""
A legacy PHP page is markup, a query and controller logic in one file, and it does **not**
convert to one C# file. Split it:

- the query and the branching → a controller action;
- the markup → a `.cshtml` view;
- the shape passed between them → a view-model, not `ViewBag`.

**`header('Location: ...')` before any output means there is no view at all** — that path is a
`RedirectToAction`. A file that emits `Content-Type: application/json` and `echo json_encode`
is an API endpoint returning a typed result, with no Razor involved.

**Includes are the layout.** `include 'header.php'` at the top and `include 'footer.php'` at
the bottom of every page is a layout: it becomes **one** `_Layout.cshtml` with `@RenderBody()`,
emitted once by the component that owns the header/footer, and the pages simply stop
mentioning it. A fragment included mid-page is `<partial name="_Foo" />`. An include whose
file only declares functions or classes is **not presentation at all** — it becomes a class,
and the include disappears into dependency injection.

`ob_start()` / `ob_get_clean()` around an include was a templating workaround; usually the
right answer is to delete it and render a partial.
""".strip(),
)

DATES = Section(
    key="dates",
    title="Dates and time zones",
    body="""
`date()` format characters are not .NET format strings. `Y`→`yyyy`, `m`→`MM`, `d`→`dd`,
`H`→`HH`, `i`→`mm`, `s`→`ss`, `D`→`ddd`, `M`→`MMM`. `N`, `w`, `z`, `t`, `L` have no format
equivalent and become expressions. `time()` → `DateTimeOffset.UtcNow.ToUnixTimeSeconds()`.

`strtotime` accepts far more than `DateTime.Parse` does; anywhere it parsed user or database
input, mark the conversion `// VERIFY:` and list it in `manual_followup`.

**`date_default_timezone_set()` is process-global and has no .NET equivalent.** Convert to
`DateTimeOffset` in UTC and apply `TimeZoneInfo` at the display boundary. A MySQL `DATETIME`
carries no offset, so a value read from the database arrives as `DateTimeKind.Unspecified` —
every `date()` applied to a database value is a place where the legacy behaviour depended on
the server's timezone setting.
""".strip(),
)

HAZARDS = Section(
    key="hazards",
    title="Constructs you must not translate",
    applies=lambda f: bool(f.get("hazard_kinds")),
    body="""
Some constructs have no safe mechanical equivalent, and producing a plausible one is worse
than producing nothing: it looks converted, it passes review, and it changes behaviour.

Where you are told a file contains one of these, emit a comment at that exact location
beginning `// MIGRATION-BLOCKED:`, leave the behaviour **unimplemented**, add it to
`manual_followup`, and set `needs_human_review`. The run checks for that marker and rejects
output that quietly resolved one of these instead.

- `eval()`, `create_function()`, `assert('...')` — arbitrary code from a string.
- `$$name`, `${$name}` — variable variables. There is no C# equivalent and the usual guess (a
  dictionary) changes the scoping rules.
- `include $var` — a dynamic include. This is routing, and it has to be made explicit.
- `extract()`, `import_request_variables()` — writes to the symbol table from user input.
- `unserialize()` on request data — remote code execution, not a conversion problem.
- `exec`/`system`/`shell_exec`/`proc_open` — shells out. Converting it moves the shell to a
  new host with different privileges.

Do not "improve" these either. Reporting an `eval` you did not convert is a useful result;
converting it into a `switch` that covers the three cases you could see is a silent behaviour
change nobody will find.
""".strip(),
)

register(Playbook(
    source="PHP",
    target="dotnet",
    sections=(CORE, SUPERGLOBALS, SESSIONS, DATA, PRESENTATION, DATES, HAZARDS),
))
# The same corpus applies to any .NET-shaped target string.
for alias in ("aspnetcore", "aspnet", "net8", "net9", "csharp", "netcore"):
    register(Playbook(source="PHP", target=alias,
                      sections=(CORE, SUPERGLOBALS, SESSIONS, DATA, PRESENTATION, DATES, HAZARDS)))
