# AI Code Provenance & Risk

Assesses the AI-origin likelihood of a code artifact as **probabilistic evidence** — Low / Possible / Likely / Strong evidence — never a definitive verdict, because human edits to AI code (and AI edits to human code) remove most visible signals and code watermarking is easily defeated by routine changes. Combines an optional developer disclosure with LLM-based style-signal analysis into a scored, evidence-backed report using a fixed, published point system (never an ad-hoc score).

**Module:** Developer Intelligence
**Endpoint:** `POST /api/v1/agents/ai-provenance`
**Agent name (for scopes/BYOL config):** `ai-provenance`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | one of code/github_pr_url/github_file_url | Source code or diff to assess |
| `github_pr_url` | string | " | GitHub PR URL — fetches the diff automatically |
| `github_file_url` | string | " | GitHub file URL |
| `ai_used` | string | no | Developer's self-declared AI usage, e.g. `"Yes"`, `"No"`, `"Partially"` |
| `ai_tool` | string | no | e.g. `"GitHub Copilot"`, `"ChatGPT"`, `"Claude"`, `"Cursor"` |
| `purpose` | string | no | e.g. `"Generate"`, `"Refactor"`, `"Debug"`, `"Document"`, `"Test"` |
| `percent_ai_assisted` | string | no | e.g. `"0-25%"`, `"25-50%"`, `"50-75%"`, `">75%"` |
| `sensitive_data_shared` | boolean | no | Was sensitive/proprietary data shared with the AI tool? |
| `prompt_stored` | boolean | no | Was the prompt/session log captured and stored? |
| `human_review_completed` | boolean | no | Has a human completed review of this artifact? |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `code` · `github_pr_url` · `github_file_url`. Resolution priority: `github_pr_url` > `github_file_url` > `code`/`text`.

## Response

Custom envelope — the report is nested under `report`:

```json
{
  "success": true,
  "agent": "ai-provenance",
  "review_id": "...",
  "project_name": "...",
  "source_type": "github_pr",
  "source_ref": "...",
  "report": { "...": "see below" },
  "llm_used": {...},
  "created_at": "..."
}
```

`report` contains:

| Field | Description |
|---|---|
| `ai_origin_likelihood` | `Low` (0–25) \| `Possible` (26–50) \| `Likely` (51–75) \| `Strong evidence` (76–100) |
| `confidence` | `Low` \| `Medium` \| `High` — how much evidence backs the likelihood |
| `score` | 0–100, the sum of `score_breakdown` points, clamped |
| `score_breakdown[]` | Each: `factor`, `points` (signed, e.g. `+20`, `-15`), `evidence` — every point awarded must cite concrete evidence from the artifact, never an unexplained total |
| `style_signals[]` | Each: `signal` (one of `uniform_style`, `over_commented_simple_logic`, `generic_variable_names`, `boilerplate_heavy_structure`, `polished_but_shallow_logic`, `sudden_style_shift`, `generic_test_code`, `comments_inconsistent_with_logic`), `present` (bool), `detail` |
| `estimated_ai_generated_lines`, `estimated_percent_ai_assisted` | Rough estimates |
| `recommended_action` | `Normal review` \| `Ask developer declaration` \| `Enhanced security, license, and test review` \| `Require reviewer approval and evidence capture` — maps 1:1 to `ai_origin_likelihood` |
| `declared_vs_observed_note` | Whether the developer's disclosure is consistent with the observed evidence, or contradicts it — phrased as evidence, never an accusation |
| `summary` | Must read as "X likelihood based on Y evidence," never "this code was written by AI" |
| `declared_ai_use`, `declared_tool` | Echoes your `ai_used`/`ai_tool` input fields |
| `_budget_stats` | **Nested inside `report`**, not at the top level |

The scoring model is fixed and documented in the agent's own instructions (not invented per-call): a declared "Yes" adds +20 (+10 for "Partially"); a declared "No" never subtracts; 3+ independent style signals together add +15; one or two add +5–10; a large, single-pass-looking block with no incremental structure adds +10; style differing from surrounding context adds +10; evidence of substantial human modification subtracts 15.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars)
- **Timeout & retry** — 900s timeout, up to 2 retries on transient errors
- **Prompt-injection scan** — non-blocking; flags surface inside `report.guardrail_flags`
- **Untrusted content wrapping** — a fetched PR/file is wrapped before analysis
- **Product policy guardrail (prompt-level, not code-enforced):** the agent is instructed to never recommend blocking or rejecting code solely on AI-origin likelihood — "AI-generated code is allowed, but must be declared, reviewed, tested, and traceable," never a block

## Cost control

- **RBAC** — this key's `scopes` must include `ai-provenance` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- `_budget_stats` is nested inside `report`, not at the top level of the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/ai-provenance \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "github_pr_url": "https://github.com/acme/patient-portal/pull/104",
    "ai_used": "Partially",
    "ai_tool": "GitHub Copilot",
    "percent_ai_assisted": "25-50%",
    "human_review_completed": true
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "ai-provenance",
  "report": {
    "ai_origin_likelihood": "Likely",
    "confidence": "Medium",
    "score": 55,
    "score_breakdown": [
      {"factor": "Developer declared AI use (Partially)", "points": 10, "evidence": "ai_used=Partially, ai_tool=GitHub Copilot"},
      {"factor": "Multiple independent style signals present together", "points": 15, "evidence": "Uniform formatting across all 3 new functions plus generic variable names (data, result, temp) in a domain with clear naming conventions elsewhere in the file"},
      {"factor": "Generic/templated test code accompanying the change", "points": 5, "evidence": "New test asserts only status_code == 200, no edge cases despite branching logic in the function"}
    ],
    "recommended_action": "Enhanced security, license, and test review",
    "declared_vs_observed_note": "Consistent — developer declared partial AI use and the observed style signals support a moderate-AI-assistance estimate.",
    "summary": "Likely evidence of AI assistance based on uniform style, generic naming, and shallow test coverage, consistent with the developer's own disclosure."
  }
}
```

## Frameworks / controls referenced

`NIST-AI-RMF`. This is an evidence-scoring agent, not a control-mapped reviewer — it has no `control_id`-tagged findings; its output feeds AI-usage governance/audit processes instead.
