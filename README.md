# LM AI-SDLC ReviewOps Agent

AI-powered governance and security review control tower for the software delivery lifecycle.

## What it does

Every requirement, code change, prompt, model workflow, and release is reviewed for security, privacy, governance, and AI risk before it reaches production. Agents are organized into the same nine Intelligence modules as the sidebar (`backend/agents/<module>/`), tracking the SDLC phases requirement → architecture → development → QA → security → operations → governance:

| Module | Agent | Reviews |
|---|---|---|
| Requirement Intelligence | Requirement Reviewer | Completeness, PHI/PII risks, regulatory impact, missing acceptance criteria |
| Architecture Intelligence | Architecture Review | Quality dimension scoring, anti-pattern detection, SPOFs, alternative patterns |
| Architecture Intelligence | Security Review (Threat Model) | STRIDE threat modelling, trust boundaries, attack surface analysis |
| Developer Intelligence | Code Review | SQL injection, secrets, PHI in logs, dependency CVEs, prompt injection vectors |
| Developer Intelligence | Code Generation | Recommends language/stack/architecture pattern/design patterns for a new system, then generates a runnable starter scaffold — optionally pre-certified via an auto Code Review + Sensitive Data pass |
| Developer Intelligence | Code Migration | Assesses migration complexity/risk/phased plan for a stack change (language upgrade, framework swap, database migration, monolith-to-microservices), then transforms a specific file to the target stack — preserving behavior, with an honest manual-follow-up list |
| Developer Intelligence | AI Code Provenance & Risk | Probabilistic AI-origin likelihood (Low/Possible/Likely/Strong evidence) with an evidence-backed score breakdown — never a verdict — combining an optional developer disclosure with LLM-based style-signal analysis |
| Developer Intelligence | Auto-Fix & Open PR | Reviews a GitHub PR, generates fixes for its findings, commits them to a new branch off the PR's head branch, and opens a follow-up PR back into it. Refuses to commit any patch that appears to introduce a hardcoded secret. Requires a write-scoped `GITHUB_TOKEN`. |
| QA Intelligence | Test Scenario Generation | Converts a requirement/user story into a business-level test scenario catalogue — happy path, negative, boundary value, role-based, integration, exception handling — each traced back to the requirement text it covers, plus a requirement-coverage gap map |
| QA Intelligence | Test Case Generation | Generates detailed, executable manual test cases — step-by-step actions, expected results, concrete test data — from a requirement directly, or chained from a Test Scenario catalogue |
| Security Intelligence | VAPT | CVSS-scored vulnerability assessment, OWASP Web/API Top 10, PoC attack scenarios |
| Security Intelligence | Sensitive Data Detector | PII/PHI/PCI/credential scanning with regulation references |
| Security Intelligence | Prompt & Agent Safety | Prompt injection, excessive agency, over-permissioned tools, hallucination risk |
| Security Intelligence | 21 CFR Part 11 Verification | Verifies a codebase (upload a .zip for a real multi-file scan) against FDA 21 CFR Part 11 — audit trail, electronic signatures, access control, data integrity (ALCOA+) — with evidence-cited findings and a separate validation-readiness note for process-only controls |
| Security Intelligence | GxP Document Check | Checks SOPs, validation protocols (IQ/OQ/PQ), batch records, deviations, CAPAs, and change controls against a type-specific required-elements checklist, signature/approval compliance, version control, and cross-document traceability — upload multiple related documents (e.g. a URS + its OQ protocol) to check consistency between them |
| Security Intelligence | GxP Document Generation | Point at an application (zip upload) and generate its full GxP validation package — GAMP 5 risk classification, then a complete URS, FRS, Risk Assessment, Validation Plan, IQ/OQ/PQ Protocols, Traceability Matrix, Validation Summary Report, and SOP, each grounded in the actual application with honest "needs human input" open items rather than fabricated content |
| Operations Intelligence | Issue Triaging Agent | Classifies, prioritizes (severity + business priority), and recommends owner/team for a defect/incident/support ticket/security finding/test failure. Detects duplicates against this project's own triage history, enriches incomplete tickets without inventing facts, splits root-cause hypotheses into confirmed/probable/possible, and flags escalation — with a deterministic human-in-the-loop gate (mandatory review for Critical/P0/P1/security or <70% confidence). Can batch-triage a GitHub repo's open issues. |
| Governance Intelligence | Compliance Mapper | NIST AI RMF, OWASP LLM Top 10, HIPAA, GDPR, GxP/21 CFR Part 11, SOC 2 (reachable via Full SDLC Review or the v1 API) |
| Governance Intelligence | Release Gate | Green / Amber / Red decision based on all findings |
| Governance Intelligence | Governance Report | Audit-ready report with control mapping and evidence checklist — synthesizes prior findings (by `review_ids` or supplied directly) via its own standalone endpoint, without re-running a full review |

