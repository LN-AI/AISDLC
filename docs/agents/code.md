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

- **Chunking** — an artifact too large for one call is split automatically, at diff-hunk and declaration boundaries (never mid-hunk), with overlap so nothing falls between the cuts. Chunks are reviewed in parallel and their findings merged and de-duplicated. Each chunk is told which files exist elsewhere in the same change and instructed not to assert anything about code it wasn't shown — a control that lives in another file is not a missing control. `MAX_AGENT_INPUT_CHARS` still applies per chunk, so it is no longer a ceiling on artifact size
- **Per-chunk failure isolation** — one chunk failing does not fail the review; `coverage` reports how many ran and the gate clamps accordingly. Every chunk failing is an error, not a clean report over nothing
- **Evidence verification** — each finding must quote the offending code verbatim. That quote is checked against the source in Python: a quote that isn't there means the finding is discarded as unfounded, and a quote that is there but at a different line has its line number corrected. Discarded findings are returned as `discarded_findings` rather than hidden
- **Baseline & waivers** — findings are fingerprinted (excluding line number, so they survive code moving) and compared against what the project has already seen. Results carry `new_findings_count` / `pre_existing_count` / `waived_count`. Waive one with `PATCH /api/v1/findings/{id}`; waived findings drop out of future reviews for that project and stop blocking. Pre-existing but un-waived findings still count toward the gate — otherwise a codebase with a hundred existing Criticals would report Green
- **Path suppression** — findings in test, fixture, vendored, generated, minified or build-output paths are separated out and cannot block
- **Timeout & retry** — `LLM_TIMEOUT_SECONDS` (360s by default), up to `LLM_MAX_RETRIES` (2) retries on transient provider errors
- **Prompt-injection scan** — non-blocking; matches surfaced as `guardrail_flags`
- **Untrusted content wrapping** — any content fetched from GitHub or Jira is delimited with `<<<UNTRUSTED_..._START/END>>>` markers before being sent to the model, so injected instructions inside it (e.g. in a PR comment or commit message) are treated as data, never followed. Applied centrally in the v1 source resolver, so it covers this agent whether the diff arrives via `github_pr_url` or via the PR workflows
- **Deterministic gate** — `gate_status` is never taken from the model alone. It is reconciled against the arithmetic over the returned findings and the result is the stricter of the two, so a response that reports a Critical finding cannot also report Green. The model's own answer is preserved as `model_gate_status`. `risk_score` is floored to match the gate (Red ≥ 70, Amber ≥ 40)
- **Coverage clamp** — if part of the artifact was never reviewed (e.g. an oversized PR diff cut at 50,000 characters), the result cannot be Green: it degrades to at least Amber, sets `coverage_complete: false`, and adds an explicit `GOV-COVERAGE` finding saying what was skipped
- **Empty-result guard** — a response with no findings *and* no summary on a non-trivial artifact is rejected rather than stored, because that shape indicates a truncated or malformed model response, not clean code
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
