# VAPT — Vulnerability Assessment

Full penetration-testing-style assessment: OWASP Web Top 10 (2021), OWASP API Top 10 (2023), XSS/CSRF, business logic flaws, file upload issues, information disclosure, TLS/security-header weaknesses, secrets in code, and AI/LLM-specific risks (tool misuse, RAG poisoning, output manipulation) — each finding carries a CVSS v3.1 score, a realistic attack scenario, and a proof-of-concept.

**Module:** Security Intelligence
**Endpoint:** `POST /api/v1/agents/vapt`
**Agent name (for scopes/BYOL config):** `vapt`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | one of code/github_pr_url/github_file_url/github_repo_url | Code, config, or infrastructure-as-code to assess |
| `github_pr_url` | string | " | GitHub PR URL |
| `github_file_url` | string | " | GitHub file URL |
| `github_repo_url` | string | " | GitHub repository URL for a full-repo assessment |
| `scope` | string | no | Scope notes for the assessor, e.g. `"API endpoints only, skip frontend bundle"` |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `code` · `github_pr_url` · `github_file_url` · `github_repo_url`. Resolution priority: `github_pr_url` > `github_file_url` > `github_repo_url` > `code`/`text`.

## Response

Custom envelope (via `_wrap`, extended with VAPT-specific fields):

| Field | Description |
|---|---|
| `findings[]` | Each: `vuln_id` (e.g. `VLN-001`), `control_id` (`VAPT-001`–`VAPT-020`, or `SEC-xxx`), `severity` (1–5), `risk_level`, `cvss_score` (0.0–10.0), `cvss_vector` (e.g. `CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H`), `owasp_category`, `cwe_id`, `title`, `description`, `affected_component`, `attack_scenario`, `proof_of_concept`, `recommendation`, `is_blocking` |
| `attack_surface_summary` | Prose summary of identified entry points |
| `gate_status`, `risk_score`, `summary`, `recommendations[]`, `compliance_flags[]` | Standard fields |
| `_budget_stats` | Top level |

Gate logic: **Red** — any Critical or multiple High findings. **Amber** — High findings needing approval, or Moderate findings without a workaround. **Green** — only Low/Informational findings.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 900s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; flags surfaced as `guardrail_flags`. This agent is deliberately never blocked from analyzing malicious-looking code — that's the point of a VAPT scan
- **Untrusted content wrapping** — fetched PR/file/repo content is wrapped before analysis

## Cost control

- **RBAC** — this key's `scopes` must include `vapt` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` appears at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/vapt \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "github_repo_url": "https://github.com/acme/patient-portal",
    "scope": "Focus on the public-facing auth and payment API endpoints",
    "project_name": "Patient Portal API"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "vapt",
  "gate_status": "Red",
  "risk_score": 91,
  "attack_surface_summary": "3 public API entry points found: /auth/login, /auth/reset, /payments/charge — all unauthenticated at the network layer.",
  "findings": [
    {
      "vuln_id": "VLN-001",
      "control_id": "VAPT-012",
      "severity": 5,
      "risk_level": "Critical",
      "cvss_score": 9.1,
      "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
      "owasp_category": "API2:2023 Broken Authentication",
      "cwe_id": "CWE-306",
      "title": "Payment charge endpoint missing authentication",
      "affected_component": "POST /payments/charge",
      "attack_scenario": "An unauthenticated attacker can POST directly to /payments/charge with an arbitrary account ID and amount.",
      "proof_of_concept": "curl -X POST https://api.acme.com/payments/charge -d '{\"account_id\":\"123\",\"amount\":1}'",
      "recommendation": "Require a validated session/JWT and verify the caller owns account_id before processing a charge.",
      "is_blocking": true
    }
  ],
  "compliance_flags": ["PCI-DSS"]
}
```

## Frameworks / controls referenced

`OWASP-Web-Top10` (A01–A10, 2021) · `OWASP-API-Top10` (API1–API10, 2023) · `CWE` · `CVSS-3.1`. Control IDs: `VAPT-001`–`VAPT-020` (mapped from OWASP Web/API categories), plus `SEC-001`/`SEC-003`/`SEC-004` for auth/secrets/logging findings.
