# Incident & Postmortem

Reviews an incident timeline, log excerpt, or draft postmortem for completeness, in the blameless tradition: timeline gaps, root cause vs. contributing factors, detection/response quality, and specific owned action items. Flags every gap and quotes the exact timeline entry or statement it applies to rather than manufacturing findings against a well-documented incident. Never suggests disciplinary action against a person.

**Module:** Operations Intelligence
**Endpoint:** `POST /api/v1/agents/incident-postmortem`
**Agent name (for scopes/BYOL config):** `incident-postmortem`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access, issued from the dashboard's **API Keys** page
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

External-key calls are also subject to per-minute/per-day rate limits (`X-RateLimit-*` response headers; `429` + `Retry-After` on breach).

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes | Incident timeline, log excerpt, or draft postmortem to review |
| `project_name` | string | no | Project/product name stored in the audit trail |
| `context` | string | no | Free-text context injected into the review prompt |
| `post_back` | boolean | no (default `false`) | Post the result as a comment on the originating PR/issue |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Bring-your-own-LLM override for this single call — see below |

**Sources accepted:** `text` only — this agent has no GitHub/Jira source, since a postmortem draft is normally pasted directly.

### Bring your own LLM (optional)
Every request field set (`llm_provider`/`llm_api_key`/`llm_base_url`/`llm_model`) routes this single call through your own Anthropic or OpenAI-compatible credentials. Resolution order: per-request fields > this API key's per-agent override for `incident-postmortem` > this key's global BYOL default > platform default.

## Response

Standard envelope (`_wrap`):

| Field | Description |
|---|---|
| `review_id` | UUID for this review, usable in `release-gate`/`report-generate` `review_ids` |
| `gate_status` | `Green` \| `Amber` \| `Red` |
| `risk_score` | 0–100, `Impact × Likelihood × Exposure × Autonomy` |
| `total_findings`, `critical_count`, `high_count` | Finding counts |
| `findings[]` | Each: `control_id` (e.g. `GOV-005`), `severity` (1–5), `risk_level`, `title`, `description`, `recommendation`, `is_blocking`, optional `owasp_ref`, `nist_ref` |
| `summary` | 2–3 sentence executive summary |
| `recommendations[]` | Top 3–5 prioritized actions |
| `compliance_flags[]` | e.g. `SOC2` |
| `post_back_status` | `not_requested` \| `posted` \| `failed` |
| `source_type`, `source_ref` | always `text` for this agent |
| `guardrail_flags` | Present only if the prompt-injection heuristic matched something in the input |
| `_budget_stats` | Present at the top level when called via an API key with usage tracked (see Cost control) |
| `llm_used` | `{provider, model, source}` describing which LLM actually served the call |

Gate logic: Red on no root cause identified at all, blame assigned to an individual rather than a system/process gap, or zero action items for a Critical/high-impact incident; Amber on a root cause given but contributing factors missing, action items present but vague or unowned, or a detection/alerting gap not addressed; Green on a clear timeline, root cause with contributing factors, specific owned action items, and blameless framing.

## Guardrails applied

- **Input size limit** — rejects (HTTP 413) artifacts over `MAX_AGENT_INPUT_CHARS` (400,000 characters by default)
- **Timeout & retry** — 900s timeout per LLM call, up to 2 retries on transient provider errors (connection/timeout/rate-limit/5xx)
- **Prompt-injection scan** — non-blocking heuristic scan of the submitted content; matches are attached to the response as `guardrail_flags`, never used to block the call

## Cost control

- **RBAC** — the calling API key's `scopes` field must include `incident-postmortem` or be `all`, else HTTP 403
- **Budget cap** — if the key has a `monthly_budget_usd` set (`PATCH /api/v1/keys/{key_id}/budget`), calls are rejected with HTTP 402 once that month's usage reaches the cap
- `_budget_stats` (`input_tokens`, `output_tokens`, `cost_usd`, `monthly_budget_usd`, `budget_used_usd`) is attached at the top level of the response when called via an API key with usage tracked

## Example

```bash
curl -X POST https://your-host/api/v1/agents/incident-postmortem \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "text": "14:02 alert fired. 14:10 engineer investigated. 14:40 rolled back. Root cause: the deploy broke it. Action items: be more careful next time.",
    "project_name": "Checkout Service",
    "context": "Sev-2, 2026-09-14"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "incident-postmortem",
  "gate_status": "Red",
  "risk_score": 70,
  "total_findings": 3,
  "critical_count": 1,
  "findings": [
    {
      "control_id": "GOV-005",
      "severity": 4,
      "risk_level": "High",
      "title": "Root cause stated as proximate trigger only, no contributing factors",
      "description": "\"The deploy broke it\" names the trigger but does not explain why the deploy was able to reach production broken, or why no safeguard caught it.",
      "recommendation": "Add contributing factors: e.g. no pre-deploy smoke test covered this path, no canary stage, or no alert on the specific failure mode.",
      "is_blocking": true
    }
  ],
  "compliance_flags": ["SOC2"],
  "post_back_status": "not_requested",
  "source_type": "text",
  "_budget_stats": {"input_tokens": 350, "output_tokens": 300, "cost_usd": 0.0054, "monthly_budget_usd": 50.0, "budget_used_usd": 4.6}
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` · `SOC2`. Control ID prefix used in findings: `GOV-005` (audit trail / incident record completeness).
