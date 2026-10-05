# CI/CD Pipeline Review

Reviews pipeline definitions (GitHub Actions, GitLab CI, Jenkinsfile, CircleCI, Azure Pipelines) for security and best-practice issues: hardcoded secrets, unpinned third-party actions, overly broad token permissions, privilege-escalation triggers, and missing approval gates before a production deploy job. Flags every gap and cites the exact job/step name it applies to rather than manufacturing findings against a well-hardened pipeline.

**Module:** DevOps Intelligence
**Endpoint:** `POST /api/v1/agents/cicd-pipeline-review`
**Agent name (for scopes/BYOL config):** `cicd-pipeline-review`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access, issued from the dashboard's **API Keys** page
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

External-key calls are also subject to per-minute/per-day rate limits (`X-RateLimit-*` response headers; `429` + `Retry-After` on breach).

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | one of text/github_file_url | Pasted pipeline definition (YAML or Jenkinsfile text) to review |
| `github_file_url` | string | " | GitHub file blob URL pointing at the pipeline definition |
| `project_name` | string | no | Project/product name stored in the audit trail |
| `context` | string | no | Free-text context injected into the review prompt |
| `post_back` | boolean | no (default `false`) | Post the result as a comment on the originating PR/issue |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Bring-your-own-LLM override for this single call — see below |

**Sources accepted:** `text` · `github_file_url`. Resolution priority when both are supplied: `github_file_url` > raw `text`.

### Bring your own LLM (optional)
Every request field set (`llm_provider`/`llm_api_key`/`llm_base_url`/`llm_model`) routes this single call through your own Anthropic or OpenAI-compatible credentials. Resolution order: per-request fields > this API key's per-agent override for `cicd-pipeline-review` > this key's global BYOL default > platform default.

## Response

Standard envelope (`_wrap`):

| Field | Description |
|---|---|
| `review_id` | UUID for this review, usable in `release-gate`/`report-generate` `review_ids` |
| `gate_status` | `Green` \| `Amber` \| `Red` |
| `risk_score` | 0–100, `Impact × Likelihood × Exposure × Autonomy` |
| `total_findings`, `critical_count`, `high_count` | Finding counts |
| `findings[]` | Each: `control_id` (e.g. `SEC-003`, `SEC-007`, `GOV-004`), `severity` (1–5), `risk_level`, `title`, `description`, `recommendation`, `is_blocking`, optional `owasp_ref`, `nist_ref` |
| `summary` | 2–3 sentence executive summary |
| `recommendations[]` | Top 3–5 prioritized actions |
| `compliance_flags[]` | e.g. `OWASP-Web-Top10` |
| `post_back_status` | `not_requested` \| `posted` \| `failed` |
| `source_type`, `source_ref` | e.g. `github_file`, file name |
| `guardrail_flags` | Present only if the prompt-injection heuristic matched something in the input |
| `_budget_stats` | Present at the top level when called via an API key with usage tracked (see Cost control) |
| `llm_used` | `{provider, model, source}` describing which LLM actually served the call |

Gate logic: Red on a hardcoded secret/credential in the file, a `pull_request_target`-style trigger with write secrets and no hardening, or a production deploy job with no approval gate and no preceding tests; Amber on unpinned third-party action versions, overly broad token permissions, or missing timeout/concurrency control; Green on least-privilege permissions, pinned actions, required approvals before deploy, and tests gating the deploy job.

## Guardrails applied

- **Input size limit** — rejects (HTTP 413) artifacts over `MAX_AGENT_INPUT_CHARS` (400,000 characters by default)
- **Timeout & retry** — 900s timeout per LLM call, up to 2 retries on transient provider errors (connection/timeout/rate-limit/5xx)
- **Prompt-injection scan** — non-blocking heuristic scan of the submitted content; matches are attached to the response as `guardrail_flags`, never used to block the call
- **Untrusted content wrapping** — a fetched pipeline file is delimited with `<<<UNTRUSTED_GITHUB_FILE_START/END>>>` markers so the model treats it as data, never as instructions

## Cost control

- **RBAC** — the calling API key's `scopes` field must include `cicd-pipeline-review` or be `all`, else HTTP 403
- **Budget cap** — if the key has a `monthly_budget_usd` set (`PATCH /api/v1/keys/{key_id}/budget`), calls are rejected with HTTP 402 once that month's usage reaches the cap
- `_budget_stats` (`input_tokens`, `output_tokens`, `cost_usd`, `monthly_budget_usd`, `budget_used_usd`) is attached at the top level of the response when called via an API key with usage tracked

## Example

```bash
curl -X POST https://your-host/api/v1/agents/cicd-pipeline-review \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "text": "name: deploy\non:\n  pull_request_target:\njobs:\n  deploy:\n    permissions: write-all\n    steps:\n      - uses: some/action@main\n      - run: echo $DEPLOY_TOKEN",
    "project_name": "Patient Portal API"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "cicd-pipeline-review",
  "gate_status": "Red",
  "risk_score": 92,
  "total_findings": 3,
  "critical_count": 2,
  "findings": [
    {
      "control_id": "SEC-003",
      "severity": 5,
      "risk_level": "Critical",
      "title": "Secret printed to logs in the deploy job",
      "description": "`run: echo $DEPLOY_TOKEN` writes the deploy token's value directly into the job log.",
      "recommendation": "Remove the echo statement; never print a secret-bearing variable, even for debugging.",
      "is_blocking": true
    }
  ],
  "compliance_flags": ["OWASP-Web-Top10"],
  "post_back_status": "not_requested",
  "source_type": "text",
  "_budget_stats": {"input_tokens": 480, "output_tokens": 390, "cost_usd": 0.0071, "monthly_budget_usd": 50.0, "budget_used_usd": 4.47}
}
```

## Frameworks / controls referenced

`OWASP-Web-Top10` · `SOC2`. Control ID prefixes used in findings: `SEC-001`–`SEC-007` (secrets, credentials, access, dependencies), `GOV-004` (approval workflow).
