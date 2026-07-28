# Code Generation Scaffold

Generates a minimal, runnable starter project (8–16 files: entry point, one model, one route/controller, config, dependency manifest, Dockerfile, README, plus a wired frontend page if a frontend framework is given) for a chosen language/framework/database/architecture pattern. Optionally auto-runs Code Review and Sensitive Data Detector against the generated code before returning it, so the scaffold ships pre-certified.

**Module:** Developer Intelligence
**Endpoint:** `POST /api/v1/agents/codegen-scaffold`
**Agent name (for scopes/BYOL config):** `codegen-scaffold`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `project_name` | string | **yes** | Also used as the generated folder/zip name |
| `language` | string | **yes** | e.g. `Python`, `TypeScript`, `Java`, `Go` |
| `backend_framework` | string | **yes** | e.g. `FastAPI`, `Express`, `Spring Boot`, `Django` |
| `frontend_framework` | string | no | e.g. `React + TypeScript`, `Vue 3`, `Angular`, or `None` |
| `database` | string | no | e.g. `PostgreSQL`, `MongoDB`, `DynamoDB` |
| `architecture_pattern` | string | no (default `Modular Monolith`) | e.g. `Modular Monolith`, `Microservices`, `Serverless` |
| `requirement` | string | no | The requirement this scaffold serves — improves generated code relevance |
| `context` | string | no | Additional free-text context |
| `include_auth` | boolean | no (default `true`) | Include a JWT auth stub (login endpoint + protected route example) |
| `include_docker` | boolean | no (default `true`) | Include a Dockerfile for the backend |
| `include_tests` | boolean | no (default `true`) | Include one example test file |
| `auto_review` | boolean | no (default `false`) | Run Code Review + Sensitive Data agents against the generated scaffold before returning it |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** none (`project_name`/`language`/`backend_framework` drive generation directly — this is a generator, not a reviewer of an existing artifact).

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `review_id` | Also used to fetch the zip download |
| `files[]` | Each: `path`, `content` (full file content — not a placeholder), `purpose` |
| `file_count` | `len(files)` |
| `setup_instructions` | Step-by-step shell commands to install and run locally |
| `next_steps[]` | 3–6 concrete next steps for the team building on this scaffold |
| `included_controls[]` | Governance controls baked in by construction, e.g. `"SEC-001: JWT auth stub included"`, `"SEC-006: secrets read from env, never hardcoded"` |
| `governance_review` | Present only when `auto_review: true` — the Code Review + Sensitive Data results run against the generated files |
| `download_url` | `GET /api/v1/agents/codegen-scaffold/{review_id}/download` — fetches all generated files as a zip |
| `llm_used`, `created_at` | Standard |

**Note:** `_budget_stats` is **not present anywhere in this response**, even though the call is scope- and budget-checked normally server-side — the handler builds an explicit field list that doesn't include it. Cost is still deducted from the key's monthly budget; you just can't read the per-call breakdown from this endpoint's response body (check the API Keys dashboard or `GET /api/v1/keys/{key_id}/budget` instead).

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars) applies to the `requirement`/`context` text passed in
- **Timeout & retry** — 120s timeout, up to 2 retries (this call also uses a raised `max_tokens=8000` output budget to fit a full multi-file scaffold in one response)
- **Prompt-injection scan** — non-blocking; would surface as `guardrail_flags` if present, though a synthetic `requirement` rarely triggers it
- If `auto_review: true`, the generated scaffold is passed through the same Code Review and Sensitive Data Detector guardrail pipeline described in [`code.md`](code.md) and [`sensitive-data.md`](sensitive-data.md) before being returned

## Cost control

- **RBAC** — this key's `scopes` must include `codegen-scaffold` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month (checked before generation starts; `auto_review: true` adds the cost of two more LLM calls to this same request)
- No `_budget_stats` field in the response (see Response, above)

## Example

```bash
curl -X POST https://your-host/api/v1/agents/codegen-scaffold \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "project_name": "clinic-booking-api",
    "language": "Python",
    "backend_framework": "FastAPI",
    "database": "PostgreSQL",
    "requirement": "Patient appointment booking with clinician availability lookup",
    "auto_review": true
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "codegen-scaffold",
  "review_id": "c48f...",
  "files": [
    {"path": "app/main.py", "content": "from fastapi import FastAPI\n...", "purpose": "FastAPI entry point"},
    {"path": "requirements.txt", "content": "fastapi\nuvicorn\nsqlalchemy\npsycopg2-binary\n...", "purpose": "Dependency manifest"},
    {"path": "Dockerfile", "content": "FROM python:3.12-slim\n...", "purpose": "Container build"}
  ],
  "file_count": 11,
  "included_controls": ["SEC-001: JWT auth stub included", "SEC-006: DB credentials read from env, never hardcoded"],
  "governance_review": {"code": {"gate_status": "Green", "risk_score": 8}, "sensitive-data": {"gate_status": "Green", "pii_count": 0}},
  "download_url": "/api/v1/agents/codegen-scaffold/c48f.../download"
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` · `OWASP-LLM`. `included_controls` cites the shared `SEC-xxx` control library for whatever governance defaults were built into the scaffold by construction (env-based config, auth stub, no hardcoded secrets).
