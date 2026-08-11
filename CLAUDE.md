# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read `DECISIONS.md` first

`DECISIONS.md` (repo root) is the durable memory across sessions: architectural decisions and
what they rejected, known landmines, and corrections to earlier analysis. Read it before
proposing architecture or diagnosing something that looks like a bug — several things that
read as defects are documented, deliberate trade-offs.

Add a row when you decide something non-obvious, hit a landmine, or find that earlier analysis
was wrong. Keep entries to one line, cite `file.py:line` rather than restating code, and delete
rows once they are resolved — the file is only useful while it stays scannable.

## Project overview

LM AI-SDLC ReviewOps Agent — an AI-powered governance/review platform covering the full SDLC (requirement → architecture → dev → QA → security → ops → governance) via ~25 specialized agents (Code Review, Code Generation, Security/Threat Model, Auto-Fix & Open PR, Issue Triaging, Compliance Mapper, Governance Report, etc.), organized into 9 "Intelligence" modules under `backend/agents/<module>/`. Backend is FastAPI + Python, orchestrating calls to Anthropic Claude. Frontend is a plain HTML/CSS/JS SPA with no build step, served as static files by FastAPI.

## Running the app

```
pip install -r requirements.txt
cp .env.example .env      # then set ANTHROPIC_API_KEY
python run.py
```

- Entry point is `run.py`, which runs `uvicorn.run("backend.main:app", ...)` on port **8000** (hardcoded), with `reload=True` watching only `backend/` (frontend static files don't need reload).
- There is no separate frontend dev server or build step — `frontend/` (`index.html`, `app.js`, `styles.css`, `logger.js`) is mounted directly at `/static` by `backend/main.py`.
- Interactive API docs available at `/docs` once running.

## Tests: smoke scripts, no framework

There is no test framework, linter/formatter config, or CI pipeline. Don't assume `pytest`, `ruff`, `eslint` etc. are available — check before suggesting a command that relies on them. Tests are plain scripts that exit 0/1 and print a PASS/FAIL line per check (see D-09 in `DECISIONS.md`); new tests should follow that shape rather than introduce a framework.

```
python scripts/smoke_products.py        # registry, entitlement, quota, metering, project scoping
python scripts/smoke_code_review.py     # chunking, gate floor, evidence verification, waivers
python scripts/smoke_modernization.py   # run lifecycle, dependency order, crash/resume
python scripts/smoke_governance.py      # Core SDLC Review stages, reports, gate fan-in, swarm
node   scripts/smoke_render.js          # frontend result rendering (DOM-stubbed, no deps)
```

The Python ones drive the real app through FastAPI's `TestClient` against a throwaway SQLite file with `llm_gateway.call_tool` stubbed, so they need no server, no API key and no network — they exercise routing, auth, entitlement, quota and persistence, but never the model itself.

## Architecture

- `backend/main.py` — FastAPI app entrypoint, mounts `/static` → `frontend/`.
- `backend/agents/` — per-module agent implementations (`architecture_intelligence/`, `developer_intelligence/`, `devops_intelligence/` [stub], `governance_intelligence/`, `modernization_intelligence/`, `operations_intelligence/`, `qa_intelligence/`, `requirement_intelligence/`, `security_intelligence/`), all extending `base_agent.py`.
- `backend/routes/` — FastAPI routers (one per concern: `agents.py`, `auth.py`, `swarm.py`, `v1_agents.py` [external API surface], etc.).
- `backend/services/` — business logic; `orchestrator.py` (`SDLCOrchestrator`) is the central coordinator. Also: `llm_gateway.py`, `github_service.py`, `jira_service.py`, `guardrail_service.py`, `agent_guard.py` (cost/budget control), `knowledge_base.py`, `crypto_service.py`.
- `backend/services/modernization/` — the one subsystem with its own schema and its own durable
  execution model: whole-codebase conversion runs (ingest → dependency graph → charter → claim-and-lease
  worker loop → Tier-0 validation). Its DDL lives in `store.py` and is called from `database.init_db()`.
  Read its package docstring before changing anything in it.
- `backend/database.py` — raw SQLite data layer, no ORM, no migration tooling. DB file defaults to `sdlc_agent.db` at repo root.
- `backend/config.py` — env var loading (see below).
- `docs/` — per-agent reference docs and integration guides (`n8n-workflows/`, `vscode-integration/`).

## Environment variables

Required: `ANTHROPIC_API_KEY`. Optional: `GITHUB_TOKEN` (fine-grained PAT, "Pull requests: Read and Write" — needed for GitHub PR/Auto-Fix features; public repos/PRs/issues work without it), `DATABASE_PATH` (default `sdlc_agent.db`), `CLAUDE_MODEL` (default `claude-sonnet-4-6`), `MAX_TOKENS`, `APP_ENV`, `SECRET_KEY` (JWT/crypto — must be changed for production). Also present in `backend/config.py` but not in `.env.example`: `JIRA_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN`/`JIRA_PROJECT` (optional Jira integration), `DEFAULT_ADMIN_PASSWORD` (defaults to a hardcoded value — flag this if touching real deployment config), `COST_OPTIMIZATION_ENABLED`, `MAX_AGENT_INPUT_CHARS`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`.

## Windows-specific note

`requirements.txt` deliberately pins `litellm<1.92.0` — from 1.92.0 onward litellm ships a Rust extension with no prebuilt Windows wheel, which would otherwise require an MSVC toolchain to install. Don't bump this pin without checking Windows wheel availability. `headroom-ai` (cost optimization) also compiles a Rust extension; if it fails to install, cost-optimization compression fails open (no-ops) rather than breaking the app.
