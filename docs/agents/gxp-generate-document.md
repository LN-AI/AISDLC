# GxP Document Generation

Generates one complete GxP validation document (URS, FRS, Risk Assessment, Validation Plan, IQ/OQ/PQ Protocol, Traceability Matrix, Validation Summary Report, or SOP), grounded in the actual application given — real modules, endpoints, fields, and workflows, not a fill-in-the-blank template. Call once per document type, typically driven by the plan from [`gxp-generate-plan`](gxp-generate-plan.md). For OQ/PQ Protocols, can also emit real, runnable test scripts against the supplied application code.

**Module:** Security Intelligence
**Endpoint:** `POST /api/v1/agents/gxp-generate-document`
**Agent name (for scopes/BYOL config):** `gxp-generate-document`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `document_type` | string | **yes** | Which document to generate, e.g. `"User Requirement Specification"`, `"OQ Protocol"`, `"Risk Assessment"` — typically copied from the plan step's `applicable_documents[].document_type` |
| `files` | array of `{path, content}` | one of files/code/github_pr_url/github_file_url | The application's files |
| `code` | string | " | Single code snippet, if not passing `files` |
| `github_pr_url` | string | " | GitHub PR URL |
| `github_file_url` | string | " | GitHub file URL |
| `system_description` | string | no | What this application does |
| `outline` | string[] | no | Section outline for this document — typically copied from the plan step's `applicable_documents[].outline` |
| `plan_context` | string | no | Free-text context from the planning step (e.g. its `system_summary`), for cross-referencing other documents in the same package |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `files` (recommended) · `code` · `github_pr_url` · `github_file_url`. `files` takes priority over the standard resolver.

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `document_type`, `title` | Echoes the request, plus a generated title |
| `content` | The full document — real prose/tables/test steps for this application, not bracketed placeholders for content the agent could reasonably infer |
| `traceability_refs[]` | What this document derives from/maps to, e.g. `"Traces to URS-003, URS-007"` |
| `included_controls[]` | GxP/21 CFR controls addressed, e.g. `GXP-003`, `21 CFR 11.10(e)` |
| `test_cases[]` | **Only populated for `OQ Protocol`/`PQ Protocol` when application code was provided.** Each: `test_case_id` (matches an ID used in `content`), `objective`, `requirement_ref`, `automatable` (bool — false for anything needing human/physical/visual verification), `why_not_automatable` (required when `automatable` is false), `script_framework` (e.g. `pytest`, `Jest`, `JUnit 5`), `script_path`, `script_content` (a complete, runnable test script calling real application functions/endpoints — required when `automatable` is true) |
| `open_items[]` | Things genuinely requiring human input before this document can be finalized/signed — real names, real dates, live test execution, policy decisions. The agent does not pad this list with things it could have written itself |
| `summary` | — |
| `llm_used`, `created_at` | Standard |

**Note:** `_budget_stats` is **not present in this response** — budget is still deducted server-side.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 900s timeout, up to 2 retries; output budget raised to `max_tokens=28000` for OQ/PQ Protocol generation (which can include full test scripts), `max_tokens=24000` for every other document type
- **Prompt-injection scan** — non-blocking heuristic (not echoed in this endpoint's explicit response fields)
- **Untrusted content wrapping** — a fetched PR/file is wrapped before analysis
- A generated test script is only marked `automatable: true` when it genuinely exercises real application code — the agent is instructed never to pad `test_cases` with a hollow script just to fill the field, and execution results (pass/fail) still require a human to run the script and sign off per §11.100/11.200

## Cost control

- **RBAC** — this key's `scopes` must include `gxp-generate-document` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- No `_budget_stats` field in the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/gxp-generate-document \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "document_type": "OQ Protocol",
    "github_repo_url": "https://github.com/acme/lims-core",
    "outline": ["Objective", "Test Cases mapped to URS-001..003", "Acceptance criteria", "Deviation handling"],
    "plan_context": "GxP-Critical LIMS with supervisor approval workflow"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "gxp-generate-document",
  "document_type": "OQ Protocol",
  "title": "Operational Qualification Protocol — LIMS Core Result Entry & Approval",
  "content": "## 1. Objective\nThis protocol verifies... ## 2. Test Cases\nOQ-001: Verify operator identity and timestamp are recorded on result entry (traces to REQ-001)...",
  "traceability_refs": ["Traces to URS REQ-001, REQ-002", "References app/services/result_service.py:record_result"],
  "included_controls": ["GXP-003", "21 CFR 11.10(e)"],
  "test_cases": [
    {
      "test_case_id": "OQ-001",
      "objective": "Verify operator identity and timestamp are recorded on every result entry",
      "requirement_ref": "REQ-001",
      "automatable": true,
      "script_framework": "pytest",
      "script_path": "tests/oq/test_oq_001_result_audit.py",
      "script_content": "def test_result_entry_records_operator_and_timestamp(client, db_session):\n    ..."
    }
  ],
  "open_items": ["Actual protocol execution date and executor identity", "Approver signature — cannot be fabricated"]
}
```

## Frameworks / controls referenced

`21CFR11` · `GxP` (GAMP 5). Uses the shared `GXP-001`–`GXP-005` control library and cites specific `§11.xx` sections in `included_controls`.
