# Issue Triaging Agent

Classifies, prioritizes, and recommends assignment for a defect, incident, support ticket, security finding, or test failure. Detects duplicates against this project's own prior triage history, enriches incomplete tickets without inventing facts, distinguishes confirmed facts from probable/possible causes, and flags mandatory human review for Critical/P0/P1/security cases per a confidence-based operating model.

**Module:** Operations Intelligence
**Endpoint:** `POST /api/v1/agents/issue-triage`
**Agent name (for scopes/BYOL config):** `issue-triage`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `title` | string | one of title+description/github_issue_url | Ticket/issue title |
| `description` | string | " | Full issue description / reporter's account |
| `github_issue_url` | string | " | GitHub Issue URL — fetched as the issue content **only if `title`/`description` are both empty** |
| `logs` | string | no | Relevant application/server logs |
| `stack_trace` | string | no | Stack trace, if any |
| `error_code` | string | no | Error code or exception type |
| `environment` | string | no | `Production` \| `Staging` \| `QA` \| `Development` |
| `application` | string | no | Affected application/service name |
| `module` | string | no | Affected module/component |
| `reporter` | string | no | Who reported this |
| `build_version` | string | no | Build/release version observed |
| `test_case_ref` | string | no | Related test case or requirement ID |
| `customer_impact_notes` | string | no | Users affected, business/revenue impact, SLA commitments |
| `team_roster` | string | no | Ownership mapping (e.g. a pasted CODEOWNERS file, or `"auth-service: @team-identity, billing: @team-payments"`). Without this, ownership is flagged as insufficient data rather than guessed |
| `project_name`, `context` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `title`+`description` (recommended, and takes priority) · `github_issue_url` (fallback only when both `title` and `description` are blank). Unlike other agents, this one does **not** use the standard `github_pr_url > github_file_url > ... > text` resolver — it's a direct `if title/description else github_issue_url` check with its own HTTP 400 if neither is present.

## Response

Custom envelope — the report is nested under `triage_report`:

```json
{
  "success": true,
  "agent": "issue-triage",
  "review_id": "...",
  "project_name": "...",
  "source_type": "text",
  "triage_report": { "...": "see below" },
  "llm_used": {...},
  "created_at": "..."
}
```

`triage_report` contains:

| Field | Description |
|---|---|
| `issue_summary` | Clear restatement, enriched if the original was vague — never inventing facts |
| `issue_category` | One of 16 enum values, e.g. `Functional defect`, `Security vulnerability`, `False positive`, `Duplicate issue` |
| `sdlc_area` | `Requirement`\|`Design`\|`Development`\|`Testing`\|`Deployment`\|`Production Operations`\|`Security`\|`Compliance` |
| `severity` | `Critical`\|`High`\|`Medium`\|`Low` — technical impact, independent of business priority |
| `priority` | `P0 - Immediate`\|`P1 - Urgent`\|`P2 - Normal`\|`P3 - Low priority` — business urgency |
| `severity_priority_reasoning` | Which specific factors actually drove the classification |
| `security_compliance_impact` | `{has_impact, details, regulatory_frameworks[]}` — only `true` for a genuine vulnerability/PII-PHI-credential exposure/access-control bypass/named regulation |
| `duplicate_analysis` | `{status, matched_review_id, matched_summary, similarity_explanation}` — status is one of `None found`, `Exact duplicate`, `Probable duplicate`, `Related issue`, `Recurring issue`, `Possible regression of a previously fixed issue` |
| `related_issues[]` | Each: `review_id`, `summary`, `relationship` |
| `recommended_assignment` | `{recommended_team, recommended_owner, assignment_confidence (0-100), reasoning, alternative_owner}` — `"Insufficient ownership data"` when no roster/signal is available, rather than a guessed team name |
| `enrichment` | `{reproduction_steps[], expected_behavior, actual_behavior, acceptance_criteria[], suggested_labels[], recommended_investigation_steps[], missing_information[]}` |
| `root_cause_analysis` | `{confirmed_facts[], probable_causes[], possible_causes[], required_validation_steps[]}` — never calls something a root cause outright |
| `release_regression` | `{first_affected_version, last_known_working_version, potential_regression, regression_reasoning, areas_requiring_regression_testing[]}` |
| `escalation` | `{required, reasons[], notify_channels[]}` — required when severity is Critical, priority is P0/P1, security/privacy data is involved, a regulatory risk is identified, or ownership confidence is very low |
| `confidence_score` | 0–100 |
| `suggested_next_actions[]`, `reasoning_summary` | — |
| `gate_status` | `Green`\|`Amber`\|`Red` — computed deterministically (see below), never left to the model's judgment |
| `risk_score` | Deterministic, from severity: `Critical`=95, `High`=70, `Medium`=40, `Low`=15 |
| `findings[]` | Synthetic findings the orchestrator adds on top of the LLM's report — one `TRIAGE-INFO` (severity 2) per missing-information item, one `TRIAGE-SEC` (severity 5, blocking) if `security_compliance_impact.has_impact`, one `TRIAGE-ESC` (severity 4, blocking) if `escalation.required` |
| `human_approval_required` | bool — mirrors whether `gate_status` is `Red` |
| `_budget_stats` | **Nested inside `triage_report`**, not at the top level |

