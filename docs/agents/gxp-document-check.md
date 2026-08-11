# GxP Document Check

Checks a GxP document — or a related set of documents — for completeness against a type-specific required-elements checklist (SOP, Validation Protocol IQ/OQ/PQ, Validation Report, Batch Record, Deviation Report, CAPA, Change Control, Risk Assessment, URS/Specification, Training Record — auto-detected, not just trusted from a hint), signature/approval compliance (21 CFR 11.50), version control, and cross-document traceability.

**Module:** Security Intelligence
**Endpoint:** `POST /api/v1/agents/gxp-document-check`
**Agent name (for scopes/BYOL config):** `gxp-document-check`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `documents` | array of `{name, content}` | one of documents/content, **recommended for a set** | Multiple related documents checked together (e.g. URS + OQ protocol) — traceability is checked across them, which is much stronger evidence than any single document alone |
| `content` | string | " | Single document text, if not passing `documents` |
| `document_type_hint` | string | no | What kind of document this is, if known, e.g. `"SOP"`, `"OQ Protocol"`, `"Batch Record"` — the agent verifies rather than just trusting it |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `documents` (recommended for a set) · `content` (single document). When `documents` is supplied, it takes priority over the standard single-artifact resolver.

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `document_type` | Auto-detected: one of `SOP`, `Validation Protocol (IQ/OQ/PQ)`, `Validation Report`, `Batch Record`, `Deviation Report`, `CAPA`, `Change Control`, `Risk Assessment`, `User Requirement Specification`, `Functional/Design Specification`, `Training Record`, `Audit Report`, `Multi-Document Set` (when multiple docs given), or `Other/Unclear` |
| `completeness_score` | 0–100, % of expected elements present and adequate for this document type |
| `overall_gate_status`, `overall_risk_score` | `Green`\|`Amber`\|`Red`, 0–100 |
| `total_findings`, `critical_count`, `high_count` | — |
| `findings[]` | Each: `control_id`, `gxp_reference` (e.g. `21 CFR 11.50`, `GAMP 5 OQ requirements`), `title`, `severity`, `risk_level`, `description`, `evidence` (cited section/field), `recommendation`, `is_blocking` |
| `required_elements[]` | The full type-specific checklist, each: `element`, `present` (bool), `detail` |
| `signature_approval_check` | `{compliant, findings_summary, gaps[], strengths[]}` — printed name + date + meaning for every signoff; also flags approvals dated after the stated effective date |
| `version_control_check` | `{compliant, findings_summary, gaps[], strengths[]}` — document number, revision, effective date, revision history |
| `traceability_check` | `{compliant, findings_summary, gaps[], strengths[]}` — does this document reference what it should (e.g. OQ → URS); cross-document consistency when a set was given |
| `missing_sections[]` | — |
| `compliance_flags[]`, `recommendations[]`, `summary` | Standard fields |

**Note:** `_budget_stats` is **not present in this response** — budget is still deducted server-side.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars) — applies to all `documents` concatenated together
- **Timeout & retry** — 900s timeout, up to 2 retries (raised `max_tokens=16000` output budget)
- **Prompt-injection scan** — non-blocking heuristic (not echoed in this endpoint's explicit response fields)
- No GitHub/Jira fetch happens by default for this endpoint unless `content` resolves via the standard resolver's file/URL fields — when it does, fetched content is wrapped before analysis

## Cost control

- **RBAC** — this key's `scopes` must include `gxp-document-check` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- No `_budget_stats` field in the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/gxp-document-check \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {"name": "URS-014.docx", "content": "User Requirement Specification... REQ-001: The system shall record operator identity and timestamp for every result entry..."},
      {"name": "OQ-014.docx", "content": "Operational Qualification Protocol... Test OQ-001 verifies REQ-001... Approved by: J. Smith"}
    ],
    "project_name": "LIMS Validation Package"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "gxp-document-check",
  "document_type": "Multi-Document Set",
  "completeness_score": 72,
  "overall_gate_status": "Amber",
  "findings": [
    {
      "control_id": "GXP-002",
      "gxp_reference": "21 CFR 11.50",
      "title": "OQ approval signature missing date and meaning",
      "severity": 4,
      "risk_level": "High",
      "description": "'Approved by: J. Smith' has no date or stated role/meaning (e.g. QA Approver) attached.",
      "evidence": "OQ-014.docx signature block",
      "recommendation": "Add date and explicit role/meaning to every signature block.",
      "is_blocking": true
    }
  ],
  "traceability_check": {"compliant": true, "findings_summary": "OQ-001 correctly traces to REQ-001 in the URS.", "gaps": [], "strengths": ["Consistent requirement numbering across both documents"]}
}
```

## Frameworks / controls referenced

`21CFR11` · `GxP`. Uses the shared `GXP-001`–`GXP-005` control library plus specific citations like `21 CFR 11.50`, `GAMP 5`, `ICH Q7` per finding.
