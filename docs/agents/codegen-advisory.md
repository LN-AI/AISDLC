# Code Generation Advisor

Recommends language, backend/frontend framework, database, architecture pattern, and 3–8 concrete design patterns for a *new* system given its requirement — 2–3 meaningfully different ranked stack options, not a single verdict. Any field can be pinned to force that exact choice while the agent adapts everything else around it. Consults this project's Architecture Knowledge Base, if any, as authoritative over generic best practice.

**Module:** Developer Intelligence
**Endpoint:** `POST /api/v1/agents/codegen-advisory`
**Agent name (for scopes/BYOL config):** `codegen-advisory`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

This agent uses `LLMOverrideFields` directly, not the standard source model — there is no GitHub/Jira fetch.

| Field | Type | Required | Description |
|---|---|---|---|
| `requirement` | string | **yes** | The system or feature requirement to design for |
| `constraints` | string | no | Scale, compliance, team, or technical constraints, e.g. `"Expect 50k MAU, must be HIPAA compliant, team knows Python"` |
| `context` | string | no | Additional free-text context |
| `project_name` | string | no | Stored in the audit trail; also used to look up this project's Knowledge Base docs |
| `pin_language` | string | no | Force this language regardless of the AI's recommendation |
| `pin_backend_framework` | string | no | Force this backend framework |
| `pin_frontend_framework` | string | no | Force this frontend framework |
| `pin_architecture_pattern` | string | no | Force this pattern, e.g. `Modular Monolith`, `Microservices`, `Serverless`, `Event-Driven` |
| `pin_database` | string | no | Force this database choice |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `requirement` (free text only — no GitHub/Jira resolution for this agent).

## Response

Custom envelope — note the entire agent result is nested under `recommendation`, not spread at the top level:

```json
{
  "success": true,
  "agent": "codegen-advisory",
  "agent_title": "Code Generation Advisor",
  "review_id": "...",
  "project_name": "...",
  "recommendation": { "...": "see below" },
  "llm_used": {"provider": "anthropic", "model": "...", "source": "platform"},
  "created_at": "..."
}
```

`recommendation` contains:

| Field | Description |
|---|---|
| `language`, `backend_framework`, `frontend_framework`, `database`, `architecture_pattern` | The top recommendation — identical to `ranked_suggestions[0]` |
| `deployment_target` | e.g. `AWS ECS Fargate`, `Kubernetes`, `Vercel + Lambda` |
| `ranked_suggestions[]` | 2–3 complete options ranked best-fit first, each with `language`, `backend_framework`, `frontend_framework`, `database`, `architecture_pattern`, `fit_score` (1–10), `why`. If you pinned fields, every suggestion respects the pin and only unpinned fields vary |
| `design_patterns[]` | Each: `pattern` (e.g. Repository, CQRS, Circuit Breaker, Saga), `where_applied`, `why` |
| `rationale` | Why the top stack/pattern combination fits |
| `alternatives[]` | Each: `option`, `when_to_choose` |
| `compliance_considerations[]` | e.g. `"HIPAA: PHI columns must use column-level encryption"` |
| `scalability_notes`, `summary` | Prose |
| `guardrail_flags` | Present only if the injection heuristic matched |
| `_budget_stats` | **Nested inside `recommendation`**, not at the top level of the response — see Cost control |

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 900s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; flags surface inside `recommendation.guardrail_flags`
- No external content is fetched for this agent, so no untrusted-content wrapping applies

## Cost control

- **RBAC** — this key's `scopes` must include `codegen-advisory` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` is computed and deducted from the key's budget as normal, but in the JSON response it appears **nested inside the `recommendation` object**, not at the top level — check `response.recommendation._budget_stats`

## Example

```bash
curl -X POST https://your-host/api/v1/agents/codegen-advisory \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "requirement": "A patient appointment booking system for a mid-size clinic network",
    "constraints": "Expect 50k MAU, must be HIPAA compliant, team knows Python",
    "pin_database": "PostgreSQL"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "codegen-advisory",
  "recommendation": {
    "language": "Python",
    "backend_framework": "FastAPI",
    "frontend_framework": "React + TypeScript",
    "database": "PostgreSQL",
    "architecture_pattern": "Modular Monolith",
    "ranked_suggestions": [
      {"language": "Python", "backend_framework": "FastAPI", "frontend_framework": "React + TypeScript", "database": "PostgreSQL", "architecture_pattern": "Modular Monolith", "fit_score": 9, "why": "Team already knows Python; Modular Monolith avoids premature microservices overhead at 50k MAU."}
    ],
    "design_patterns": [
      {"pattern": "Repository", "where_applied": "Patient/Appointment data access layer", "why": "Isolates HIPAA-sensitive data access behind a testable interface."}
    ],
    "compliance_considerations": ["HIPAA: encrypt PHI columns at rest (pgcrypto or column-level KMS-backed encryption)"],
    "summary": "A HIPAA-ready Modular Monolith on FastAPI + PostgreSQL fits the stated scale without the operational overhead of microservices."
  }
}
```

## Frameworks / controls referenced

`NIST-AI-RMF`. This is an advisory/recommendation agent, not a findings-based reviewer — it flags compliance-driven *build requirements* in `compliance_considerations` rather than emitting `control_id`-tagged findings.