**Deterministic human-in-the-loop gate** (never left to model judgment): `gate_status = Red` when severity is `Critical`, priority is `P0`/`P1`, `security_compliance_impact.has_impact` is true, `issue_category` is `Security vulnerability`, or `confidence_score < 70`. Otherwise `Amber` when `confidence_score < 90`, else `Green`.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 120s timeout, up to 2 retries (raised `max_tokens=6000` output budget)
- **Prompt-injection scan** — non-blocking; flags surface inside `triage_report.guardrail_flags`
- **Ground rule (prompt-level):** the agent never invents facts — any field it can't determine is marked `"Unavailable"` rather than guessed, and duplicate/ownership claims require concrete cited similarity, not a plausible-sounding guess

## Cost control

- **RBAC** — this key's `scopes` must include `issue-triage` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` is nested inside `triage_report`, not at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/issue-triage \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "title": "Login fails for all users in production",
    "description": "Since the 14:00 UTC deploy, every login attempt returns 500. No errors visible to end users beyond a generic failure page.",
    "logs": "2026-07-21T14:03:12 ERROR auth-service: KeyError: '\''JWT_SECRET'\'' at auth/token.py:41",
    "environment": "Production",
    "application": "auth-service",
    "build_version": "v2.14.0",
    "team_roster": "auth-service: @team-identity"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "issue-triage",
  "triage_report": {
    "issue_summary": "Production-wide login outage starting at the 14:00 UTC v2.14.0 deploy, caused by a missing JWT_SECRET environment variable.",
    "issue_category": "Configuration issue",
    "severity": "Critical",
    "priority": "P0 - Immediate",
    "security_compliance_impact": {"has_impact": false, "details": "Outage, not a data exposure.", "regulatory_frameworks": []},
    "root_cause_analysis": {"confirmed_facts": ["KeyError: 'JWT_SECRET' at auth/token.py:41 immediately following the v2.14.0 deploy"], "probable_causes": ["JWT_SECRET env var not set in the v2.14.0 deployment config"], "possible_causes": [], "required_validation_steps": ["Confirm JWT_SECRET is present in the production environment for the new deploy"]},
    "recommended_assignment": {"recommended_team": "team-identity", "recommended_owner": "team-identity", "assignment_confidence": 90, "reasoning": "Matched via provided team_roster for auth-service."},
    "escalation": {"required": true, "reasons": ["Critical severity", "P0 priority", "Production-wide outage"], "notify_channels": ["PagerDuty on-call", "Slack #incidents"]},
    "gate_status": "Red",
    "risk_score": 95,
    "human_approval_required": true,
    "findings": [
      {"control_id": "TRIAGE-ESC", "severity": 4, "risk_level": "High", "title": "Escalation required", "description": "Critical severity; P0 priority; Production-wide outage", "is_blocking": true}
    ]
  }
}
```

## Frameworks / controls referenced

`NIST-AI-RMF`. Not a control-mapped reviewer — `security_compliance_impact.regulatory_frameworks` cites named frameworks (HIPAA, GDPR, PCI-DSS, 21 CFR Part 11) only when the issue itself genuinely implicates them.
