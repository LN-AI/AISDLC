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

- Entry point is `run.py`, which runs `uvicorn.run("backend.main:app", ...)` on `0.0.0.0:8000`
  with the reloader watching only `backend/` (frontend static files don't need reload).
  `HOST`, `PORT` and `RELOAD=0` override all three — **use `RELOAD=0 python run.py` rather than
  a bare `uvicorn backend.main:app`**, because that changes the bind address from `0.0.0.0` to
  `127.0.0.1` and is how the collision below happens.
- **`run.py` refuses to start when something is already serving that port**, naming the PID,
  its command line and its start time. This is not politeness: `0.0.0.0` and `127.0.0.1` are
  different addresses to Windows, so a stale process on `127.0.0.1:8000` does *not* stop a new
  bind on `0.0.0.0:8000` — both listen, the log says "Application startup complete", and the
  browser (resolving `localhost` to `127.0.0.1`) keeps talking to the old one. A restart then
  appears to do nothing. See D-243 and X-07.
- There is no separate frontend dev server or build step — `frontend/` (`index.html`, `app.js`, `styles.css`, `logger.js`) is mounted directly at `/static` by `backend/main.py`.
- Interactive API docs available at `/docs` once running.

## Tests: smoke scripts, no framework

There is no test framework, linter/formatter config, or CI pipeline (no `.github/` either). Don't assume `pytest`, `ruff`, `eslint` etc. are available — check before suggesting a command that relies on them. Tests are plain scripts that exit 0/1 and print a PASS/FAIL line per check (see D-09 in `DECISIONS.md`); new tests should follow that shape rather than introduce a framework. These eight scripts are the *entire* regression net — there is no CI and nothing else to fall back on, and the pass counts quoted in `DECISIONS.md`'s history table are this project's only record that a change was verified.

```
python scripts/smoke_products.py        # registry, entitlement, quota, metering, project scoping
python scripts/smoke_code_review.py     # chunking, gate floor, evidence verification, waivers
python scripts/smoke_review_sources.py  # zip/repo/branch intake, cost preview, per-file verification, VAPT substrate
python scripts/smoke_modernization.py   # run lifecycle, dependency order, crash/resume (much the longest)
python scripts/smoke_governance.py      # Core SDLC Review stages, reports, gate fan-in, swarm
python scripts/smoke_llm_providers.py   # admin LLM connections: key never returned, resolution order
node   scripts/smoke_render.js          # frontend result rendering (DOM-stubbed, no deps)
node   scripts/smoke_plan_picker.js frontend/app.js products.json    # Plans screen product picker
python scripts/ui_examine.py            # the real UI in a real browser — every review source, no model called
```

Two live drivers sit beside these. They are **not** part of the offline net: both spend real
money, and `verify_pr_review.py` writes to a real repository.

```
python scripts/eval_code_review.py                       # accuracy against the labelled corpus (~$0.20)
python scripts/verify_pr_review.py <pr-url>              # every GitHub write path, on a real PR
python scripts/verify_pr_review.py <pr-url> --dry-run    # ...the same, writing nothing
python scripts/verify_pr_review.py <pr-url> --cleanup    # remove what it posted
```

`eval_code_review.py` is the only thing that can tell an accuracy improvement from a
regression — the floors in it are measured, not guessed (D-245), so a prompt change that
looks better and scores worse gets caught. `verify_pr_review.py` exists because the offline
suites stub the model and never touch GitHub: they cannot tell you that an inline comment
landed on the line intended, and the worst defect of the session was invisible until a real
diff went through it (D-244).

`ui_examine.py` is the only one that drives a browser (Playwright + Chromium, both already in
`venv`). It starts its own server on a spare port with its own database and **calls no model** —
every case stops at the free cost estimate. `--with-review` runs a full review with the gateway
stubbed, `--headed` shows the browser, `--shots DIR` saves a screenshot per case, and
`--url http://localhost:8000` examines a server you are already running. It exists because the
DOM-stubbed `smoke_render.js` cannot see whether a button is wired to a function, whether an
element id in `index.html` matches the one `app.js` reads, or whether a click reaches the
server — and that gap is where the defects reported on 2026-09-02 were.

**Windows note:** run the Python ones with `venv/Scripts/python.exe`, not a bare `python` — the
system interpreter has none of the dependencies installed. Add `PYTHONIOENCODING=utf-8` when you
need to read output containing the em dashes and emoji these scripts print.

The Python ones drive the real app through FastAPI's `TestClient` against a throwaway SQLite file with `llm_gateway.call_tool` stubbed, so they need no server, no API key and no network — they exercise routing, auth, entitlement, quota and persistence, but never the model itself. Two exceptions worth knowing before you run them:

- `smoke_llm_providers.py` stubs the *provider* functions (`_call_anthropic` / `_call_openai`) rather than `call_tool`, because the resolution logic under test lives inside `call_tool` — stubbing that would stub out the thing being tested.
- `smoke_plan_picker.js` takes a `products.json` argument (a saved `GET /api/v1/products` response) and **there is no such file in the repo**, so it dies with `ENOENT` until you capture one from a running server. `smoke_render.js` needs no argument — it defaults to `frontend/app.js`.

Everything else under `scripts/` is tooling, not tests; don't run it expecting a free offline check. `eval_code_review.py` measures the agent against a labelled corpus and **spends real money**. `build_run.py` compiles a converted codebase with the local .NET SDK and posts the verdict back — the app itself prints that command into modernization checkpoints and the UI (`build_results.py`, `runtime.py`, `frontend/app.js`), so it has to keep working. `drive_modernization.py` walks a run through the live HTTP API as the browser does; `audit_source_analysis.py` reports what the deterministic layer concludes about a zip before any spend.

## Architecture

- `backend/main.py` — FastAPI app entrypoint, mounts `/static` → `frontend/`.
- `backend/agents/` — per-module agent implementations (`architecture_intelligence/`, `developer_intelligence/`, `devops_intelligence/` [stub], `governance_intelligence/`, `modernization_intelligence/`, `operations_intelligence/`, `qa_intelligence/`, `requirement_intelligence/`, `security_intelligence/`), all extending `base_agent.py`.
- `backend/routes/` — FastAPI routers (one per concern: `agents.py`, `auth.py`, `swarm.py`, `v1_agents.py` [external API surface], etc.).
- `backend/services/` — business logic; `orchestrator.py` (`SDLCOrchestrator`) is the central coordinator. Also: `llm_gateway.py`, `github_service.py`, `jira_service.py`, `guardrail_service.py`, `agent_guard.py` (cost/budget control), `knowledge_base.py`, `crypto_service.py`.
- **The review pipeline.** Code Review and VAPT share one implementation, and it is worth
  knowing the pieces before changing either: `source_bundle.py` turns any input (paste, diff,
  zip, GitHub repo at a ref, PR, `base...head`) into one `Bundle`; `code_chunker.py` splits it;
  `backend/agents/chunked_review.py` fans the chunks out and assembles the result;
  `finding_pipeline.py` verifies each finding's quote against the file it names, de-duplicates,
  suppresses test/vendor paths and fingerprints; `finding_gate.py` floors the gate so a model's
  own verdict can only ever be tightened. `review_estimate.py` prices a run before it happens,
  reusing `modernization/estimate.py`'s arithmetic so a quote and an invoice come from one
  price table. `sarif.py` exports a finished review in the format GitHub code scanning and
  most security dashboards read.

  **One screen reviews everything, including pull requests.** The separate GitHub PR screen
  is retired (D-267); its nav item opens the review screen with the source pre-set, and the
  post-back options appear only for a pull-request source. The *products* are still two: a
  run that will write to GitHub goes to `/api/github/pr/async` (`github-pr-review`), one that
  will not goes to `/api/review/code/source` (`code`) — merging the screens deliberately did
  not merge the entitlement, quota or budget.

  Two behavioural notes that surprise people: a review over four model calls returns **202 and
  a job id** (`/api/review/jobs/{id}` for progress) rather than blocking; and every screen
  asks for a **cost quote first** — `/preflight` is free, calls no model, and the run button
  names the ceiling it was approved against.

  Two more pieces sit behind a *finished* review. `review_source_store.py` records which
  files it read, which it did not and why (in its own two tables, never in `result_json` —
  see D-256), and keeps the source itself for a per-run retention window: 7 days by default,
  encrypted, and **never for a GitHub source**, which is re-read from the repository at the
  commit sha the review recorded. `fix_apply.py` is Apply Fixes — it starts from a review and
  the findings a person ticked, reuses the per-file fixer and *both* of Auto-Fix's guardrails
  unchanged, and delivers as a diff, a zip, a branch, or a branch and a pull request
  depending on what the source was. Two things to hold onto before changing either: a fix
  costs **per file, not per finding**, and nothing in the review pipeline ever compiles or
  runs the code it produces.
- `backend/services/modernization/` — the one subsystem with its own schema and its own durable
  execution model: whole-codebase conversion runs (ingest → dependency graph → charter → claim-and-lease
  worker loop → Tier-0 validation). Its DDL lives in `store.py` and is called from `database.init_db()`.
  Read its package docstring before changing anything in it.
- `backend/database.py` — raw SQLite data layer, no ORM, no migration tooling. DB file defaults to `sdlc_agent.db` at repo root.
  **New columns go in by `ALTER TABLE` inside `init_db`, wrapped in `try/except
  sqlite3.OperationalError`** — `CREATE TABLE IF NOT EXISTS` never revisits a table that
  already exists, so a column added to the DDL alone is missing on every existing database.
  There is also **no scheduler in this deployment**, so anything time-based runs at startup
  *and* on the read path that depends on it: `lapse_expired_waivers` (before every baseline
  lookup) and `review_source_store.sweep_expired` (on every inventory read).
- `backend/config.py` — env var loading (see below).
- `docs/` — per-agent reference docs and integration guides (`n8n-workflows/`, `vscode-integration/`).

## Environment variables

Required: `ANTHROPIC_API_KEY`. **`GITHUB_TOKEN` is now a fallback, not the only option** —
per-project credentials live in the `github_credentials` table (encrypted, managed at
`/api/v1/github-credentials`, settable from the review screens) and take precedence over it;
see D-236. Optional: `GITHUB_TOKEN` (fine-grained PAT, "Pull requests: Read and Write" — needed for GitHub PR/Auto-Fix features; public repos/PRs/issues work without it. **Also "Contents: Read and Write"** if you use the modernization PR workflow, which pushes a converted tree to a branch rather than only commenting), `DATABASE_PATH` (default `sdlc_agent.db`), `CLAUDE_MODEL` (default `claude-sonnet-4-6`), `MAX_TOKENS`, `APP_ENV`, `SECRET_KEY` (JWT/crypto — must be changed for production). Also present in `backend/config.py` but not in `.env.example`: `JIRA_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN`/`JIRA_PROJECT` (optional Jira integration), `DEFAULT_ADMIN_PASSWORD` (defaults to a hardcoded value — flag this if touching real deployment config), `COST_OPTIMIZATION_ENABLED`, `MAX_AGENT_INPUT_CHARS`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `PUBLIC_BASE_URL` (only for the modernization build loop — the address a GitHub Actions runner posts diagnostics back to; it cannot be derived from the request behind a proxy).

## Windows-specific note

`requirements.txt` deliberately pins `litellm<1.92.0` — from 1.92.0 onward litellm ships a Rust extension with no prebuilt Windows wheel, which would otherwise require an MSVC toolchain to install. Don't bump this pin without checking Windows wheel availability. `headroom-ai` (cost optimization) also compiles a Rust extension; if it fails to install, cost-optimization compression fails open (no-ops) rather than breaking the app.
