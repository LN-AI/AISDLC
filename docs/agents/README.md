# Agent API Reference

Every agent on the LM AI-SDLC ReviewOps platform is exposed as an individually callable, typed endpoint under `/api/v1/agents/*`, authenticated with either `X-API-Key: lmsdlc_...` (external teams/CI) or `Authorization: Bearer <jwt>` (UI). Each file below documents one endpoint end-to-end: request/response schema, guardrails, RBAC/budget behavior, and a worked example — enough to integrate against that agent using only its own file.

All 25 endpoints share the same cross-cutting behavior, described identically in every file:

- **RBAC** — an API key's `scopes` field (comma-separated agent names, or `all`) gates which agents it may call; an out-of-scope call returns HTTP 403.
- **Monthly budget cap** — if a key has a `monthly_budget_usd` set (`PATCH /api/v1/keys/{key_id}/budget`), calls are rejected with HTTP 402 once that month's usage reaches the cap.
- **Input size limit** — artifacts over `MAX_AGENT_INPUT_CHARS` (400,000 characters by default) are rejected with HTTP 413.
- **Timeout & retry** — 120s timeout per LLM call, up to 2 retries on transient provider errors.
- **Prompt-injection scan** — a non-blocking heuristic scan whose hits (if any) are attached to the response as `guardrail_flags` — informational only, never blocks.
- **Untrusted content wrapping** — content fetched from GitHub/Jira is delimited so the model treats it as data, never as instructions.
- **Bring your own LLM** — every request accepts `llm_provider` / `llm_api_key` / `llm_base_url` / `llm_model` to route a single call through your own Anthropic or OpenAI-compatible credentials.

One nuance worth knowing up front: **not every endpoint returns `_budget_stats` at the top level of its response.** Some nest it inside a sub-object (e.g. `recommendation`, `report`, `assessment`, `triage_report`), and a few omit it from the response body entirely even though the budget is still deducted server-side. Each agent's own doc states exactly where (or whether) it appears.

## Requirement Intelligence

| Agent | Endpoint | Description |
|---|---|---|
| [Requirement Risk Reviewer](requirement.md) | `POST /api/v1/agents/requirement` | Reviews user stories, BRDs, and acceptance criteria for risk, ambiguity, and compliance gaps. |

## Developer Intelligence

| Agent | Endpoint | Description |
|---|---|---|
| [Code & PR Security Reviewer](code.md) | `POST /api/v1/agents/code` | Security and quality review of code, git diffs, and pull requests (OWASP, CWE, injection, secrets). |
| [Code Generation Advisor](codegen-advisory.md) | `POST /api/v1/agents/codegen-advisory` | Recommends language, framework, database, architecture pattern, and design patterns for a new system. |
| [Code Generation Scaffold](codegen-scaffold.md) | `POST /api/v1/agents/codegen-scaffold` | Generates a minimal, runnable starter project for a chosen stack, optionally pre-certified via auto-review. |
| [AI Code Provenance & Risk](ai-provenance.md) | `POST /api/v1/agents/ai-provenance` | Assesses the AI-origin likelihood of a code artifact as probabilistic evidence, never a verdict. |
| [Migration Assessment](migration-assessment.md) | `POST /api/v1/agents/migration-assessment` | Assesses migration complexity, breaking changes, risk, and a phased plan between two stacks. |
| [Migration Execution](migration-execute.md) | `POST /api/v1/agents/migration-execute` | Transforms a specific file/artifact from its current stack to a target stack, preserving behavior. |
| [Legacy Modernization](modernization.md) | `POST /api/v1/modernization/runs` | Converts a **whole codebase** as a durable, resumable run — dependency-ordered, charter-bound, secrets redacted before any model call. Metered per converted file. |
| [Auto-Fix & Open PR](auto-fix.md) | `POST /api/v1/agents/auto-fix` | Reviews a PR, commits fixes to a new branch, and opens a follow-up PR — writes real commits to GitHub. |

## Security Intelligence

| Agent | Endpoint | Description |
|---|---|---|
| [Prompt & Agent Safety Reviewer](prompt.md) | `POST /api/v1/agents/prompt` | Reviews AI system prompts, tool definitions, and RAG configs for injection and safety risks. |
| [Compliance Mapper](compliance.md) | `POST /api/v1/agents/compliance` | Maps any artifact to NIST AI-RMF, OWASP LLM, HIPAA, GDPR, ISO 27001, GxP, PCI DSS controls. |
| [VAPT — Vulnerability Assessment](vapt.md) | `POST /api/v1/agents/vapt` | Full vulnerability assessment with CVSS v3.1 scoring, OWASP API Top 10, and PoC attack scenarios. |
| [Security Architecture Review](security.md) | `POST /api/v1/agents/security` | STRIDE threat modelling, trust boundary analysis, and attack surface assessment. |
| [Sensitive Data Detector](sensitive-data.md) | `POST /api/v1/agents/sensitive-data` | Scans code, configs, schemas, and logs for PII, PHI, PCI data, credentials, and location data. |
| [21 CFR Part 11 Verification](cfr21-verification.md) | `POST /api/v1/agents/cfr21-verification` | Verifies a codebase against FDA 21 CFR Part 11 — audit trail, e-signatures, access control, data integrity. |
| [GxP Document Check](gxp-document-check.md) | `POST /api/v1/agents/gxp-document-check` | Checks GxP documents for completeness, signature compliance, version control, and traceability. |
| [GxP Validation Plan](gxp-generate-plan.md) | `POST /api/v1/agents/gxp-generate-plan` | Plans an application's GxP validation document package — GAMP 5 classification and per-document outlines. |
| [GxP Document Generation](gxp-generate-document.md) | `POST /api/v1/agents/gxp-generate-document` | Generates one complete, application-grounded GxP validation document. |

## Architecture Intelligence

| Agent | Endpoint | Description |
|---|---|---|
| [Architecture Review](architecture.md) | `POST /api/v1/agents/architecture` | Quality dimension scoring (8 axes), anti-pattern detection, SPOF analysis, and architectural alternatives. |

## Governance Intelligence

| Agent | Endpoint | Description |
|---|---|---|
| [Release Gate](release-gate.md) | `POST /api/v1/agents/release-gate` | Evaluates release readiness: aggregates prior findings, checks mandatory controls, issues a gate decision. |
| [Full SDLC Review](full.md) | `POST /api/v1/agents/full` | Runs requirement, code, compliance, architecture, and release gate agents in one call. |
| [Governance Report Generator](report-generate.md) | `POST /api/v1/agents/report-generate` | Synthesizes prior review findings into an audit-ready governance report without re-running a review. |

## Operations Intelligence

| Agent | Endpoint | Description |
|---|---|---|
| [Issue Triaging Agent](issue-triage.md) | `POST /api/v1/agents/issue-triage` | Classifies, prioritizes, and recommends assignment for a defect/incident/support ticket/security finding. |
| [Issue Triaging — GitHub Batch](issue-triage-batch-github.md) | `POST /api/v1/agents/issue-triage/batch-github` | Pulls open issues from a GitHub repo and triages each one, with a severity/escalation summary. |

## QA Intelligence

| Agent | Endpoint | Description |
|---|---|---|
| [Test Scenario Generation](test-scenario.md) | `POST /api/v1/agents/test-scenario` | Converts a requirement into a business-level test scenario catalogue traced back to the requirement text. |
| [Test Case Generation](test-case.md) | `POST /api/v1/agents/test-case` | Generates detailed, executable manual test cases from a requirement or a prior test scenario catalogue. |
