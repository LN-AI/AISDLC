# Core SDLC Review

A composite endpoint: runs Requirement Review, Compliance Mapping, Architecture Review, Code Review and Prompt Safety Review as applicable in one call, aggregates every finding, runs the Release Gate agent over the combined set, and generates a governance report — one call instead of orchestrating five.

Three further stages — **Security Review (STRIDE)**, **VAPT** and the **Sensitive Data Scan** — run over artifacts you already supply, but only when you ask for them (`include_security` / `include_vapt` / `include_sensitive_data`). They are off by default because each adds an LLM call to what is billed as a single call.

> **The gate is only as broad as the stages that ran.** Whatever did not run is listed in the response as `steps_not_run`. A Green over five stages is not a Green over eight, and this endpoint will not let that difference stay invisible.

**Module:** Governance Intelligence
**Endpoint:** `POST /api/v1/agents/full`
**Agent name (for scopes/BYOL config):** `full` — unchanged; only the display title changed, so existing key scopes, per-agent BYOL config and cost history keep resolving.

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | at least one source field | Requirement or code artifact — used as a fallback when neither `github_pr_url` nor `jira_ticket` is given. When supplied, it is analyzed as **both** requirement and code |
| `github_pr_url` | string | " | GitHub PR URL — its diff is reviewed as `code`. It does **not** by itself run VAPT; set `include_vapt` for that |
| `jira_ticket` | string | " | Jira ticket — runs Requirement Review against the issue content |
| `compliance_artifact` | string | no | Artifact to map to compliance frameworks — runs the Compliance Mapper *in addition to* whichever of the above triggered |
| `architecture` | string | no | Architecture/system description — runs a real Architecture Review (quality scoring, anti-patterns, SPOFs) alongside the others |
| `prompt_config` | object | no | Keys: `system_prompt`, `user_prompt`, `tool_definitions`, `rag_config` — runs Prompt Safety Review |
| `test_evidence` | string | no | Test results or sign-off evidence, fed into the Release Gate step |
| `include_security` | bool | no | Also run the STRIDE Security Review. Reads `architecture`; a no-op without it. Adds one LLM call |
| `include_vapt` | bool | no | Also run the VAPT assessment (CVSS, OWASP API Top 10). Reads the code; a no-op without it. Adds one LLM call |
| `include_sensitive_data` | bool | no | Also scan the code for PII/PHI/PCI and credentials. Reads the code; a no-op without it. Adds one LLM call |

### Stage order

`Requirement → Compliance → Architecture → Security (STRIDE) → Code → VAPT → Sensitive Data → Prompt Safety → Release Gate → Report`

Each opt-in stage sits directly after the stage whose artifact it reads. Requesting one without its artifact is silently skipped rather than rejected, and named in `steps_not_run`.

### Cost and quota

A default run is up to 7 LLM calls; with all three stages on it is up to 10. All of it is metered as **one** call of the `full` product, so per-call quota (`plan_products.quota_calls_per_month`) is unaffected by how many stages you enable — but the underlying token cost, and any USD budget, is not. Budget is reserved from the actual step count for the request.

The workflow is all-or-nothing: if any stage raises, the whole run aborts and nothing is persisted. A longer plan is a wider window for that to happen.
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

At least one of `text`, `github_pr_url`, `jira_ticket`, `compliance_artifact`, `architecture`, `prompt_config` is required, or the call returns HTTP 400.

**Sources accepted:** `text` · `github_pr_url` · `jira_ticket` · `compliance_artifact` · `architecture`. Unlike other endpoints, `full` can trigger **multiple** sub-agents from one request — the trigger table:

| Input given | Sub-agents run |
|---|---|
| `github_pr_url` | Code Review |
| `jira_ticket` | Requirement Review |
| `text` (no PR/Jira) | Requirement Review + Code Review |
| `compliance_artifact` | Compliance Mapper (always, additively) |
| `architecture` | Architecture Review (always, additively) |
| `prompt_config` | Prompt Safety Review (always, additively) |

## Response

Custom envelope (does not use `_wrap`) — this is a synthesized cross-agent result, not one sub-agent's raw output:

| Field | Description |
|---|---|
| `review_id`, `report_id` | The full review's ID, and the generated governance report's ID (feed `report_id` results into other tooling, or re-fetch via the reports API) |
| `gate_status` | Consolidated gate decision from the Release Gate agent evaluating all sub-review findings together |
| `overall_risk_score` | Weighted: `max(sub-scores) × 0.5 + average(sub-scores) × 0.5` |
| `total_findings`, `critical_count`, `high_count` | Across all sub-reviews combined |
| `sub_reviews[]` | One entry per sub-agent run: `source` (`requirement`\|`code`\|`compliance`\|`architecture`\|`prompt`\|`release_gate`), `agent`, `gate`, `score`, `findings` (count) |
| `summary` | The Release Gate agent's executive summary |
| `top_findings[]` | Top 10 findings across all sub-reviews, sorted by severity |
| `recommendations[]` | From the Release Gate step |
| `post_back_status`, `source_type`, `source_ref` | Only meaningful when `github_pr_url` or `jira_ticket` was the trigger |
| `_budget_stats` | Top level — reflects the **combined** token usage of every sub-agent call made during this one request |

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars), enforced independently on each sub-agent's own artifact
- **Timeout & retry** — 120s timeout per underlying LLM call (there may be several — one per sub-agent triggered), up to 2 retries each
- **Prompt-injection scan** — non-blocking per sub-agent call; any `guardrail_flags` surface on that sub-review's own result within the composite response
- **Untrusted content wrapping** — a fetched PR diff or Jira issue body is wrapped before analysis
- Because this endpoint can invoke up to five separate LLM calls per request, it typically has the highest latency and cost of any single endpoint — factor that into budget caps and timeouts for automation that calls it

## Cost control

- **RBAC** — this key's `scopes` must include `full` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month (checked once before the whole composite call begins, not per sub-agent)
- `_budget_stats` appears at the top level, summed across every sub-agent call this request made

## Example

```bash
curl -X POST https://your-host/api/v1/agents/full \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "github_pr_url": "https://github.com/acme/patient-portal/pull/88",
    "compliance_artifact": "This PR adds a new endpoint that exports patient visit summaries as PDF.",
    "project_name": "Patient Portal API",
    "post_back": true
  }'
```

Abbreviated response:

```json
{
  "review_id": "3fbb...",
  "report_id": "9a21...",
  "gate_status": "Red",
  "overall_risk_score": 79,
  "total_findings": 9,
  "sub_reviews": [
    {"source": "code", "agent": "Code Security Review Agent", "gate": "Red", "score": 88, "findings": 3},
    {"source": "compliance", "agent": "Compliance Mapper Agent", "gate": "Amber", "score": 61, "findings": 4},
    {"source": "release_gate", "agent": "Release Gate Agent", "gate": "Red", "score": 79, "findings": 0}
  ],
  "summary": "Blocking SQL injection in the new export endpoint plus missing HIPAA audit-trail controls for PHI export.",
  "post_back_status": "posted"
}
```

## Frameworks / controls referenced

`NIST-AI-RMF` · `OWASP-LLM` · `HIPAA` · `GDPR` · `ISO27001` — the union of whichever sub-agents ran. See each sub-agent's own doc ([code](code.md), [compliance](compliance.md), [architecture](architecture.md), [prompt](prompt.md), [requirement](requirement.md), [release-gate](release-gate.md)) for its specific control IDs.
