# GxP Validation Plan

Points at an application (files, a code snippet, or a GitHub PR/file) and plans its GxP validation document package: a GAMP 5 risk classification, which of the standard documents actually apply (URS, FRS, Risk Assessment, Validation Plan, IQ/OQ/PQ Protocols, Traceability Matrix, Validation Summary Report, SOP), and an application-specific outline for each, in the correct generation order.

**Module:** Security Intelligence
**Endpoint:** `POST /api/v1/agents/gxp-generate-plan`
**Agent name (for scopes/BYOL config):** `gxp-generate-plan`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `files` | array of `{path, content}` | one of files/code/github_pr_url/github_file_url, **recommended** | The application's files — the primary way to use this agent |
| `code` | string | " | Single code snippet, if not passing `files` |
| `github_pr_url` | string | " | GitHub PR URL |
| `github_file_url` | string | " | GitHub file URL |
| `system_description` | string | no | What this application does, e.g. `"Lab data management system (LIMS) with approval workflow"` |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `files` (recommended) · `code` · `github_pr_url` · `github_file_url`. `files` takes priority over the standard resolver.

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `gxp_classification` | `GxP-Critical` \| `GxP-Supporting` \| `Not GxP-Relevant` — assessed honestly; not every application is GxP-critical |
| `classification_rationale` | Why |
| `system_summary` | What the application does and its GxP-relevant characteristics (creates/modifies e-records, handles signatures, needs an audit trail) |
| `applicable_documents[]` | Every one of the 10 standard document types assessed, each: `document_type` (`User Requirement Specification`, `Functional/Design Specification`, `Risk Assessment`, `Validation Plan`, `IQ Protocol`, `OQ Protocol`, `PQ Protocol`, `Traceability Matrix`, `Validation Summary Report`, `Standard Operating Procedure`), `applicable` (bool), `why`, `outline[]` (section headings specific to this application, not a generic template) |
| `recommended_generation_order[]` | Applicable `document_type`s in dependency order (URS before FRS before OQ, etc.) — feed each into [`gxp-generate-document`](gxp-generate-document.md) in this order |
| `summary` | — |
| `llm_used`, `created_at` | Standard |

**Note:** `_budget_stats` is **not present in this response** — budget is still deducted server-side.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars) — applies to all `files` concatenated together
- **Timeout & retry** — 900s timeout, up to 2 retries (raised `max_tokens=16000` output budget)
- **Prompt-injection scan** — non-blocking heuristic (not echoed in this endpoint's explicit response fields)
- **Untrusted content wrapping** — a fetched PR/file is wrapped before analysis

## Cost control

- **RBAC** — this key's `scopes` must include `gxp-generate-plan` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- No `_budget_stats` field in the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/gxp-generate-plan \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "github_repo_url": "https://github.com/acme/lims-core",
    "system_description": "Lab data management system tracking clinical test results with a supervisor approval workflow before results are released",
    "project_name": "LIMS Core"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "gxp-generate-plan",
  "gxp_classification": "GxP-Critical",
  "classification_rationale": "The system creates and releases clinical test results under a supervisor approval workflow — directly affects data integrity for a regulated process.",
  "applicable_documents": [
    {
      "document_type": "User Requirement Specification",
      "applicable": true,
      "why": "Foundational requirements document every later validation document traces back to.",
      "outline": ["REQ-001: Result entry and operator identification", "REQ-002: Supervisor approval workflow", "REQ-003: Audit trail for result changes"]
    },
    {"document_type": "PQ Protocol", "applicable": true, "why": "System must be validated under real business-process conditions given its GxP-Critical classification.", "outline": ["..."]}
  ],
  "recommended_generation_order": ["User Requirement Specification", "Functional/Design Specification", "Risk Assessment", "Validation Plan", "OQ Protocol", "PQ Protocol", "Traceability Matrix", "Validation Summary Report"]
}
```

## Frameworks / controls referenced

`21CFR11` · `GxP` (GAMP 5 risk-based validation). Uses the shared `GXP-001`–`GXP-005` control library as reference for what each document type must satisfy.
