# Test Scenario Generation

Converts a requirement, user story, or acceptance criteria into a business-level test scenario catalogue — happy path, negative, boundary value, role-based, integration, and exception/error handling — each scenario traced back to the specific requirement text it covers, with an explicit gap list for missing acceptance criteria (never invented).

**Module:** QA Intelligence
**Endpoint:** `POST /api/v1/agents/test-scenario`
**Agent name (for scopes/BYOL config):** `test-scenario`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | one of text/jira_ticket/github_issue_url | Requirement, user story, or acceptance criteria |
| `jira_ticket` | string | " | Jira ticket ID or full Atlassian URL |
| `github_issue_url` | string | " | GitHub issue URL |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `text` · `jira_ticket` · `github_issue_url`. Resolution priority: `jira_ticket` > `github_issue_url` > `text` (this agent doesn't use the PR/file/repo fields).

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `scenarios[]` | Each: `scenario_id` (e.g. `TS-001`), `title`, `type` (`Happy Path`\|`Negative`\|`Boundary Value`\|`Role-Based`\|`Integration`\|`Exception/Error Handling`), `description`, `priority` (`Critical`\|`High`\|`Medium`\|`Low`), `linked_requirement` (the exact requirement text this traces to), `roles_involved[]` |
| `requirement_coverage[]` | Each: `requirement_fragment`, `covered_by_scenarios[]` (scenario IDs), `fully_covered` (bool) — one entry per distinct requirement/AC found in the input |
| `missing_acceptance_criteria[]` | Gaps flagged, never invented criteria |
| `test_data_requirements[]` | Categories of test data needed, e.g. `"a user account with an expired subscription"` |
| `total_scenarios`, `critical_count` | Added by the orchestrator |
| `summary` | — |
| `post_back` | `not_requested`\|`posted`\|`failed` |
| `llm_used`, `created_at` | Standard |

**Note:** `_budget_stats` is **not present in this response** — the handler builds an explicit field list without it. Budget is still deducted server-side.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 120s timeout, up to 2 retries (raised `max_tokens=6000` output budget)
- **Prompt-injection scan** — non-blocking heuristic (not echoed in this endpoint's explicit response fields)
- **Untrusted content wrapping** — a fetched Jira/GitHub issue is wrapped before analysis

## Cost control

- **RBAC** — this key's `scopes` must include `test-scenario` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- No `_budget_stats` field in the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/test-scenario \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "text": "As a clinician, I want to export a patient'\''s visit history to CSV so I can share it with another provider. Only the treating clinician may export; every export is audit-logged.",
    "project_name": "Patient Portal API"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "test-scenario",
  "scenarios": [
    {"scenario_id": "TS-001", "title": "Treating clinician successfully exports visit history", "type": "Happy Path", "priority": "High", "linked_requirement": "As a clinician, I want to export a patient's visit history to CSV"},
    {"scenario_id": "TS-002", "title": "Non-treating clinician attempts export and is denied", "type": "Negative", "priority": "Critical", "linked_requirement": "Only the treating clinician may export"},
    {"scenario_id": "TS-003", "title": "Export action is captured in the audit log", "type": "Integration", "priority": "Critical", "linked_requirement": "every export is audit-logged"}
  ],
  "missing_acceptance_criteria": ["No stated behavior for exporting a patient with zero visit history"],
  "total_scenarios": 3,
  "critical_count": 2
}
```

## Frameworks / controls referenced

None declared in `AGENT_CATALOG` — this is a QA coverage agent, not a compliance/security reviewer. It has no `control_id`-tagged findings; its output feeds [`test-case`](test-case.md) via `scenarios_context`.
