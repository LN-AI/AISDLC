# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## How to explain your work

Every answer, every proposal and every finding needs four things. Not as headings to fill in —
as things the reader must not have to ask for.

**1. Plain language first.** Lead with what happened and what it means for the user, in words
that assume no knowledge of this codebase, of .NET, of PHP, or of the platform's own
vocabulary. "The converted app compiles but crashes the moment you start it" before
`AmbiguousMatchException`. Domain terms are fine *after* the plain sentence, never instead of
it. If a term is unavoidable, define it once in passing.

**2. The reasoning, not just the conclusion.** Say why this and not the alternative you
considered. A recommendation with no visible reasoning cannot be disagreed with, which makes
the user a rubber stamp rather than a decision maker — and they are the one who knows things
you do not.

**3. What could go wrong.** State the weaknesses of your own suggestion: what it does not
cover, what it assumes, what evidence it rests on and how thin that evidence is (`n=1` is worth
saying out loud). If you are choosing where to intervene in a chain of causes, say why that
point and not one further upstream. A proposal presented without its downsides is a sales
pitch.

**4. How you intend to build it, briefly.** A few sentences of mechanism — which files, what
shape, what the new thing does when it fails — before writing code. Enough that the user can
stop you if the approach is wrong, and not so much that it is the code written twice.

The same applies to explaining *findings*, not only proposals: lead with the consequence, then
cite `file.py:line`. And never use the platform's own conventions as though they were common
knowledge — a checkpoint, a gate, a unit, a seam and a component all mean something specific
here and nothing anywhere else.

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

There is no test framework, linter/formatter config, or CI pipeline (no `.github/` either). Don't assume `pytest`, `ruff`, `eslint` etc. are available — check before suggesting a command that relies on them. Tests are plain scripts that exit 0/1 and print a PASS/FAIL line per check (see D-09 in `DECISIONS.md`); new tests should follow that shape rather than introduce a framework. These seven scripts are the *entire* regression net — there is no CI and nothing else to fall back on, and the pass counts quoted in `DECISIONS.md`'s history table are this project's only record that a change was verified.

```
python scripts/smoke_products.py        # registry, entitlement, quota, metering, project scoping
python scripts/smoke_code_review.py     # chunking, gate floor, evidence verification, waivers
python scripts/smoke_modernization.py   # run lifecycle, dependency order, crash/resume (much the longest)
python scripts/smoke_governance.py      # Core SDLC Review stages, reports, gate fan-in, swarm
python scripts/smoke_llm_providers.py   # admin LLM connections: key never returned, resolution order
node   scripts/smoke_render.js          # frontend result rendering (DOM-stubbed, no deps)
node   scripts/smoke_plan_picker.js frontend/app.js products.json    # Plans screen product picker
```

The Python ones drive the real app through FastAPI's `TestClient` against a throwaway SQLite file with `llm_gateway.call_tool` stubbed, so they need no server, no API key and no network — they exercise routing, auth, entitlement, quota and persistence, but never the model itself. Two exceptions worth knowing before you run them:

- `smoke_llm_providers.py` stubs the *provider* functions (`_call_anthropic` / `_call_openai`) rather than `call_tool`, because the resolution logic under test lives inside `call_tool` — stubbing that would stub out the thing being tested.
- `smoke_plan_picker.js` takes a `products.json` argument (a saved `GET /api/v1/products` response) and **there is no such file in the repo**, so it dies with `ENOENT` until you capture one from a running server. `smoke_render.js` needs no argument — it defaults to `frontend/app.js`.

Everything else under `scripts/` is tooling, not tests; don't run it expecting a free offline check. `eval_code_review.py` measures the agent against a labelled corpus and **spends real money**. `build_run.py` compiles a converted codebase with the local .NET SDK and posts the verdict back — the app itself prints that command into modernization checkpoints and the UI (`build_results.py`, `runtime.py`, `frontend/app.js`), so it has to keep working. `drive_modernization.py` walks a run through the live HTTP API as the browser does; `audit_source_analysis.py` reports what the deterministic layer concludes about a zip before any spend.

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

Required: `ANTHROPIC_API_KEY`. Optional: `GITHUB_TOKEN` (fine-grained PAT, "Pull requests: Read and Write" — needed for GitHub PR/Auto-Fix features; public repos/PRs/issues work without it. **Also "Contents: Read and Write"** if you use the modernization PR workflow, which pushes a converted tree to a branch rather than only commenting), `DATABASE_PATH` (default `sdlc_agent.db`), `CLAUDE_MODEL` (default `claude-sonnet-4-6`), `MAX_TOKENS`, `APP_ENV`, `SECRET_KEY` (JWT/crypto — must be changed for production). Also present in `backend/config.py` but not in `.env.example`: `JIRA_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN`/`JIRA_PROJECT` (optional Jira integration), `DEFAULT_ADMIN_PASSWORD` (defaults to a hardcoded value — flag this if touching real deployment config), `COST_OPTIMIZATION_ENABLED`, `MAX_AGENT_INPUT_CHARS`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `PUBLIC_BASE_URL` (only for the modernization build loop — the address a GitHub Actions runner posts diagnostics back to; it cannot be derived from the request behind a proxy).

## Windows-specific note

`requirements.txt` deliberately pins `litellm<1.92.0` — from 1.92.0 onward litellm ships a Rust extension with no prebuilt Windows wheel, which would otherwise require an MSVC toolchain to install. Don't bump this pin without checking Windows wheel availability. `headroom-ai` (cost optimization) also compiles a Rust extension; if it fails to install, cost-optimization compression fails open (no-ops) rather than breaking the app.