`backend/agents/base_agent.py` (shared base classes) and the single central `SDLCOrchestrator` (`backend/services/orchestrator.py`) sit outside the module folders, since they're used across all of them. QA Intelligence and Security Intelligence's Prompt/Compliance/GxP/CFR21 agents are the newest modules — DevOps Intelligence and the rest of Operations Intelligence (CI/CD, deployment readiness, monitoring, incident management) are the next planned additions, not yet built.

A Governance Swarm layer (`POST /api/v1/swarm`) runs several of these agents in parallel behind one policy-enforced gate decision (built-in policies: `standard`, `hipaa`, `pci`, `ai-product`).

Every agent above (25 endpoints total) has its own reference doc under [`docs/agents/`](docs/agents/README.md) — request/response fields, guardrails, and a worked example.

## API access, guardrails & cost control

Every one of the 25 `/api/v1/agents/*` endpoints is externally callable with an API key (`X-API-Key`, see the **API Keys** page) or a UI Bearer JWT, and every call goes through the same shared guardrail and cost-control layer:

- **Guardrails** (`backend/services/guardrail_service.py`, applied inside `llm_gateway.call_tool()`) — an input-size limit (413 if exceeded), a request timeout with automatic retry on transient provider errors, a non-blocking prompt-injection heuristic scan (surfaced as `guardrail_flags`), and untrusted-content delimiting for anything fetched from GitHub/Jira. The Auto-Fix agent additionally refuses to commit any patch that looks like it introduces a hardcoded secret.
- **RBAC scopes** — an API key's `scopes` field (`all`, or a comma-separated agent-name list) gates which agents it may call; an out-of-scope call returns 403.
- **Cost control** (`backend/services/agent_guard.py`) — API keys can carry a hard `monthly_budget_usd` cap (`PATCH /api/v1/keys/{key_id}/budget`); once a key's tracked spend for the calendar month reaches its cap, further calls are rejected with 402 until the cap is raised or the month resets. Every response includes `_budget_stats` (tokens + estimated cost) when usage was tracked.

## Vertical Dashboards

A curated view over the agents above, tailored per industry vertical. Each project is tagged with a vertical (`Hospital`, `Pharma`, `BFSI`, `IT Services`, or `Generic`) at creation time; the Vertical Dashboard page pulls that project's most recent relevant reviews into stat tiles (e.g. compliance gate, PHI/PII findings, 21 CFR Part 11 gate, GxP document completeness) and offers one-click quick launches straight into the right agent page with the project pre-filled. It's a pure config/aggregation layer — no new orchestration or scanning logic. `Hospital` is fully configured today; `Pharma`, `BFSI`, and `IT Services` are reserved for future rollout. See `backend/services/vertical_registry.py` and `GET /api/v1/verticals`, `GET /api/v1/verticals/{project_id}/stats`.

## Knowledge Base (per-agent)

Every agent has its own Knowledge Base category — upload `.md` standards, checklists, and guidelines so that agent's reviews are grounded in your organization's actual practices instead of generic advice. A document is either shared/global (used for every project) or scoped to one project (takes priority over global docs for that project). Categories: `requirement`, `code-review`, `prompt-safety`, `compliance`, `vapt`, `security`, `architecture` (shared by Architecture Review, Code Migration, and Code Generation), `sensitive-data`, `ai-provenance`, `gxp` (shared by 21 CFR Part 11 Verification, GxP Document Check, and GxP Document Generation), `issue-triage`, and `qa` (shared by Test Scenario Generation and Test Case Generation). Manage docs from the **Knowledge Base** page, or via `GET/POST/PUT/DELETE /api/knowledge-base` (`GET /api/knowledge-base/categories` lists all categories). See `backend/services/knowledge_base.py`.

