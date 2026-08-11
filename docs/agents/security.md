# Security Architecture Review (STRIDE)

Security architecture review: a full STRIDE threat matrix per major component, a trust-boundary map (from/to zones, data crossing, controls present, gaps), attack-surface enumeration (entry points, auth surfaces, admin interfaces, data egress, third-party integrations), and compliance-architecture flags for HIPAA/GDPR/SOC2/NIST AI RMF/GxP.

**Module:** Security Intelligence
**Endpoint:** `POST /api/v1/agents/security`
**Agent name (for scopes/BYOL config):** `security`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `artifact` | string | one of artifact/github_repo_url/github_file_url/jira_ticket | Architecture description, design doc, or code to review |
| `github_repo_url` | string | " | GitHub repository URL |
| `github_file_url` | string | " | GitHub file URL |
| `jira_ticket` | string | " | Jira ticket ID or URL |
| `system_type` | string | no | System type hint, e.g. `"Healthcare API with PHI"`, `"Payment processing microservice"`, `"LLM-powered chatbot"` |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `artifact` · `github_repo_url` · `github_file_url` · `jira_ticket`. Resolution priority: `github_file_url` > `github_repo_url` > `jira_ticket` > `artifact`/`text`.

## Response

Custom envelope (via `_wrap`, extended with STRIDE-specific fields):

| Field | Description |
|---|---|
| `findings[]` | `control_id` (`SEC-ARCH-001`–`005`, `SEC-001`–`SEC-007`, `GOV-001`–`GOV-008`), `severity`, `risk_level`, `title`, `description`, `recommendation`, `is_blocking` |
| `stride_threats[]` | Each: `threat_type` (`Spoofing`\|`Tampering`\|`Repudiation`\|`Information Disclosure`\|`Denial of Service`\|`Elevation of Privilege`), `component`, `threat_description`, `mitigation`, `risk_level` |
| `trust_boundaries[]` | Each: `boundary`, `from_zone`, `to_zone`, `data_crossing`, `controls_present[]`, `gaps[]` |
| `attack_vectors[]` | Enumerated attack vectors (strings) |
| `stride_count`, `boundary_count` | Counts (added by the orchestrator, alongside `critical_count`/`high_count`) |
| `gate_status`, `risk_score`, `summary`, `recommendations[]`, `compliance_flags[]` | Standard fields |
| `_budget_stats` | Top level |

Gate logic: **Red** — critical architectural flaws (missing auth, broken encryption, no trust boundaries). **Amber** — significant gaps needing remediation before production. **Green** — minor observations only.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 900s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; flags surfaced as `guardrail_flags`
- **Untrusted content wrapping** — fetched repo/file/Jira content is wrapped before analysis

## Cost control

- **RBAC** — this key's `scopes` must include `security` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` appears at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/security \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "artifact": "A public API gateway forwards requests to an internal claims-processing service. The gateway validates JWTs; the internal service trusts any request from the gateway'\''s IP with no further check.",
    "system_type": "Healthcare claims processing microservice",
    "project_name": "Claims API"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "security",
  "gate_status": "Amber",
  "risk_score": 64,
  "stride_threats": [
    {
      "threat_type": "Spoofing",
      "component": "Internal claims-processing service",
      "threat_description": "Any host that can reach the internal network on the gateway's IP range can impersonate the gateway and submit unauthenticated claims requests.",
      "mitigation": "Require a signed internal service token (e.g. mTLS or a short-lived JWT re-issued by the gateway) validated by the internal service itself, not just network-level trust.",
      "risk_level": "High"
    }
  ],
  "trust_boundaries": [
    {
      "boundary": "Gateway -> Internal claims service",
      "from_zone": "DMZ/Gateway",
      "to_zone": "Internal trusted network",
      "data_crossing": "Claim payloads including PHI",
      "controls_present": ["Network segmentation"],
      "gaps": ["No service-to-service authentication", "No re-validation of caller identity at the internal service"]
    }
  ],
  "compliance_flags": ["HIPAA", "SOC2"]
}
```

## Frameworks / controls referenced

`STRIDE` · `NIST-AI-RMF` · `HIPAA` · `GDPR` · `SOC2`. Control IDs: `SEC-ARCH-001`–`005` (defense in depth, threat modeling, attack surface, third-party integration, secrets management), `SEC-001`–`SEC-007`, `GOV-001`–`GOV-008`.
