# Requirement Risk Reviewer

Reviews user stories, BRDs, FRDs, and acceptance criteria for completeness gaps, data/privacy risk, missing security requirements, AI/ML-specific risk, and applicable regulatory frameworks. Flags every gap and cites the exact requirement text it applies to rather than manufacturing findings against a well-written requirement.

**Module:** Requirement Intelligence
**Endpoint:** `POST /api/v1/agents/requirement`
**Agent name (for scopes/BYOL config):** `requirement`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access, issued from the dashboard's **API Keys** page
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

External-key calls are also subject to per-minute/per-day rate limits (`X-RateLimit-*` response headers; `429` + `Retry-After` on breach).

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | one of text/jira_ticket/github_issue_url | Raw requirement text, user story, or BRD to review |
| `jira_ticket` | string | " | Jira ticket ID (e.g. `PROJ-123`) or full Atlassian URL |
| `github_issue_url` | string | " | GitHub issue URL |
| `project_name` | string | no | Project/product name stored in the audit trail |
| `context` | string | no | Free-text context injected into the review prompt |
| `post_back` | boolean | no (default `false`) | Post the result as a comment on the originating GitHub issue or Jira ticket |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Bring-your-own-LLM override for this single call — see below |

**Sources accepted:** `text` · `jira_ticket` · `github_issue_url`. Resolution priority when more than one is supplied: `github_pr_url` > `github_file_url` > `github_repo_url` > `jira_ticket` > `github_issue_url` > raw `text` — this agent doesn't use the PR/file/repo fields, so in practice it's `jira_ticket` > `github_issue_url` > `text`.

### Bring your own LLM (optional)
Every request field set (`llm_provider`/`llm_api_key`/`llm_base_url`/`llm_model`) routes this single call through your own Anthropic or OpenAI-compatible credentials. Resolution order: per-request fields > this API key's per-agent override for `requirement` > this key's global BYOL default > platform default.

## Response

Standard envelope (`_wrap`):

| Field | Description |
|---|---|
| `review_id` | UUID for this review, usable in `release-gate`/`report-generate` `review_ids` |
| `gate_status` | `Green` \| `Amber` \| `Red` |
| `risk_score` | 0–100, `Impact × Likelihood × Exposure × Autonomy` |
| `total_findings`, `critical_count`, `high_count` | Finding counts |
| `findings[]` | Each: `control_id` (e.g. `SEC-001`, `AI-003`, `GOV-005`), `severity` (1–5), `risk_level`, `title`, `description`, `recommendation`, `is_blocking`, optional `owasp_ref`, `nist_ref` |
| `summary` | 2–3 sentence executive summary |
| `recommendations[]` | Top 3–5 prioritized actions |
| `compliance_flags[]` | e.g. `HIPAA`, `GDPR`, `OWASP-LLM01` |
| `post_back_status` | `not_requested` \| `posted` \| `failed` |
| `source_type`, `source_ref` | e.g. `jira`, `PROJ-123` |
| `guardrail_flags` | Present only if the prompt-injection heuristic matched something in the input |
| `_budget_stats` | Present at the top level when called via an API key with usage tracked (see Cost control) |
| `llm_used` | `{provider, model, source}` describing which LLM actually served the call |

## Guardrails applied

- **Input size limit** — rejects (HTTP 413) artifacts over `MAX_AGENT_INPUT_CHARS` (400,000 characters by default)
- **Timeout & retry** — 120s timeout per LLM call, up to 2 retries on transient provider errors (connection/timeout/rate-limit/5xx)
- **Prompt-injection scan** — non-blocking heuristic scan of the submitted content; matches are attached to the response as `guardrail_flags`, never used to block the call
- **Untrusted content wrapping** — content fetched from Jira/GitHub is delimited with `<<<UNTRUSTED_..._START/END>>>` markers so the model treats it as data, never as instructions

## Cost control

- **RBAC** — the calling API key's `scopes` field must include `requirement` or be `all`, else HTTP 403
- **Budget cap** — if the key has a `monthly_budget_usd` set (`PATCH /api/v1/keys/{key_id}/budget`), calls are rejected with HTTP 402 once that month's usage reaches the cap
- `_budget_stats` (`input_tokens`, `output_tokens`, `cost_usd`, `monthly_budget_usd`, `budget_used_usd`) is attached at the top level of the response when called via an API key with usage tracked

## Example

```bash
curl -X POST https://your-host/api/v1/agents/requirement \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "text": "As a clinician, I want to export a patient'\''s visit history to CSV so I can share it with another provider. Acceptance criteria: export completes in under 5 seconds.",
    "project_name": "Patient Portal API",
    "context": "Healthcare SaaS — HIPAA environment"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "requirement",
  "gate_status": "Red",
  "risk_score": 78,
  "total_findings": 4,
  "critical_count": 1,
  "findings": [
    {
      "control_id": "SEC-004",
      "severity": 5,
      "risk_level": "Critical",
      "title": "No access control or audit logging defined for PHI export",
      "description": "The requirement exports patient visit history (PHI) with no stated authorization check or audit trail requirement.",
      "recommendation": "Add: only the treating clinician or an explicitly authorized provider may export; every export must be audit-logged with requester, patient, and timestamp.",
      "is_blocking": true,
      "nist_ref": "GOVERN 1.1"
    }
  ],
  "compliance_flags": ["HIPAA", "GDPR"],
  "post_back_status": "not_requested",
  "source_type": "text",
  "_budget_stats": {"input_tokens": 612, "output_tokens": 480, "cost_usd": 0.0091, "monthly_budget_usd": 50.0, "budget_used_usd": 4.32}
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` · `OWASP-LLM` · `HIPAA` · `GDPR`. Control ID prefixes used in findings: `SEC-001`–`SEC-007` (security requirements), `AI-001`–`AI-010` (AI/ML-specific risk), `GOV-001`–`GOV-008` (governance/ownership/audit).
