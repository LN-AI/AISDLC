import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "sdlc_agent.db")
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production-32chars!!")
_DEFAULT_SECRET_KEY = "change-this-in-production-32chars!!"
_DEFAULT_ADMIN_PASSWORD = "Admin@1234"
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
# The response budget for an agent that names none of its own. A floor, not a ceiling:
# most agents pass an explicit `max_tokens` (see base_agent.py and the per-agent modules),
# and the model's real ceiling is far higher — claude-sonnet-4-6 allows 128000 output
# tokens. Kept well below that because a budget only has to be larger than the longest
# legitimate response; every token of headroom above that is latency risk, not quality.
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "16000"))
APP_ENV = os.getenv("APP_ENV", "development")
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
# `or`, not a getenv default: `DEFAULT_ADMIN_PASSWORD=` present-but-empty in a .env is
# a real and likely state (someone copies .env.example and fills in only the API key),
# and `os.getenv(name, default)` returns "" for that, not the default — which would
# create the bootstrap admin with a **blank** password. Worse than the weak default it
# was meant to replace, and it would pass every check in this file.
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD") or "Admin@1234"
JIRA_URL = os.getenv("JIRA_URL", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT = os.getenv("JIRA_PROJECT", "")

# Cost Optimization (Headroom) — default state on first boot; overridable at
# runtime via PUT /api/v1/cost-optimization/settings, persisted in the DB.
COST_OPTIMIZATION_ENABLED_DEFAULT = os.getenv("COST_OPTIMIZATION_ENABLED", "true").lower() == "true"

# The legacy /api/review/* endpoints (which frontend/app.js uses) once accepted callers
# with no credentials at all, resolving them to "unrestricted". That is a bypass around
# every limit — a governed UI is pointless if the same endpoints answer to no credential —
# so the default is now true and they return 401 instead.
#
# Every one of these routes is called through app.js's apiFetch(), which attaches the
# bearer token whenever the user is logged in, so a signed-in session cannot 401 here.
# What does stop working is *anonymous* browser use: the login modal is dismissible, and
# a user who dismissed it could previously still run agents. They now get the modal back
# with "Sign in to use this feature." Set this to false only to restore that.
LEGACY_ROUTES_REQUIRE_AUTH = os.getenv("LEGACY_ROUTES_REQUIRE_AUTH", "true").lower() == "true"

# Governance binds to a project: an API key carries its project, a browser session sends
# the one the user picked (X-Project-Id). A UI caller with no project resolves to no plan
# and no budget — i.e. unrestricted — which is the right default for an internal console
# but a hole once customers get logins. Set this to true to reject UI agent calls that
# aren't bound to a project, so nothing can be run outside a governed allowance.
#
# Still `false`, and that is now a recorded choice rather than an unexamined one. Flipping
# it was considered as part of closing the unauthenticated-endpoint set and deliberately
# left alone: `smoke_products.py` section 14 pins this default with the reasoning "opt in,
# so no one is locked out on upgrade", and turning it on refuses every agent run from a
# session that has not picked a project — which is a change to what existing users can do,
# not a security fix. The endpoints that had no authorization at all were the actual hole,
# and they are closed regardless of this flag. See C-80.
UI_REQUIRE_PROJECT = os.getenv("UI_REQUIRE_PROJECT", "false").lower() == "true"

# Where *other systems* reach this deployment — specifically a GitHub Actions runner posting
# build diagnostics back to POST /api/v1/modernization/runs/{id}/build-results.
#
# It cannot be derived from the incoming request. `request.base_url` is whatever the proxy
# forwarded to (often `http://127.0.0.1:8000`), and a callback URL pointing at localhost
# produces a PR whose CI runs, fails to call back, and reports nothing — a silent failure
# that looks exactly like a build nobody triggered. Left empty, the route asks for the URL
# explicitly rather than guessing one.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Guardrails — applied uniformly to every agent call inside llm_gateway.call_tool().
MAX_AGENT_INPUT_CHARS = int(os.getenv("MAX_AGENT_INPUT_CHARS", "400000"))
# This has to be large enough for the largest output budget to actually finish
# generating. The top tier is now 32000 tokens (architecture, codegen-scaffold,
# test-case, unit conversion); at the 50-80 tokens/sec a Sonnet-class model sustains,
# that is 400-640 seconds of generation. Too short a timeout here turns a
# slow-but-successful call into an APITimeoutError *and* retries it, so the tokens are
# paid for twice more and no result is ever returned. Long-running calls no longer
# block an HTTP connection for their full duration (see the job-status endpoint used
# by the `full` review), so a generous value is safe on Anthropic.
#
# It is NOT safe on a slow local router: with LLM_MAX_RETRIES=2 a wedged endpoint can
# burn 3x this before erroring (C-30). Set `llm_connections.timeout_seconds` per
# connection rather than lowering this globally — lowering it globally reintroduces
# spurious timeouts on the large-budget agents.
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "900"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))