## Cost Optimization

Integrates [Headroom](https://github.com/headroomlabs-ai/headroom) to compress a raw artifact (logs, stack traces, codebase files, JSON) *before* it's wrapped into an agent's instruction prompt — Headroom's content router only applies real compression to a message that's purely JSON/code/log content, so compressing the artifact in isolation (at the orchestrator level, via `SDLCOrchestrator._compress()`) is what actually works; compressing the fully-assembled prompt string does not, since mixed instruction+data reads as prose and no-ops. Verified against the real package:

| Content type | Real-world savings |
|---|---|
| JSON-shaped content | ~35-55% (SmartCrusher) |
| Log-shaped content | ~15% (dedicated log compressor) |
| Code | none — Headroom protects the "most recent" turn's code from compression by design, and every call here is a single fresh turn |
| Plain prose (requirements, compliance text) | none, without the optional `headroom-ai[ml]` extra (Kompress-v2-base) |

**21 CFR Part 11 Verification, GxP Document Check, and GxP Document Generation/Plan are always exempt** (their orchestrator methods never call `_compress()`), and so is Migration Execution (it generates replacement code 1:1 from the source — exact fidelity matters more than token cost there). Toggle platform-wide (super admin only) and view token/$ savings by agent and by day from the **Cost Optimization** page, or via `GET/PUT /api/v1/cost-optimization/settings` and `GET /api/v1/cost-optimization/dashboard`. Compression fails open — if the `headroom-ai` package isn't installed or a compression call errors, the original uncompressed content is sent and the review proceeds normally. See `backend/services/cost_optimization_service.py` and `SDLCOrchestrator._compress()` in `backend/services/orchestrator.py`.

**Installing `headroom-ai` on Windows:** it compiles a Rust extension on install (needs `cargo`/`rustup`), and as of `litellm` 1.92.0 that dependency only ships manylinux wheels (no `win_amd64` wheel), so a plain `pip install headroom-ai` will try to build `litellm` from source and typically fail without a full MSVC linker. `requirements.txt` pins `litellm<1.92.0`, which has a pure-Python wheel and installs cleanly with just `rustup` (https://rustup.rs) — no Visual Studio Build Tools needed. The platform degrades gracefully either way (compression simply no-ops) if the package can't be installed.

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

# 3. Run
python run.py
```

Open [http://localhost:8000](http://localhost:8000)

## API routes

| Method | Route | Description |
|---|---|---|
| POST | `/api/review/requirement` | Review a requirement text |
| POST | `/api/review/requirement/upload` | Upload PDF/DOCX/TXT requirement |
| POST | `/api/review/code` | Review code or PR diff |
| POST | `/api/review/prompt` | Review system prompt + tools + RAG |
| POST | `/api/review/full` | Run all agents in one pass |
| POST | `/api/github/pr` | Review a GitHub PR (posts comment) |
| POST | `/api/release-gate` | Evaluate release gate decision |
| GET  | `/api/dashboard` | Risk overview stats |
| GET  | `/api/reviews` | List all reviews |
| GET  | `/api/reviews/{id}` | Get review details |
| GET  | `/api/reports/{id}` | Get governance report |
| GET  | `/api/controls` | List all control library entries |

Interactive API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

## External team access (API v1)

External teams and CI pipelines authenticate with an `X-API-Key` header (issued from the
dashboard's **API Keys** page) instead of logging in. Every agent is available individually
under `/api/v1/agents/*` (e.g. `POST /api/v1/agents/code`), plus `POST /api/v1/swarm` to run
several agents in parallel behind one policy-enforced gate decision.

- **Rate limits** — each key has a per-minute and per-day quota (editable by an admin);
  responses carry `X-RateLimit-*` headers and a `429` + `Retry-After` when exceeded.
- **Bring your own LLM** — a team can route its calls through its own Anthropic or
  OpenAI-compatible credentials (`PUT /api/v1/llm-config`, X-API-Key auth) instead of the
  platform's shared account, or override a single call via `llm_provider` / `llm_api_key` /
  `llm_base_url` / `llm_model` fields on any agent request.
- **Notifications** — a blocked review can reach a Slack, Teams or your own webhook
  instead of waiting for somebody to open a tab. One https URL per channel, per project,
  encrypted and never shown again. Delivery is a **durable row written inside the review's
  own transaction**, then attempted on a background thread and retried at startup — there
  is no job runner here, so a notification written seconds before a restart is *late*
  rather than lost, and the delivery log says which. The URL is checked against private
  and link-local address space before anything is stored or sent, because a webhook this
  server requests on a user's behalf is server-side request forgery if it is not.
- **Suppression as code** — a team's accepted risks live in their repository, not in this
  database. A `.lmignore` at the root takes path globs optionally narrowed to a control
  (`legacy/**: SEC-003 deleted in Q3`), and an inline `# lm-ignore: SEC-001 admin only`
  covers one line. An ignore rule **lowers the gate and never hides the evidence** — a
  suppressed finding is still reported and still exported, marked with the rule that did
  it, because otherwise anyone with commit access could turn a Red release Green by adding
  two lines. Rules that matched nothing are reported, because the way this fails is a typo.
  Existing waivers export as a file with `lmsdlc findings --lmignore`. See
  [`docs/suppression.md`](docs/suppression.md).
- **CLI and CI** — `scripts/lmsdlc.py` runs a review from a terminal or any pipeline.
  `--fail-on red` exits non-zero on a Red gate, so the review *is* the build step rather
  than a report beside it; `--sarif out.sarif` writes the format GitHub code scanning and
  GitLab ingest, so findings land in the Security tab **without needing a GitHub App**; and
  `--quote-only` prices the run without calling a model, so a pipeline can refuse to spend.
  Ready workflows for GitHub Actions and GitLab CI are in [`docs/ci/`](docs/ci/README.md),
  including a per-pull-request cost ceiling. What it deliberately does *not* do — inline
  diff annotations, incremental review — is listed there too.
- **No-code integration** — importable [n8n](https://n8n.io) workflow templates (PR gate, nightly
  compliance sweep, Jira requirement gate) are in [`docs/n8n-workflows/`](docs/n8n-workflows/) for
  teams who'd rather wire this up visually than write CI scripts.
- **VS Code integration** — run Code Review on the open file, your git diff, or a PR without leaving
  the editor via a `.vscode/tasks.json` + script template in
  [`docs/vscode-integration/`](docs/vscode-integration/) (no extension install required).

## Controls library

**Security controls:** SEC-001 through SEC-007  
**AI safety controls:** AI-001 through AI-010  
**Governance controls:** GOV-001 through GOV-008  

Mapped to: NIST AI RMF · OWASP LLM Top 10 · HIPAA · GDPR · GxP · SOC 2

## Risk scoring

```
Risk Score = Impact × Likelihood × Exposure × Autonomy
```

| Score | Level | Gate impact |
|---|---|---|
| 5 | Critical | Always Red |
| 4 | High | Red unless formally accepted |
| 3 | Moderate | Amber — reviewer decision |
| 2 | Minor | Non-blocking |
| 1 | Low | Informational |

## Gate logic

- **Green** — Proceed to production
- **Amber** — Proceed only with named reviewer approval
- **Red** — Blocked until critical issues resolved

## Tech stack

- **Backend:** FastAPI + Python
- **AI:** Anthropic Claude (claude-sonnet-4-6) via tool_use for structured output
- **Database:** SQLite (upgrade to PostgreSQL for production)
- **Frontend:** Vanilla HTML/CSS/JS
- **GitHub integration:** GitHub REST API v3

## Project structure

```
prodreadyagent/
├── backend/
│   ├── agents/           # 6 review agents
│   ├── controls/         # SEC, AI, GOV control library
│   ├── models/           # Pydantic schemas
│   ├── services/         # Orchestrator + GitHub service
│   ├── database.py       # SQLite layer
│   ├── config.py         # Environment config
│   └── main.py           # FastAPI app
├── frontend/             # Dashboard (HTML/CSS/JS)
├── run.py                # Entry point
├── requirements.txt
└── .env.example
```
