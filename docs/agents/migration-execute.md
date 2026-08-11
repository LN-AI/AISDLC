# Migration Execution

Transforms a specific file/artifact from its current stack to a target stack, preserving behavior exactly unless the migration itself requires a change. Returns complete, runnable code — never a diff or partial snippet — plus a change-by-change summary and an honest list of anything needing manual follow-up. Optionally auto-runs Code Review and Sensitive Data agents against the migrated output.

**Module:** Developer Intelligence
**Endpoint:** `POST /api/v1/agents/migration-execute`
**Agent name (for scopes/BYOL config):** `migration-execute`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | one of code/github_pr_url/github_file_url | Source code to migrate |
| `github_pr_url` | string | " | GitHub PR URL |
| `github_file_url` | string | " | GitHub file URL |
| `migration_target` | string | **yes** | e.g. `"Flask to FastAPI"` |
| `file_path` | string | no | Original file path, reused for the migrated output (defaults to the resolved source reference if omitted) |
| `requirement` | string | no | Why this migration is happening — improves relevance |
| `auto_review` | boolean | no (default `false`) | Run Code Review + Sensitive Data agents against the migrated code before returning it |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `code` · `github_pr_url` · `github_file_url`. Resolution priority: `github_pr_url` > `github_file_url` > `code`/`text`.

## Response

Custom envelope, fields returned at the top level (not nested):

| Field | Description |
|---|---|
| `review_id` | — |
| `files[]` | Each: `path` (reuses the original path or a sensible target-stack path), `content` (full migrated file — complete and runnable, never a diff), `purpose` |
| `file_count` | `len(files)` |
| `changes_summary[]` | Each: `change`, `reason` — every meaningful change, in order, for line-by-line audit against the original |
| `manual_followup[]` | Things that could NOT be safely auto-migrated — config/secrets/infra wiring, business-logic-sensitive branches, anything the model wasn't confident about. Honest by design: a genuinely trivial migration is the only case where this should be empty |
| `test_recommendations[]` | What to specifically test after this migration |
| `risk_notes` | Anything about this specific transformation the reviewer should be cautious about |
| `governance_review` | Present only when `auto_review: true` |
| `summary` | — |
| `llm_used`, `created_at` | Standard |

**Note:** `_budget_stats` is **not present anywhere in this response** — the handler returns an explicit field list that omits it, even though the call is scope/budget-checked and billed normally server-side. Check `GET /api/v1/keys/{key_id}/budget` for the running total instead.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 900s timeout, up to 2 retries (this call uses a raised `max_tokens=16000` output budget to fit a complete migrated file)
- **Prompt-injection scan** — non-blocking; would surface as `guardrail_flags` if triggered, though this field is not echoed in the explicit response shape above — check the underlying orchestrator result if you need it
- **Untrusted content wrapping** — a fetched PR/file is wrapped before analysis
- If `auto_review: true`, the migrated output is passed through the same Code Review and Sensitive Data Detector pipeline described in [`code.md`](code.md) and [`sensitive-data.md`](sensitive-data.md)

## Cost control

- **RBAC** — this key's `scopes` must include `migration-execute` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month (`auto_review: true` adds the cost of two more LLM calls to this request)
- No `_budget_stats` field in the response (see Response, above)

## Example

```bash
curl -X POST https://your-host/api/v1/agents/migration-execute \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "github_file_url": "https://github.com/acme/legacy-billing/blob/main/app/views.py",
    "migration_target": "Flask to FastAPI",
    "file_path": "app/views.py",
    "auto_review": true
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "migration-execute",
  "files": [
    {"path": "app/routes/billing.py", "content": "from fastapi import APIRouter\n...", "purpose": "Migrated billing routes"}
  ],
  "file_count": 1,
  "changes_summary": [
    {"change": "Replaced @app.route with APIRouter + @router.get/post", "reason": "FastAPI's routing model"},
    {"change": "Replaced request.json with a Pydantic request model", "reason": "Preserves the existing JSON contract with explicit validation"}
  ],
  "manual_followup": ["Verify the new Pydantic model's field validators match the old manual validation exactly for the 'amount' field edge cases."],
  "test_recommendations": ["Run the existing contract test suite against the new routes before cutover."],
  "governance_review": {"code": {"gate_status": "Green"}, "sensitive-data": {"gate_status": "Green"}}
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` · `OWASP-LLM`. No `control_id`-tagged findings of its own — compliance-relevant risk is folded into `risk_notes`/`manual_followup`, or surfaced explicitly via the `governance_review` sub-reports when `auto_review: true`.