# Anthropic prompt caching — marks the system prompt + tool schema (a byte-identical
# prefix across every call to the same agent) as cacheable. Output is unchanged; only
# billing differs: a cache write costs 1.25x and a read 0.1x, so break-even is two
# calls within the 5-minute TTL. `full` (7 calls) and `swarm` (6 agents on one
# artifact) profit immediately. No effect on the OpenAI/BYOL path. Set false to
# disable if a provider or gateway rejects the cache_control block.
PROMPT_CACHE_ENABLED = os.getenv("PROMPT_CACHE_ENABLED", "true").lower() == "true"

# SQLite concurrency. Swarm runs 6 worker threads and background jobs run on their
# own threads, all opening separate connections to one file — without a busy timeout
# those collide as "database is locked" instead of waiting their turn.
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))

# Background jobs are scheduled in-process (asyncio.create_task), so a restart
# orphans every in-flight job as status='running' forever. On startup we reap jobs
# that belong to a dead process. See database.recover_orphaned_jobs().
JOB_STALE_MINUTES = int(os.getenv("JOB_STALE_MINUTES", "30"))



# ── Who may call this API from a browser ──────────────────────────────────
#
# Was `allow_origins=["*"]` with `allow_credentials=True`. Starlette echoes the requesting
# origin back when both are set, so *any* web page could make credentialed calls to this
# API — which mattered most for the routes that needed no credential at all (the Knowledge
# Base, the AI inventory and the audit trail, all now fixed): those turned a network-local
# problem into one a malicious page could trigger from a colleague's browser.
#
# The bundled SPA is served from this same origin and needs nothing here, so the default is
# A GitHub App, which is the only credential GitHub will accept for a **check run** — the
# thing that puts a finding on the line it is about, inside the diff. A personal access
# token can only set a commit status: a coloured dot with a description. That is an API
# restriction, not a gap here.
#
# Unset means the commit-status path, unchanged. See `backend/services/github_app.py` for
# what the App needs (Checks: write, Pull requests: write, Contents: read) and
# `docs/github-app.md` for how to create one.
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID", "")
GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
GITHUB_APP_PRIVATE_KEY_PATH = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "")

# Whether a push to an open pull request re-reviews the whole diff or only the commits
# that have arrived since the last review. On by default, and the reasoning is a trade
# rather than a free win:
#
#   * **For:** `synchronize` fires on every push, so an active 40-file pull request costs a
#     full review per push. That is the number that gets an integration switched off, and
#     the gate stays correct because still-open findings from earlier pushes are carried
#     into the new result and still vote (see `workflows/review_pr.py`).
#   * **Against:** files changed two pushes ago are not re-read, so a problem that only
#     appears in the interaction between old and new code is not seen until the next full
#     review. That is a genuine reduction in what a single run covers.
#
# `opened`, `reopened` and `ready_for_review` are always full reviews — those are the
# moments where scope matters more than cost. Set to 0/false for a full review on every
# push.
INCREMENTAL_PR_REVIEW = os.getenv("INCREMENTAL_PR_REVIEW", "true").strip().lower() not in (
    "0", "false", "no", "off")

# empty — no cross-origin browser access. Set `CORS_ALLOW_ORIGINS` to a comma-separated
# list of exact origins (scheme, host and port, no trailing slash) for a separately hosted
# front end. Server-to-server callers using `X-API-Key` are unaffected: CORS is a browser
# mechanism and never applied to them.
CORS_ALLOW_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]


def assert_production_secrets() -> None:
    """Refuse to boot a production deployment on the shipped default credentials.

    `SECRET_KEY` signs every JWT *and* derives the one key that encrypts every stored
    secret — LLM keys, GitHub tokens, webhook secrets and retained review source. On the
    default value all of those are decryptable by anyone holding a copy of this repository,
    and any JWT can be forged. `DEFAULT_ADMIN_PASSWORD` creates the bootstrap admin.

    Both had hardcoded fallbacks and nothing anywhere checked them, so a deployment could
    reach production still using both and look completely healthy. Enforced only when
    `APP_ENV=production`, so development, the smoke suites and `ui_examine.py` are
    untouched — they all rely on those defaults deliberately.
    """
    if APP_ENV != "production":
        return
    problems = []
    if SECRET_KEY == _DEFAULT_SECRET_KEY:
        problems.append(
            "SECRET_KEY is still the shipped default. It signs every session token and "
            "derives the encryption key for every stored credential. Set it to a random "
            "32+ character value. Note that changing it later makes already-stored "
            "credentials unreadable, so set it before storing any."
        )
    if DEFAULT_ADMIN_PASSWORD == _DEFAULT_ADMIN_PASSWORD:
        problems.append(
            "DEFAULT_ADMIN_PASSWORD is still the shipped default, which is published in "
            "this repository's own scripts. Set it, or create the first admin by hand."
        )
    if problems:
        raise RuntimeError(
            "Refusing to start with APP_ENV=production and default credentials:\n  - "
            + "\n  - ".join(problems)
        )
