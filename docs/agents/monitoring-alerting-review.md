# Monitoring & Alerting Config Review

Reviews alerting rules, SLO/SLI definitions, and dashboard configuration for coverage gaps: missing golden-signal alerts, unjustified thresholds, SLOs with no matching SLI or error budget policy, and dashboards with no baseline to compare against. Flags every gap and cites the exact alert rule, SLO, or panel it applies to rather than manufacturing findings against well-covered monitoring.

**Module:** Operations Intelligence
**Endpoint:** `POST /api/v1/agents/monitoring-alerting-review`
**Agent name (for scopes/BYOL config):** `monitoring-alerting-review`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access, issued from the dashboard's **API Keys** page
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

External-key calls are also subject to per-minute/per-day rate limits (`X-RateLimit-*` response headers; `429` + `Retry-After` on breach).

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | one of text/github_file_url | Pasted alerting rules, SLO/SLI definitions, or dashboard config (e.g. Prometheus alert rules, Grafana JSON) |
| `github_file_url` | string | " | GitHub file blob URL pointing at the alerting/monitoring config |
| `project_name` | string | no | Project/product name stored in the audit trail |
| `context` | string | no | Free-text context injected into the review prompt |
| `post_back` | boolean | no (default `false`) | Post the result as a comment on the originating PR/issue |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Bring-your-own-LLM override for this single call — see below |

**Sources accepted:** `text` · `github_file_url`. Resolution priority when both are supplied: `github_file_url` > raw `text`.

### Bring your own LLM (optional)
Every request field set (`llm_provider`/`llm_api_key`/`llm_base_url`/`llm_model`) routes this single call through your own Anthropic or OpenAI-compatible credentials. Resolution order: per-request fields > this API key's per-agent override for `monitoring-alerting-review` > this key's global BYOL default > platform default.

## Response

Standard envelope (`_wrap`):

| Field | Description |
|---|---|
| `review_id` | UUID for this review, usable in `release-gate`/`report-generate` `review_ids` |
| `gate_status` | `Green` \| `Amber` \| `Red` |
| `risk_score` | 0–100, `Impact × Likelihood × Exposure × Autonomy` |
| `total_findings`, `critical_count`, `high_count` | Finding counts |
| `findings[]` | Each: `control_id` (e.g. `GOV-005`, `SEC-005`), `severity` (1–5), `risk_level`, `title`, `description`, `recommendation`, `is_blocking`, optional `owasp_ref`, `nist_ref` |
| `summary` | 2–3 sentence executive summary |
| `recommendations[]` | Top 3–5 prioritized actions |
| `compliance_flags[]` | e.g. `SOC2` |
| `post_back_status` | `not_requested` \| `posted` \| `failed` |
| `source_type`, `source_ref` | e.g. `github_file`, file name |
| `guardrail_flags` | Present only if the prompt-injection heuristic matched something in the input |
| `_budget_stats` | Present at the top level when called via an API key with usage tracked (see Cost control) |
| `llm_used` | `{provider, model, source}` describing which LLM actually served the call |

Gate logic: Red on no alerting at all on a described critical path, or a page-worthy alert with no runbook/remediation guidance and no clear owner; Amber on partial golden-signal coverage, unjustified thresholds, or an SLO defined with no matching SLI or error budget policy; Green on golden-signal coverage on critical paths, justified thresholds with page/ticket routing, and SLOs with matching SLIs and a stated error budget policy.

## Guardrails applied

- **Input size limit** — rejects (HTTP 413) artifacts over `MAX_AGENT_INPUT_CHARS` (400,000 characters by default)
- **Timeout & retry** — 900s timeout per LLM call, up to 2 retries on transient provider errors (connection/timeout/rate-limit/5xx)
- **Prompt-injection scan** — non-blocking heuristic scan of the submitted content; matches are attached to the response as `guardrail_flags`, never used to block the call
- **Untrusted content wrapping** — a fetched config file is delimited with `<<<UNTRUSTED_GITHUB_FILE_START/END>>>` markers so the model treats it as data, never as instructions

## Cost control

- **RBAC** — the calling API key's `scopes` field must include `monitoring-alerting-review` or be `all`, else HTTP 403
- **Budget cap** — if the key has a `monthly_budget_usd` set (`PATCH /api/v1/keys/{key_id}/budget`), calls are rejected with HTTP 402 once that month's usage reaches the cap
- `_budget_stats` (`input_tokens`, `output_tokens`, `cost_usd`, `monthly_budget_usd`, `budget_used_usd`) is attached at the top level of the response when called via an API key with usage tracked

## Example

```bash
curl -X POST https://your-host/api/v1/agents/monitoring-alerting-review \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "text": "groups:\n- name: checkout\n  rules:\n  - alert: HighErrorRate\n    expr: rate(http_requests_total{status=~\"5..\"}[5m]) > 0.5",
    "project_name": "Checkout Service",
    "context": "production"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "monitoring-alerting-review",
  "gate_status": "Amber",
  "risk_score": 55,
  "total_findings": 2,
  "critical_count": 0,
  "findings": [
    {
      "control_id": "GOV-005",
      "severity": 3,
      "risk_level": "Moderate",
      "title": "No latency or saturation alert alongside the error-rate alert",
      "description": "Only `HighErrorRate` is defined; the golden signals also include latency, traffic, and saturation, none of which have a rule here.",
      "recommendation": "Add a latency SLO-backed alert (e.g. p99 over threshold) and a saturation alert (CPU/memory/queue depth) for the checkout service.",
      "is_blocking": false
    }
  ],
  "compliance_flags": ["SOC2"],
  "post_back_status": "not_requested",
  "source_type": "text",
  "_budget_stats": {"input_tokens": 300, "output_tokens": 260, "cost_usd": 0.0047, "monthly_budget_usd": 50.0, "budget_used_usd": 4.65}
}
```

## Frameworks / controls referenced

`SOC2` · `NIST-AI-RMF`. Control ID prefixes used in findings: `GOV-005` (operational visibility / audit trail), `SEC-005` (rate limiting / saturation-related alerting).
