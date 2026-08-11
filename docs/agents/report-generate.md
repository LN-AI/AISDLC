# Governance Report Generator

Synthesizes prior review findings — by `review_ids`, a directly-supplied `findings` list, or both — into a comprehensive, audit-ready governance write-up: executive summary, findings by category, compliance coverage matrix, controls assessment, prioritized recommendations, and a release decision. Does not re-run any review agent; it's pure synthesis over findings you already have.

**Module:** Governance Intelligence
**Endpoint:** `POST /api/v1/agents/report-generate`
**Agent name (for scopes/BYOL config):** `report-generate`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

This agent uses `LLMOverrideFields` directly — there is no generic source resolver; findings are supplied, not fetched from GitHub/Jira.

| Field | Type | Required | Description |
|---|---|---|---|
| `project_name` | string | **yes** | e.g. `"Patient Portal API"` |
| `review_ids` | string[] | no | Review IDs (from any prior agent call's `review_id`) whose stored findings to aggregate |
| `findings` | array of `Finding` | no | Findings to synthesize directly, instead of (or in addition to) `review_ids`. Each: `control_id`, `severity` (1-5), `risk_level`, `title`, `description`, `recommendation`, `is_blocking`, optional `owasp_ref`, `nist_ref` |
| `gate_status` | string | no | `Green`\|`Amber`\|`Red` — computed from the aggregated findings' severities if omitted (`Red` if any severity-5 finding, else `Amber` if any severity-4, else `Green`) |
| `overall_risk_score` | number | no (0-100) | Computed via the same weighted-average formula as [`full`](full.md) if omitted and `review_ids` were supplied; `0.0` if neither is available |
| `context` | string | no | Free-text context injected into the synthesis prompt |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `review_ids` · `findings`. Both are optional individually but at least one should be supplied for a meaningful output — an empty call still succeeds and produces a mostly-empty Green result.

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `report_id` | UUID for this synthesized document |
| `project_name`, `gate_status`, `overall_risk_score` | Echoed/computed as above |
| `executive_summary` | One-paragraph risk posture summary |
| `findings_by_category` | `{"Security": [...], "AI Safety": [...], "Governance": [...], "Other": [...]}` — findings bucketed by their `control_id` prefix (`SEC`->Security, `AI`->AI Safety, `GOV`->Governance, anything else->Other) |
| `compliance_mapping` | `{framework_flag: []}` for every distinct `compliance_flags` value seen across the source reviews (an empty list per key in the current implementation — presence indicates the framework was in scope) |
| `controls_checked` | Every control ID in the shared control library |
| `controls_passed` | Control IDs from `controls_checked` **not** violated by any aggregated finding |
| `controls_failed` | Control IDs that appear on at least one aggregated finding |
| `recommendations[]` | Prioritized actions from the LLM synthesis step |
| `approval_required` | bool — `true` when `gate_status` is `Amber` or `Red` |
| `approvers_needed[]` | Derived from findings: `"Security Engineer (mandatory...)"` for Red, `"Team Lead (required for Amber approval)"` for Amber, plus `"Compliance Officer"` if any HIPAA reference is present, `"Chief Risk Officer or CISO"` if an `AI-003` finding is present, `"Security Lead"` if any Critical-severity finding is present |
| `human_oversight_required` | bool — `true` if any aggregated finding has `control_id == "AI-003"` (high-risk AI decision without human approval) |
| `_budget_stats` | **At the top level**, explicitly included by this handler |

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars), applied to the aggregated findings/summary text sent to the model
- **Timeout & retry** — 900s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; would surface as `guardrail_flags` on the internal result, though this field is not included in the explicit top-level response shape above
- No GitHub/Jira fetch happens for this endpoint — all input is either findings you supply directly or findings already stored from prior agent calls, so there is no untrusted-content wrapping step

## Cost control

- **RBAC** — this key's `scopes` must include `report-generate` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` appears at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/report-generate \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "project_name": "Patient Portal API",
    "review_ids": ["b3f1c2a4-9e77-4c11-9a0d-1234567890ab"],
    "context": "Pre-release governance write-up for v3.2.0 sign-off"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "report-generate",
  "report_id": "9a21c7e0-...",
  "project_name": "Patient Portal API",
  "gate_status": "Amber",
  "overall_risk_score": 58.0,
  "executive_summary": "The release has no unresolved Critical findings. One High-severity governance gap around AI oversight requires reviewer sign-off before Green.",
  "findings_by_category": {"Security": [], "AI Safety": [{"control_id": "AI-003", "severity": 4}], "Governance": []},
  "controls_passed": ["SEC-001", "SEC-002", "..."],
  "controls_failed": ["AI-003"],
  "approval_required": true,
  "approvers_needed": ["Team Lead (required for Amber approval)", "Chief Risk Officer or CISO (high-risk AI decision-making)"],
  "human_oversight_required": true,
  "_budget_stats": {"input_tokens": 1890, "output_tokens": 1120, "cost_usd": 0.021, "monthly_budget_usd": 50.0, "budget_used_usd": 6.55}
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` · `OWASP-LLM` · `HIPAA` · `GDPR` · `ISO27001` · `GxP` — whichever frameworks appear in the source findings' `compliance_flags`. Findings are bucketed by the shared `SEC-xxx`/`AI-xxx`/`GOV-xxx` control library prefixes.
