# Migration Assessment

Assesses migration complexity, risk, and a phased plan for moving code from a source stack to a target stack — language upgrade, framework swap, database migration, monolith-to-microservices, or cloud migration. Identifies the actual source stack from the code itself (never just trusting a label), names concrete breaking changes rather than generic warnings, and refuses to undersell a genuinely hard migration.

**Module:** Developer Intelligence
**Endpoint:** `POST /api/v1/agents/migration-assessment`
**Agent name (for scopes/BYOL config):** `migration-assessment`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | one of code/github_pr_url/github_file_url | Source code or artifact to assess for migration |
| `github_pr_url` | string | " | GitHub PR URL |
| `github_file_url` | string | " | GitHub file URL |
| `migration_target` | string | **yes** | What you're migrating to, e.g. `"Python 2.7 to Python 3.12"`, `"Flask to FastAPI"`, `"Monolith to Microservices"` |
| `constraints` | string | no | Scale, timeline, team, or compliance constraints on the migration |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `code` · `github_pr_url` · `github_file_url`. Resolution priority: `github_pr_url` > `github_file_url` > `code`/`text`. This project's Architecture Knowledge Base (if `project_name` matches uploaded docs) is treated as authoritative for target stack/pattern preferences.

## Response

Custom envelope — the assessment is nested under `assessment`:

```json
{
  "success": true,
  "agent": "migration-assessment",
  "review_id": "...",
  "project_name": "...",
  "source_type": "...",
  "source_ref": "...",
  "assessment": { "...": "see below" },
  "llm_used": {...},
  "created_at": "..."
}
```

`assessment` contains:

| Field | Description |
|---|---|
| `source_stack` | Detected current language/framework/version, e.g. `"Python 2.7, Flask 0.12"` |
| `target_stack` | The migration target |
| `migration_type` | `Language Version Upgrade` \| `Framework Migration` \| `Database Migration` \| `Monolith to Microservices` \| `Cloud/Platform Migration` \| `Runtime/Infra Migration` \| `Other` |
| `complexity` | `Low` \| `Moderate` \| `High` \| `Very High` |
| `estimated_effort` | Realistic prose estimate, e.g. `"2-3 weeks for a team of 2"` |
| `breaking_changes[]` | Each: `area`, `description`, `impact` (`Low`\|`Moderate`\|`High`\|`Critical`), `mitigation` |
| `risk_areas[]` | Each: `area`, `risk_level`, `description` |
| `phased_plan[]` | 3–6 phases, each: `phase` (int), `title`, `description`, `deliverables[]` |
| `dependencies_affected[]` | Libraries/packages/external systems whose usage must change |
| `recommended_approach` | e.g. big-bang vs. incremental/strangler-fig, with justification |
| `rollback_strategy` | How to revert if the migration fails partway |
| `compliance_considerations[]` | e.g. `"HIPAA: verify the new ORM still enforces column-level encryption"` |
| `summary` | 2–3 sentence executive summary |
| `guardrail_flags` | If the injection heuristic matched |
| `_budget_stats` | **Nested inside `assessment`**, not at the top level |

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 120s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; flags surface inside `assessment.guardrail_flags`
- **Untrusted content wrapping** — a fetched PR/file is wrapped before analysis

## Cost control

- **RBAC** — this key's `scopes` must include `migration-assessment` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` is nested inside `assessment`, not at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/migration-assessment \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "github_file_url": "https://github.com/acme/legacy-billing/blob/main/app/views.py",
    "migration_target": "Flask to FastAPI",
    "constraints": "Must maintain existing API contract for third-party integrators"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "migration-assessment",
  "assessment": {
    "source_stack": "Python 3.9, Flask 2.0",
    "target_stack": "FastAPI",
    "migration_type": "Framework Migration",
    "complexity": "Moderate",
    "estimated_effort": "1-2 weeks for a team of 2, plus 1 week of contract regression testing",
    "breaking_changes": [
      {"area": "Request/response model binding", "description": "Flask's implicit request.json access is replaced by explicit Pydantic request models.", "impact": "Moderate", "mitigation": "Define Pydantic models matching the existing JSON contract exactly before swapping route handlers."}
    ],
    "recommended_approach": "Incremental: stand up FastAPI behind the same reverse-proxy path, migrate one route group at a time behind a feature flag.",
    "rollback_strategy": "Keep the Flask app deployed in parallel until all routes are migrated and contract tests pass against FastAPI.",
    "summary": "A moderate-complexity framework migration; the main risk is preserving the exact existing JSON contract for third-party integrators."
  }
}
```

## Frameworks / controls referenced

`NIST-AI-RMF`. Compliance-relevant migration risks are called out in `assessment.compliance_considerations` rather than emitted as `control_id`-tagged findings.
