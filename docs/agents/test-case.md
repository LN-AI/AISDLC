# Test Case Generation

Generates detailed, executable manual test cases — step-by-step actions, expected results, concrete (synthetic, never real-PII) test data — either directly from a requirement, or from a prior Test Scenario catalogue passed via `scenarios_context`. Anything too vague to turn into a real test case is listed in `untestable_items`, never guessed at.

**Module:** QA Intelligence
**Endpoint:** `POST /api/v1/agents/test-case`
**Agent name (for scopes/BYOL config):** `test-case`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | one of text/jira_ticket/github_issue_url | Requirement or user story |
| `scenarios_context` | string | no | A prior test scenario catalogue (the `scenarios` array from [`test-scenario`](test-scenario.md), JSON-stringified) — write cases against those scenarios instead of deriving directly from the requirement |
| `jira_ticket` | string | " | Jira ticket ID or full Atlassian URL |
| `github_issue_url` | string | " | GitHub issue URL |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `text` · `jira_ticket` · `github_issue_url` · `scenarios_context`. Resolution priority for the primary artifact: `jira_ticket` > `github_issue_url` > `text`; `scenarios_context` is a separate, additive field passed alongside whichever source resolves.

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `test_cases[]` | Each: `test_case_id` (e.g. `TC-001`), `title`, `type` (`Functional`\|`Negative`\|`Boundary Value`\|`Role-Based`\|`Integration`\|`Regression`\|`Exception/Error Handling`), `priority`, `preconditions[]`, `steps[]` (each `{step_number, action, expected_result}` — concrete actions, never "verify it works"), `test_data[]` (specific synthetic values), `dependencies[]`, `requirement_ref` (the requirement text or `scenario_id` this validates) |
| `traceability[]` | Each: `requirement_or_scenario_ref`, `test_case_ids[]`. Returned as the model wrote it. The screen **reconciles** it against `test_cases[]` before showing it, and an integrator should too: a row citing an id that no test case carries is claimed coverage resting on a test case nobody generated, and a case in no row at all traces to nothing |
| `untestable_items[]` | Anything too vague to turn into an executable case, named rather than guessed |
| `total_test_cases`, `critical_count` | Added by the orchestrator |
| `summary` | — |
| `post_back` | `not_requested`\|`posted`\|`failed` |
| `llm_used`, `created_at` | Standard |

**Note:** `_budget_stats` is **not present in this response** — the handler builds an explicit field list without it. Budget is still deducted server-side.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 900s timeout, up to 2 retries (raised `max_tokens=32000` output budget)
- **Prompt-injection scan** — non-blocking heuristic (not echoed in this endpoint's explicit response fields)
- **Untrusted content wrapping** — a fetched Jira/GitHub issue is wrapped before analysis
- Test data is required to be clearly synthetic — the agent is instructed never to invent values that would need real PII

## Cost control

- **RBAC** — this key's `scopes` must include `test-case` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- No `_budget_stats` field in the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/test-case \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "text": "As a clinician, I want to export a patient'\''s visit history to CSV so I can share it with another provider. Only the treating clinician may export.",
    "scenarios_context": "[{\"scenario_id\":\"TS-002\",\"title\":\"Non-treating clinician attempts export and is denied\",\"type\":\"Negative\"}]",
    "project_name": "Patient Portal API"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "test-case",
  "test_cases": [
    {
      "test_case_id": "TC-001",
      "title": "Non-treating clinician is denied export access",
      "type": "Negative",
      "priority": "Critical",
      "preconditions": ["Patient P-00042 exists with a visit history", "Clinician C-002 is NOT assigned as the treating clinician for P-00042", "Clinician C-002 is logged in"],
      "steps": [
        {"step_number": 1, "action": "Navigate to Patient P-00042's record as Clinician C-002", "expected_result": "Patient record loads with export button visible but disabled, or hidden entirely per design"},
        {"step_number": 2, "action": "Attempt to call GET /patients/P-00042/export directly as Clinician C-002", "expected_result": "API returns 403 Forbidden; no CSV is returned; an audit log entry records the denied attempt"}
      ],
      "test_data": ["patient_id: P-00042", "clinician_id: C-002 (non-treating)"],
      "requirement_ref": "TS-002"
    }
  ],
  "total_test_cases": 1,
  "critical_count": 1
}
```

## Frameworks / controls referenced

None declared in `AGENT_CATALOG` — a QA execution agent, not a compliance/security reviewer. No `control_id`-tagged findings.
