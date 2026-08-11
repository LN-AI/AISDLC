# Compliance Mapper

Maps a requirement, architecture description, policy, or design document to NIST AI RMF, OWASP LLM Top 10, HIPAA, GDPR, ISO 27001, GxP/21 CFR Part 11, PCI DSS, and SOC 2 — for each applicable framework, lists which controls are covered, which are missing, what audit evidence would be needed, and whether each is mandatory or recommended.

**Module:** Security Intelligence
**Endpoint:** `POST /api/v1/agents/compliance`
**Agent name (for scopes/BYOL config):** `compliance`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | one of text/jira_ticket | Artifact to map — requirement, architecture doc, policy, or system design |
| `jira_ticket` | string | " | Jira ticket ID or URL |
| `frameworks` | string[] | no | Restrict mapping to specific frameworks, e.g. `["HIPAA", "GDPR"]` — leave empty for auto-detect |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `text` · `jira_ticket`. Resolution priority: `jira_ticket` > `text`.

## Response

Standard envelope (`_wrap`):

- `gate_status`, `risk_score` (0–100; reflects compliance-gap severity: 80–100 = multiple critical regulatory gaps, 0–19 = well-covered with minor documentation needs)
- `findings[]` — each mapped to a control ID (`SEC-xxx`, `AI-xxx`, `GOV-xxx`), with `severity`, `risk_level`, `title`, `description`, `recommendation`, `is_blocking`
- `summary`, `recommendations[]`
- `compliance_flags[]` — the frameworks actually triggered by this artifact
- `post_back_status`, `source_type` (`jira` \| `text`), `source_ref`
- `_budget_stats` at the top level

Gate logic: **Red** — fundamental gap in a mandatory framework (e.g. a PHI system with no HIPAA controls). **Amber** — key controls missing but not yet in production. **Green** — adequate coverage with minor documentation gaps.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 900s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; flags surfaced as `guardrail_flags`
- **Untrusted content wrapping** — a fetched Jira ticket body is wrapped before analysis

## Cost control

- **RBAC** — this key's `scopes` must include `compliance` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` appears at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/compliance \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "text": "System design: a microservice stores patient lab results and exposes them via an internal API to three partner clinics. No encryption-at-rest is mentioned; access is via a shared service account.",
    "frameworks": ["HIPAA", "GDPR"],
    "project_name": "LabResults Service"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "compliance",
  "gate_status": "Red",
  "risk_score": 88,
  "findings": [
    {
      "control_id": "SEC-006",
      "severity": 5,
      "risk_level": "Critical",
      "title": "No encryption-at-rest for PHI (HIPAA §164.312(a)(2)(iv))",
      "description": "Patient lab results are stored with no stated encryption-at-rest control.",
      "recommendation": "Encrypt the data store at rest (e.g. column-level or full-disk encryption with managed key rotation) and document the control for audit.",
      "is_blocking": true
    },
    {
      "control_id": "SEC-002",
      "severity": 4,
      "risk_level": "High",
      "title": "Shared service account provides no per-user accountability",
      "description": "Partner clinics access the API via one shared account, defeating HIPAA's minimum-necessary and audit requirements.",
      "recommendation": "Issue per-clinic or per-user credentials with RBAC and individual audit logging.",
      "is_blocking": true
    }
  ],
  "compliance_flags": ["HIPAA", "GDPR"],
  "source_type": "text"
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` (GOVERN/MAP/MEASURE/MANAGE) · `OWASP-LLM` (LLM01–LLM10) · `HIPAA` · `GDPR` · `ISO27001` · `GxP` · `PCI-DSS`. Findings use `SEC-xxx`, `AI-xxx`, `GOV-xxx` control IDs from the shared control library.
