# Sensitive Data Detector

Scans code, configuration, API schemas, data models, logs, and documentation for exposed or mishandled PII, PHI, PCI data, credentials/secrets, and location data. Every finding includes an obfuscated sample (never the real value), a location reference, and the specific regulation clause it violates.

**Module:** Security Intelligence
**Endpoint:** `POST /api/v1/agents/sensitive-data`
**Agent name (for scopes/BYOL config):** `sensitive-data`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `content` | string | one of content/github_file_url | Code, configuration file, database schema, API response, or log sample to scan |
| `github_file_url` | string | " | GitHub file URL — fetches and scans the file automatically |
| `project_name`, `context` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

Note: this request model does **not** accept `post_back` (or rather, it is accepted on the shared base model but ignored — see Guardrails: this agent never posts back).

**Sources accepted:** `content` · `github_file_url`. Resolution priority: `github_file_url` > `content`/`text`.

## Response

Custom envelope (via `_wrap`, extended with sensitive-data-specific counts):

| Field | Description |
|---|---|
| `findings[]` | Each: `control_id`, `severity` (1–5), `risk_level`, `category` (`PII`\|`PHI`\|`PCI`\|`Credential`\|`Location`\|`Other`), `title`, `description`, `location` (file/function/line reference), `sample` (obfuscated — real data replaced with `[REDACTED]`), `recommendation`, `regulation_refs[]` (e.g. `HIPAA §164.312`, `GDPR Art.9`, `PCI DSS Req.3`), `is_blocking` |
| `pii_count`, `phi_count`, `pci_count`, `credential_count` | Per-category counts from the tool schema |
| `total_findings`, `critical_count`, `high_count` | Added by the orchestrator |
| `gate_status`, `risk_score`, `summary`, `recommendations[]`, `compliance_flags[]` | Standard fields |
| `_budget_stats` | Top level |

`post_back_status` is always `"not_requested"` for this agent — its own output contains obfuscated sensitive-data samples, so it is intentionally never posted back to a public PR/issue/Jira comment.

Severity guide: 5 (Critical) — real sensitive data exposed, credentials in code, unprotected PHI/PAN; 4 (High) — sensitive data logged or sent over an insecure channel; 3 (Moderate) — test data resembling real PII; 2 (Minor) — weak anonymization; 1 (Low) — configuration that could lead to future exposure.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 900s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; flags surfaced as `guardrail_flags`
- **Untrusted content wrapping** — a fetched file is wrapped before analysis
- **No post-back, ever** — unlike every other source-based agent, this endpoint never posts its result to GitHub/Jira, because the finding samples (even obfuscated) are sensitive by nature

## Cost control

- **RBAC** — this key's `scopes` must include `sensitive-data` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` appears at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/sensitive-data \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "content": "logger.info(f\"Processing payment for card {card_number}, cvv={cvv}, user_ssn={ssn}\")",
    "project_name": "Payments Service"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "sensitive-data",
  "gate_status": "Red",
  "risk_score": 96,
  "pii_count": 1,
  "phi_count": 0,
  "pci_count": 2,
  "credential_count": 0,
  "findings": [
    {
      "control_id": "SEC-004",
      "severity": 5,
      "risk_level": "Critical",
      "category": "PCI",
      "title": "CVV logged in plaintext",
      "description": "The CVV is written to application logs, an absolute prohibition under PCI DSS.",
      "location": "logger.info() call, payments module",
      "sample": "cvv=[REDACTED]",
      "recommendation": "Remove cvv from the log statement entirely — it must never be persisted after authorization.",
      "regulation_refs": ["PCI DSS Req.3.2.1"],
      "is_blocking": true
    }
  ],
  "post_back_status": "not_requested"
}
```

## Frameworks / controls referenced

`HIPAA` · `GDPR` · `PCI-DSS` · `NIST-AI-RMF`. Findings draw from the shared `SEC-xxx`/`GOV-xxx` control library plus explicit regulation citations (HIPAA §, GDPR Art., PCI DSS Req.) per finding.
