# Release Gate

The final governance checkpoint before a feature moves to production. Aggregates findings from prior reviews (by `review_ids`, or from a supplied evidence summary) against a fixed Red/Amber/Green decision framework — decisive by design, never hedging.

**Module:** Governance Intelligence
**Endpoint:** `POST /api/v1/agents/release-gate`
**Agent name (for scopes/BYOL config):** `release-gate`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

This agent does not use the standard source-resolution model (`_SourceBase`) — it takes `LLMOverrideFields` plus its own fields directly, and there is no `github_pr_url`/`jira_ticket`/`text` source to fetch.

| Field | Type | Required | Description |
|---|---|---|---|
| `project_name` | string | **yes** | Project/product name |
| `review_ids` | string[] | no | Prior review IDs (returned as `review_id` from any other agent call) to aggregate findings from automatically |
| `release_version` | string | no | Version tag or release name, e.g. `v3.2.0` |
| `evidence_summary` | string | no | Manual evidence text: test pass rates, pen-test report refs, sign-off notes, etc. — used instead of, or alongside, `review_ids` |
| `context` | string | no | Additional release context |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `review_ids` · `evidence_summary`. Neither is strictly required by the schema, but a call with both empty will simply evaluate against "No findings submitted" and "No evidence summary provided," which typically returns a low-confidence Amber/Red — supply at least one for a meaningful decision.

## Response

Standard envelope (`_wrap`), with `source_type` hardcoded to `"internal"` and `source_ref`/`project_name` set to the request's `project_name`:

- `gate_status` — `Green` \| `Amber` \| `Red`, the definitive release decision
- `risk_score` — weighted average across aggregated sub-reviews
- `findings[]` — the aggregated findings actually driving the decision (from `review_ids` and/or the mandatory-control check)
- `summary` — executive-dashboard-ready justification
- `recommendations[]` — required actions before Green
- `compliance_flags[]`
- `total_findings`, `critical_count`, `high_count`
- `_budget_stats` at the top level

Gate Decision Framework:
- **Red** (mandatory block) — any unresolved Critical finding; PHI/financial data logging; hardcoded secrets; SQL injection/auth bypass/command injection; AI prompt injection; high-risk AI decisions without human approval; no audit trail for a regulated system; missing encryption for sensitive data.
- **Amber** (requires named reviewer approval) — High findings mitigated but not resolved; missing rollback/monitoring plan; unvalidated AI model performance; test coverage below 70%; undocumented accepted risk.
- **Green** — all Critical/High findings resolved or formally accepted, mandatory controls (`SEC-001`, `SEC-003`, `SEC-004`, `AI-001`, `AI-003`, `GOV-004`, `GOV-005`) satisfied, evidence documented.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars) applies to the aggregated evidence/findings text sent to the model
- **Timeout & retry** — 120s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; flags surfaced as `guardrail_flags`
- No GitHub/Jira fetch happens for this endpoint, so there is no untrusted-content wrapping step — all input is either your own `evidence_summary` text or findings already stored from prior agent calls

## Cost control

- **RBAC** — this key's `scopes` must include `release-gate` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` appears at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/release-gate \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "project_name": "Patient Portal API",
    "release_version": "v3.2.0",
    "review_ids": ["b3f1c2a4-9e77-4c11-9a0d-1234567890ab", "7a2e5d90-11cd-4a88-9b3f-abcdef012345"],
    "evidence_summary": "Regression suite: 94% pass. Pen-test report PT-2026-014 filed, no criticals. Rollback plan: blue/green swap documented in runbook."
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "release-gate",
  "gate_status": "Amber",
  "risk_score": 42,
  "findings": [
    {"control_id": "GOV-005", "severity": 4, "risk_level": "High", "title": "Monitoring plan for new AI feature not documented", "is_blocking": false}
  ],
  "recommendations": ["Document a monitoring/alerting plan for the new triage-assist feature before Green.", "Named reviewer (Team Lead) must approve given the High-severity monitoring gap."],
  "summary": "No unresolved Critical findings. One High-severity governance gap (monitoring plan) requires reviewer approval before Green.",
  "source_type": "internal"
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` · `SOC2` · `HIPAA`. Mandatory blocking controls checked: `SEC-001`, `SEC-003`, `SEC-004`, `AI-001`, `AI-003`, `GOV-004`, `GOV-005`, plus any other control marked `blocking=True` in the shared control library.
