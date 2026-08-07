import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "sdlc_agent.db")
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production-32chars!!")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8000"))
APP_ENV = os.getenv("APP_ENV", "development")
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "Admin@1234")
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
UI_REQUIRE_PROJECT = os.getenv("UI_REQUIRE_PROJECT", "false").lower() == "true"

# Guardrails — applied uniformly to every agent call inside llm_gateway.call_tool().
MAX_AGENT_INPUT_CHARS = int(os.getenv("MAX_AGENT_INPUT_CHARS", "400000"))
# Several agents now request up to 16000 tokens (architecture, codegen-scaffold) or
# 14000 (vapt, security) which can legitimately take longer to finish generating —
# too short a timeout here causes a spurious APITimeoutError mid-generation rather
# than letting a genuinely slow-but-successful call complete. Long-running calls no
# longer block an HTTP connection for their full duration (see the job-status
# endpoint used by the `full` review), so a generous value here is safe.
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "360"))
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
