# Issue Triaging — GitHub Batch

Pulls open issues from a GitHub repo (most recently updated first, pull requests excluded) and runs the full Issue Triage pipeline on each one — classification, severity/priority, duplicate detection against this project's history, ownership recommendation, enrichment, and escalation flags. Returns one report per issue plus a severity/escalation summary for the whole batch.

**Module:** Operations Intelligence
**Endpoint:** `POST /api/v1/agents/issue-triage/batch-github`
**Agent name (for scopes/BYOL config):** `issue-triage-batch-github`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

Requires `GITHUB_TOKEN` configured on the platform (read access is sufficient — this endpoint only reads issues) — HTTP 503 if unset.

## Request

This agent uses `LLMOverrideFields` directly — there is no generic source resolver.

| Field | Type | Required | Description |
|---|---|---|---|
| `github_repo_url` | string | **yes** | GitHub repo URL, e.g. `https://github.com/acme/patient-portal` |
| `limit` | integer | no (default `10`, 1–25) | Max number of open issues to triage, most recently updated first |
| `project_name` | string | no | Stored in the audit trail |
| `context` | string | no | Free-text context injected into **every** issue's triage prompt |
| `team_roster` | string | no | Ownership mapping applied to every issue in this batch |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `github_repo_url` only.

## Response

Custom envelope — unlike the single-issue endpoint, the underlying result dict is spread directly at the **top level** of the response (`**result`), not nested:

| Field | Description |
|---|---|
| `success`, `agent`, `agent_title`, `project_name` | Standard |
| `repo` | Echoes `github_repo_url` |
| `issues_fetched` | Number of open issues fetched from GitHub (≤ `limit`) |
| `issues_triaged` | Number actually triaged (normally equal to `issues_fetched`) |
| `results[]` | One full triage report per issue — **the same shape as `triage_report` in [`issue-triage`](issue-triage.md)** (`issue_summary`, `issue_category`, `severity`, `priority`, `security_compliance_impact`, `duplicate_analysis`, `recommended_assignment`, `enrichment`, `root_cause_analysis`, `escalation`, `confidence_score`, `gate_status`, `risk_score`, `human_approval_required`, `findings[]`, etc.), plus two fields added for the batch context: `github_issue_number`, `github_issue_url` |
| `severity_breakdown` | `{Critical, High, Medium, Low}` — count of issues at each severity across the batch |
| `escalations_required` | Count of issues in `results[]` where `human_approval_required` is true |
| `llm_used`, `created_at` | Standard |
| `_budget_stats` | **At the top level** (via the `**result` spread) — reflects the combined token usage of triaging every issue in the batch, since all `limit` issues are triaged inside one `guarded_call`/`track_usage()` block |

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars), enforced independently per issue's content
- **Timeout & retry** — 900s timeout per issue's LLM call, up to 2 retries each — a batch of `limit=25` issues can therefore take substantially longer than a single triage call; size your client-side timeout accordingly
- **Prompt-injection scan** — non-blocking per issue; any `guardrail_flags` appear on that issue's own triage report within the batch response
- **Untrusted content wrapping** — each fetched issue body is wrapped before analysis, same as any other GitHub-sourced content
- GitHub API errors during the fetch (not LLM/guardrail errors) surface as HTTP 502 with the underlying error message

## Cost control

- **RBAC** — this key's `scopes` must include `issue-triage-batch-github` or be `all`, else HTTP 403
- **Budget** — checked once before the batch starts; HTTP 402 if already exhausted. Note the budget is **not** re-checked between individual issues within the batch, so a large `limit` on a key close to its cap can push usage past the nominal cap for that one call
- `_budget_stats` appears at the top level, summed across every issue triaged in this call

## Example

```bash
curl -X POST https://your-host/api/v1/agents/issue-triage/batch-github \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "github_repo_url": "https://github.com/acme/patient-portal",
    "limit": 15,
    "team_roster": "auth-service: @team-identity, billing: @team-payments"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "issue-triage-batch-github",
  "project_name": "",
  "repo": "https://github.com/acme/patient-portal",
  "issues_fetched": 15,
  "issues_triaged": 15,
  "severity_breakdown": {"Critical": 1, "High": 4, "Medium": 7, "Low": 3},
  "escalations_required": 5,
  "results": [
    {
      "github_issue_number": 412,
      "github_issue_url": "https://github.com/acme/patient-portal/issues/412",
      "issue_summary": "Password reset email never arrives for users on the new SMTP relay.",
      "severity": "High",
      "priority": "P1 - Urgent",
      "issue_category": "Integration or API issue",
      "gate_status": "Red",
      "human_approval_required": true
    }
  ],
  "_budget_stats": {"input_tokens": 18400, "output_tokens": 9200, "cost_usd": 0.31, "monthly_budget_usd": 50.0, "budget_used_usd": 12.77}
}
```

## Frameworks / controls referenced

`NIST-AI-RMF`. Same per-issue reporting shape as [`issue-triage`](issue-triage.md); see that doc for the full field-level breakdown of each issue's `triage_report`.
