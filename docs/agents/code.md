# Code & PR Security Reviewer

Security and quality review of source code, git diffs, and pull requests: injection flaws, auth/access control issues, hardcoded secrets, data exposure, weak cryptography, vulnerable dependencies, and AI/LLM-specific code risks (unsanitized input to LLMs, unvalidated LLM output, over-permissioned agent tools).

**Module:** Developer Intelligence
**Endpoint:** `POST /api/v1/agents/code`
**Agent name (for scopes/BYOL config):** `code`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access, issued from the dashboard's **API Keys** page
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | one of code/github_pr_url/github_file_url | Source code, git diff, or file content to review |
| `github_pr_url` | string | " | GitHub PR URL — fetches the PR diff automatically |
| `github_file_url` | string | " | GitHub file blob URL |
| `language` | string | no | Language hint, e.g. `python`, `typescript`, `java`, `go` — improves accuracy |
| `project_name` | string | no | Project name stored in the audit trail |
| `context` | string | no | Free-text context injected into the review prompt |
| `post_back` | boolean | no (default `false`) | Post the result as a comment on the originating PR/issue |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `code` · `github_pr_url` · `github_file_url`. Resolution priority: `github_pr_url` > `github_file_url` > raw `code`/`text` fields.

## Response

Standard envelope (`_wrap`), same shape as [`requirement`](requirement.md):

- `gate_status`, `risk_score` (0–100), `total_findings`, `critical_count`, `high_count`
- `findings[]` — `control_id` (`SEC-001`–`SEC-007`, `AI-001`, `AI-002`, `AI-007`, `AI-009`), `severity` (1=Low → 5=Critical), `risk_level`, `title`, `description`, `recommendation`, `is_blocking`, optional `owasp_ref` / `nist_ref`
- `summary`, `recommendations[]`, `compliance_flags[]`
- `post_back_status`, `source_type` (`text` \| `github_pr` \| `github_file`), `source_ref`
- `guardrail_flags` if the injection heuristic matched
- `_budget_stats` at the top level (see Cost control)

Gate logic: **Red** on any Critical finding (hardcoded secret, SQL injection, auth bypass, PHI exposure); **Amber** on High findings or multiple Moderates; **Green** for Minor/Low only.

## Guardrails applied

- **Input size limit** — rejects (HTTP 413) artifacts over `MAX_AGENT_INPUT_CHARS` (400,000 characters by default)
- **Timeout & retry** — 120s timeout, up to 2 retries on transient provider errors
- **Prompt-injection scan** — non-blocking; matches surfaced as `guardrail_flags`
- **Untrusted content wrapping** — a fetched PR diff is delimited with `<<<UNTRUSTED_GITHUB_PR_DIFF_START/END>>>` markers before being sent to the model, so injected instructions inside the diff (e.g. in a comment or commit message) are treated as data, never followed
- This agent is deliberately never blocked from analyzing "suspicious" code — a Code Review agent that refused to look at malicious-looking input would be useless for its actual job

## Cost control

- **RBAC** — this key's `scopes` must include `code` or be `all`, else HTTP 403
- **Budget** — rejected with HTTP 402 once the key's `monthly_budget_usd` cap is reached this month
- `_budget_stats` appears at the top level of every successful response when usage is tracked

## Example

```bash
curl -X POST https://your-host/api/v1/agents/code \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "code": "def get_user(request):\n    uid = request.GET[\"id\"]\n    q = f\"SELECT * FROM users WHERE id = {uid}\"\n    return db.execute(q)",
    "language": "python",
    "project_name": "Patient Portal API"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "code",
  "gate_status": "Red",
  "risk_score": 92,
  "critical_count": 1,
  "findings": [
    {
      "control_id": "SEC-003",
      "severity": 5,
      "risk_level": "Critical",
      "title": "SQL injection via unparameterized query",
      "description": "`uid` is taken directly from the query string and concatenated into a SQL statement with an f-string, allowing arbitrary SQL execution.",
      "recommendation": "Use a parameterized query, e.g. db.execute(\"SELECT * FROM users WHERE id = %s\", (uid,)).",
      "is_blocking": true,
      "owasp_ref": "A03:2021 — Injection"
    }
  ],
  "compliance_flags": ["OWASP-Web-Top10"],
  "source_type": "text"
}
```

## Frameworks / controls referenced

`OWASP-Web-Top10` · `OWASP-LLM` · `CWE` · `NIST-AI-RMF`. Control IDs: `SEC-001` (missing auth), `SEC-002` (missing RBAC), `SEC-003` (hardcoded secret), `SEC-004` (PII/PHI in logs), `SEC-005` (missing rate limiting), `SEC-006` (missing/weak crypto), `SEC-007` (vulnerable dependency), `AI-001` (unsanitized input to LLM), `AI-002` (unvalidated LLM output), `AI-007` (over-permissioned agent tool), `AI-009` (confidential data exposure via model output).
