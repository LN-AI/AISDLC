import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "sdlc_agent.db")
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production-32chars!!")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))
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

# Guardrails — applied uniformly to every agent call inside llm_gateway.call_tool().
MAX_AGENT_INPUT_CHARS = int(os.getenv("MAX_AGENT_INPUT_CHARS", "400000"))
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
