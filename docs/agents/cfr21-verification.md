# 21 CFR Part 11 Verification

Verifies a codebase (multiple files, or a single snippet/PR/file) against FDA 21 CFR Part 11 across four assessments — audit trail (§11.10(e)), electronic signatures (§11.50/70/100/200), access control (§11.10(d)/(g)/§11.300), and data integrity (ALCOA+) — with an exhaustive, evidence-cited findings list. Process-only controls (training, written policy, documentation control — §11.10(i)/(j)/(k)) are reported separately as validation-readiness notes; the agent never claims a code-only pass/fail on those.

**Module:** Security Intelligence
**Endpoint:** `POST /api/v1/agents/cfr21-verification`
**Agent name (for scopes/BYOL config):** `cfr21-verification`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `files` | array of `{path, content}` | one of files/code/github_pr_url/github_file_url, **recommended** | Multiple files for a real multi-file scan — the primary way to use this agent, since audit trails and signature workflows are usually cross-cutting concerns a single file rarely fully reveals |
| `code` | string | " | Single code snippet, if not passing `files` |
| `github_pr_url` | string | " | GitHub PR URL |
| `github_file_url` | string | " | GitHub file URL |
| `system_description` | string | no | What this system does — calibrates applicability (e.g. e-signature checks are skipped if there's no approval workflow) |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `files` (recommended) · `code` · `github_pr_url` · `github_file_url`. When `files` is supplied, it takes priority and the standard single-artifact resolver is skipped entirely.

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `overall_gate_status`, `overall_risk_score` | `Green`\|`Amber`\|`Red`, 0–100 |
| `total_findings`, `critical_count`, `high_count` | — |
| `findings[]` | Each: `control_id`, `cfr_section` (e.g. `§11.10(e)`, `§11.50`), `title`, `severity` (1–5), `risk_level`, `description`, `evidence` (file/function/pattern — never a bare claim), `recommendation`, `is_blocking` |
| `audit_trail_assessment` | `{compliant, findings_summary, gaps[], strengths[]}` — §11.10(e) |
| `esignature_assessment` | `{applicable, compliant, findings_summary, gaps[], strengths[]}` — `applicable: false` when there's no signing/approval workflow at all |
| `access_control_assessment` | `{compliant, findings_summary, gaps[], strengths[]}` — §11.10(d)/(g)/§11.300 |
| `data_integrity_assessment` | `{compliant, alcoa_plus_notes, gaps[], strengths[]}` — ALCOA+ (Attributable, Legible, Contemporaneous, Original, Accurate, +Complete, Consistent, Enduring, Available) |
| `validation_readiness_notes` | Process-only items (IQ/OQ/PQ validation, training, written policy, documentation control) — flagged as needing documentation evidence, never claimed pass/fail from code alone |
| `files_scanned[]` | File paths actually considered |
| `compliance_flags[]`, `recommendations[]`, `summary` | Standard fields |
| `llm_used`, `created_at` | Standard |

**Note:** `_budget_stats` is **not present in this response** — the handler builds an explicit field list without it. Budget is still deducted normally.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars) — applies to the combined text of all `files` concatenated together, so a large multi-file scan can hit this limit faster than a single-file call
- **Timeout & retry** — 900s timeout, up to 2 retries (raised `max_tokens=16000` output budget for an exhaustive findings list)
- **Prompt-injection scan** — non-blocking heuristic scan (not echoed in this endpoint's explicit response fields)
- **Untrusted content wrapping** — a fetched PR/file is wrapped before analysis

## Cost control

- **RBAC** — this key's `scopes` must include `cfr21-verification` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- No `_budget_stats` field in the response (see Response, above)

## Example

```bash
curl -X POST https://your-host/api/v1/agents/cfr21-verification \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "files": [
      {"path": "app/models/record.py", "content": "class ClinicalRecord(Base):\n    __tablename__ = \"records\"\n    id = Column(Integer, primary_key=True)\n    value = Column(String)\n    updated_at = Column(DateTime)"}
    ],
    "system_description": "Lab data management system (LIMS) storing clinical test results",
    "project_name": "LIMS Core"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "cfr21-verification",
  "overall_gate_status": "Red",
  "overall_risk_score": 87,
  "findings": [
    {
      "control_id": "GXP-003",
      "cfr_section": "§11.10(e)",
      "title": "No audit trail table — record updates overwrite prior values",
      "severity": 5,
      "risk_level": "Critical",
      "description": "ClinicalRecord.value is updated in place with only an updated_at timestamp; no prior value or actor is preserved.",
      "evidence": "app/models/record.py — ClinicalRecord model has no associated audit_log/history table",
      "recommendation": "Add an append-only audit_trail table capturing old value, new value, actor, and timestamp on every write to ClinicalRecord.",
      "is_blocking": true
    }
  ],
  "audit_trail_assessment": {"compliant": false, "gaps": ["No append-only history of record changes"], "strengths": []},
  "esignature_assessment": {"applicable": false, "compliant": true, "findings_summary": "No signing/approval workflow present in the files given."},
  "validation_readiness_notes": "IQ/OQ/PQ validation status and written SOP/training records were not provided — these process-level controls require separate documentation evidence."
}
```

## Frameworks / controls referenced

`21CFR11` · `GxP`. Findings use the shared `GXP-001`–`GXP-005` control library plus specific `§11.xx` citations per finding.
