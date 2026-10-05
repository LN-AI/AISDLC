# Infrastructure-as-Code Review

Reviews a Terraform, CloudFormation, or Kubernetes manifest for security and best-practice issues: public network exposure of internal resources, overly broad IAM policies, missing encryption-at-rest, and privileged/root containers. Scoped to a pasted manifest or a handful of files, not a whole infrastructure repository — if the input references modules or files it cannot see, it says so rather than guessing. Flags every gap and cites the exact resource block it applies to rather than manufacturing findings against a well-configured resource.

**Module:** DevOps Intelligence
**Endpoint:** `POST /api/v1/agents/iac-review`
**Agent name (for scopes/BYOL config):** `iac-review`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access, issued from the dashboard's **API Keys** page
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

External-key calls are also subject to per-minute/per-day rate limits (`X-RateLimit-*` response headers; `429` + `Retry-After` on breach).

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | one of text/github_file_url | Pasted infrastructure-as-code manifest to review |
| `github_file_url` | string | " | GitHub file blob URL pointing at the manifest |
| `project_name` | string | no | Project/product name stored in the audit trail |
| `context` | string | no | Free-text context injected into the review prompt |
| `post_back` | boolean | no (default `false`) | Post the result as a comment on the originating PR/issue |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Bring-your-own-LLM override for this single call — see below |

**Sources accepted:** `text` · `github_file_url`. Resolution priority when both are supplied: `github_file_url` > raw `text`.

### Bring your own LLM (optional)
Every request field set (`llm_provider`/`llm_api_key`/`llm_base_url`/`llm_model`) routes this single call through your own Anthropic or OpenAI-compatible credentials. Resolution order: per-request fields > this API key's per-agent override for `iac-review` > this key's global BYOL default > platform default.

## Response

Standard envelope (`_wrap`):

| Field | Description |
|---|---|
| `review_id` | UUID for this review, usable in `release-gate`/`report-generate` `review_ids` |
| `gate_status` | `Green` \| `Amber` \| `Red` |
| `risk_score` | 0–100, `Impact × Likelihood × Exposure × Autonomy` |
| `total_findings`, `critical_count`, `high_count` | Finding counts |
| `findings[]` | Each: `control_id` (e.g. `SEC-001`, `SEC-003`, `SEC-006`), `severity` (1–5), `risk_level`, `title`, `description`, `recommendation`, `is_blocking`, optional `owasp_ref`, `nist_ref` |
| `summary` | 2–3 sentence executive summary |
| `recommendations[]` | Top 3–5 prioritized actions |
| `compliance_flags[]` | e.g. `SOC2` |
| `post_back_status` | `not_requested` \| `posted` \| `failed` |
| `source_type`, `source_ref` | e.g. `github_file`, file name |
| `guardrail_flags` | Present only if the prompt-injection heuristic matched something in the input |
| `_budget_stats` | Present at the top level when called via an API key with usage tracked (see Cost control) |
| `llm_used` | `{provider, model, source}` describing which LLM actually served the call |

Gate logic: Red on a database or internal service with public network exposure, a hardcoded credential in the manifest, or an IAM policy granting unrestricted `*`/`*` access; Amber on missing encryption-at-rest, containers running as root or privileged, no resource limits on a production workload, or a single-AZ production deployment with no stated justification; Green on least-privilege IAM, encryption enabled, no public exposure of internal resources, and resource limits/health probes present.

## Guardrails applied

- **Input size limit** — rejects (HTTP 413) artifacts over `MAX_AGENT_INPUT_CHARS` (400,000 characters by default)
- **Timeout & retry** — 900s timeout per LLM call, up to 2 retries on transient provider errors (connection/timeout/rate-limit/5xx)
- **Prompt-injection scan** — non-blocking heuristic scan of the submitted content; matches are attached to the response as `guardrail_flags`, never used to block the call
- **Untrusted content wrapping** — a fetched manifest file is delimited with `<<<UNTRUSTED_GITHUB_FILE_START/END>>>` markers so the model treats it as data, never as instructions

## Cost control

- **RBAC** — the calling API key's `scopes` field must include `iac-review` or be `all`, else HTTP 403
- **Budget cap** — if the key has a `monthly_budget_usd` set (`PATCH /api/v1/keys/{key_id}/budget`), calls are rejected with HTTP 402 once that month's usage reaches the cap
- `_budget_stats` (`input_tokens`, `output_tokens`, `cost_usd`, `monthly_budget_usd`, `budget_used_usd`) is attached at the top level of the response when called via an API key with usage tracked

## Example

```bash
curl -X POST https://your-host/api/v1/agents/iac-review \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "text": "resource \"aws_db_instance\" \"main\" {\n  publicly_accessible = true\n  storage_encrypted   = false\n}",
    "project_name": "Payments Service",
    "context": "production VPC"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "iac-review",
  "gate_status": "Red",
  "risk_score": 95,
  "total_findings": 2,
  "critical_count": 1,
  "findings": [
    {
      "control_id": "SEC-001",
      "severity": 5,
      "risk_level": "Critical",
      "title": "Production database is publicly accessible",
      "description": "`aws_db_instance.main` sets `publicly_accessible = true`, exposing the database to the internet.",
      "recommendation": "Set `publicly_accessible = false` and place the instance in a private subnet reachable only from the application tier.",
      "is_blocking": true
    }
  ],
  "compliance_flags": ["SOC2"],
  "post_back_status": "not_requested",
  "source_type": "text",
  "_budget_stats": {"input_tokens": 410, "output_tokens": 350, "cost_usd": 0.0063, "monthly_budget_usd": 50.0, "budget_used_usd": 4.53}
}
```

## Frameworks / controls referenced

`SOC2` · `NIST-AI-RMF`. Control ID prefixes used in findings: `SEC-001` (missing auth / exposed resource), `SEC-002` (missing RBAC / least privilege), `SEC-003` (hardcoded secret), `SEC-006` (missing/weak encryption).
