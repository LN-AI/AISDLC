# Deployment Readiness

Reviews a release/deployment plan, changelog, or the pull request being released for rollout risk: rollback and recovery, rollout strategy (staged vs. big-bang), observability named for the change, dependency readiness, and change-management evidence. Flags every gap and cites the exact plan text it applies to rather than manufacturing findings against a well-prepared release.

**Module:** DevOps Intelligence
**Endpoint:** `POST /api/v1/agents/deployment-readiness`
**Agent name (for scopes/BYOL config):** `deployment-readiness`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access, issued from the dashboard's **API Keys** page
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

External-key calls are also subject to per-minute/per-day rate limits (`X-RateLimit-*` response headers; `429` + `Retry-After` on breach).

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | one of text/github_pr_url | Deployment/release plan, changelog, or runbook text to review |
| `github_pr_url` | string | " | GitHub Pull Request URL — reviews readiness of the change being released |
| `project_name` | string | no | Project/product name stored in the audit trail |
| `context` | string | no | Free-text context injected into the review prompt |
| `post_back` | boolean | no (default `false`) | Post the result as a comment on the originating PR |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Bring-your-own-LLM override for this single call — see below |

**Sources accepted:** `text` · `github_pr_url`. Resolution priority when both are supplied: `github_pr_url` > raw `text`.

### Bring your own LLM (optional)
Every request field set (`llm_provider`/`llm_api_key`/`llm_base_url`/`llm_model`) routes this single call through your own Anthropic or OpenAI-compatible credentials. Resolution order: per-request fields > this API key's per-agent override for `deployment-readiness` > this key's global BYOL default > platform default.

## Response

Standard envelope (`_wrap`):

| Field | Description |
|---|---|
| `review_id` | UUID for this review, usable in `release-gate`/`report-generate` `review_ids` |
| `gate_status` | `Green` \| `Amber` \| `Red` |
| `risk_score` | 0–100, `Impact × Likelihood × Exposure × Autonomy` |
| `total_findings`, `critical_count`, `high_count` | Finding counts |
| `findings[]` | Each: `control_id` (e.g. `GOV-004`, `GOV-005`), `severity` (1–5), `risk_level`, `title`, `description`, `recommendation`, `is_blocking`, optional `owasp_ref`, `nist_ref` |
| `summary` | 2–3 sentence executive summary |
| `recommendations[]` | Top 3–5 prioritized actions |
| `compliance_flags[]` | e.g. `SOC2` |
| `post_back_status` | `not_requested` \| `posted` \| `failed` |
| `source_type`, `source_ref` | e.g. `github_pr`, PR title |
| `guardrail_flags` | Present only if the prompt-injection heuristic matched something in the input |
| `_budget_stats` | Present at the top level when called via an API key with usage tracked (see Cost control) |
| `llm_used` | `{provider, model, source}` describing which LLM actually served the call |

Gate logic: Red on no rollback plan for a high-blast-radius change, a schema/data migration with no safe rollback, a risky change with zero monitoring named, or a release with no test evidence at all; Amber on an untested rollback plan, single-shot rollout for a moderate-risk change, partial monitoring coverage, or missing on-call assignment; Green on a staged/flagged rollout with a tested rollback path, named monitoring and on-call, and referenced test evidence.

## Guardrails applied

- **Input size limit** — rejects (HTTP 413) artifacts over `MAX_AGENT_INPUT_CHARS` (400,000 characters by default)
- **Timeout & retry** — 900s timeout per LLM call, up to 2 retries on transient provider errors (connection/timeout/rate-limit/5xx)
- **Prompt-injection scan** — non-blocking heuristic scan of the submitted content; matches are attached to the response as `guardrail_flags`, never used to block the call
- **Untrusted content wrapping** — a fetched PR diff is delimited with `<<<UNTRUSTED_GITHUB_PR_DIFF_START/END>>>` markers so the model treats it as data, never as instructions

## Cost control

- **RBAC** — the calling API key's `scopes` field must include `deployment-readiness` or be `all`, else HTTP 403
- **Budget cap** — if the key has a `monthly_budget_usd` set (`PATCH /api/v1/keys/{key_id}/budget`), calls are rejected with HTTP 402 once that month's usage reaches the cap
- `_budget_stats` (`input_tokens`, `output_tokens`, `cost_usd`, `monthly_budget_usd`, `budget_used_usd`) is attached at the top level of the response when called via an API key with usage tracked

## Example

```bash
curl -X POST https://your-host/api/v1/agents/deployment-readiness \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "text": "Deploying v3.2 with a payments schema migration. Big-bang rollout to all regions, no feature flag. Rollback plan: redeploy previous image (migration not reversed).",
    "project_name": "Payments Service",
    "context": "production, EU region"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "deployment-readiness",
  "gate_status": "Red",
  "risk_score": 85,
  "total_findings": 3,
  "critical_count": 1,
  "findings": [
    {
      "control_id": "GOV-005",
      "severity": 5,
      "risk_level": "Critical",
      "title": "No safe rollback for a schema migration on a big-bang rollout",
      "description": "The plan states the migration is not reversed on rollback, so redeploying the previous image would run against an incompatible schema.",
      "recommendation": "Add a backward-compatible migration step (expand/contract pattern) or a tested down-migration before rolling out to all regions at once.",
      "is_blocking": true
    }
  ],
  "compliance_flags": ["SOC2"],
  "post_back_status": "not_requested",
  "source_type": "text",
  "_budget_stats": {"input_tokens": 540, "output_tokens": 410, "cost_usd": 0.0079, "monthly_budget_usd": 50.0, "budget_used_usd": 4.4}
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` · `SOC2`. Control ID prefixes used in findings: `GOV-001`–`GOV-008` (governance/approval/audit), `SEC-xxx` where a security control applies to the rollout.
