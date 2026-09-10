# Test Automation Generation

Generates **runnable automated tests** against a real codebase — pytest, Playwright, Jest, JUnit, xUnit and the rest — following the project's own test conventions, fixtures and directory layout rather than a generic framework example. Declares what a script genuinely cannot verify (`not_automated`) and every fact it had to guess (`assumptions`, each with what breaks if the guess is wrong).

This is the only QA product that reads source, and that is the whole reason it exists. [`test-scenario`](test-scenario.md) and [`test-case`](test-case.md) take prose and produce work for a human: a manual step can say "click Submit". A line of code cannot — it has to name a route, a function, a fixture or a selector that really exists, and an agent that has never seen the codebase will invent all four.

**Nothing here is executed.** The output is a suite to run, not a result. `execution_status` is always `not_executed`.

**Module:** QA Intelligence
**Endpoint:** `POST /api/v1/agents/test-automation`
**Preflight:** `POST /api/v1/agents/test-automation/preflight` (free, no model call)
**Agent name (for scopes/BYOL config):** `test-automation`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

## Request

| Field | Type | Required | Description |
|---|---|---|---|
| `requirement` | string | one of requirement/cases_context | What the tests must verify |
| `cases_context` | string | " | A prior manual catalogue (the `test_cases` array from [`test-case`](test-case.md), JSON-stringified) to automate. Generated tests reference its `test_case_id` values in `covers` |
| `levels` | string[] | no | Any of `Unit`, `API/Integration`, `UI/End-to-End`. Omit to let the agent choose and justify in `levels_chosen` |
| `files[]` | object[] | one source required | Explicit `{path, content}` list — highest precedence, and the best way to keep a run cheap and in `source` mode |
| `github_repo_url` + `ref` | string | " | Repository at a branch, tag or commit |
| `github_pr_url` | string | " | Generate tests for what a PR changed |
| `github_file_url` | string | " | One file |
| `base_ref` + `head_ref` | string | " | A branch comparison |
| `github_token` | string | no | Per-call token; overrides the project credential and the deployment default |
| `project_name`, `context`, `post_back` | — | no | Standard fields |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

A zip upload is accepted through the browser screen. `requirement` also resolves from `artifact` / `content` / `code` / `text` for callers driving the registry generically.

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `test_files[]` | Each: `path`, `framework`, `level`, `prerequisites[]` (`none`\|`running application`\|`database`\|`browser`\|`network access`\|`credentials`\|`seeded test data`), `content` (the complete runnable file), `test_names[]`, `covers[]` |
| `levels_chosen[]` | Each: `level`, `rationale` — why this level suits this requirement and this codebase |
| `requirement_coverage[]` | Each: `requirement_ref`, `covered`, `test_names[]`, `note`. One row per requirement **including the ones not reached** |
| `not_automated[]` | Each: `requirement_ref`, `reason` — what needs a human, and what they would have to do |
| `assumptions[]` | Each: `assumption`, `why_unverifiable`, `risk_if_wrong`. **Read this first** — it is where a guessed route or selector is declared |
| `setup_instructions` | Install steps, the exact command, env vars, how to bring the app up and seed data |
| `input_mode` | `source` if the codebase was sent verbatim, `description` if it was too large and a computed structural summary went instead |
| `test_conventions` | What was found about how this project already tests itself: `note`, `has_tests`, `test_files`, `test_paths`, `frameworks`, `located_by`, `exemplar_paths` |
| `code_brief` | Coverage facts from the structural analysis, present when `input_mode` is `description` |
| `total_files`, `total_tests`, `levels_present`, `prerequisites_present`, `uncovered_count`, `assumption_count`, `not_automated_count` | Counted server-side from the returned arrays, not taken from the model |
| `execution_status` | Always `not_executed` — see below |
| `summary`, `_budget_stats`, `llm_used`, `created_at` | Standard |

## Why nothing is executed

D-05 forbids compiling or running code on the API host: executing model-written code derived from customer source is arbitrary code execution, and no human has reviewed it. Running a generated suite also needs things the platform does not have — the application up, a database in the state the test assumes, and credentials (C-63).

So the honest claim is **"these were generated"**, never "these pass", and `execution_status` says so rather than leaving a reader to infer it from the absence of results. When execution arrives it will follow D-62 — a results contract that the customer's CI, a local script or a sandbox worker can all fill, exactly as `build_results.py` already does for compiler diagnostics.

## What the agent is shown, and why it matters

Two deterministic descriptions are assembled before the call. Both are free — no model, no cost — and both are also returned by the preflight so their limits are visible *before* deciding to spend.

- **`test_context_brief`** — how this project already tests itself: framework, runner config, shared fixtures, where the client is constructed, and **one or two of its real test files quoted verbatim**. This is the D-316 mitigation. A generated test that imports a helper the project does not have fails at collection time and proves nothing, and the strongest defence against that is showing the agent a test that already works.
- **The codebase itself, in one of two modes.** Small enough to send and the real source goes, so scripts can reference real signatures and routes (`input_mode: source`). Too large and `code_brief`'s computed structural description goes instead (`input_mode: description`), and the agent is told in as many words that anything the description does not state is a guess belonging in `assumptions`.

A suite generated from a description is a weaker artifact than one from source. `input_mode` exists so a reader can tell them apart instead of trusting both equally. **Narrowing the source — an explicit `files[]` list, or a PR — is the cheapest way to get the stronger mode.**

## Guardrails applied

- **Free preflight** — `POST /api/v1/agents/test-automation/preflight` prices the exact prompt the run will send, calls no model and meters nothing. Priced from the assembled prompt rather than a chunked bundle, because this agent makes **one** call
- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars); the source-inlining threshold sits well below it so the requirement and briefs always fit
- **Secret redaction on ingest** — the shared `source_bundle` path redacts credentials before any model call, and reports what it redacted by path and line, never by value
- **Traversal and zip-bomb guards** — inherited from `repo_ingest`, the same intake Code Review uses
- **Untrusted content wrapping** — a fetched PR diff is wrapped before analysis
- **Degeneracy guard** — a response with no `test_files` and no `summary` raises rather than storing an empty suite as a successful generation. Neither QA agent goes through `attest`, so this is explicit in the orchestrator
- **Counts computed server-side** — `total_tests` and the rest are derived from the returned arrays, so a model miscounting its own output cannot put a wrong number on a screen
- Test data is required to be clearly synthetic — never real or plausible personal data, never live credentials

## Cost control

- **RBAC** — this key's `scopes` must include `test-automation` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month
- **Output budget** — `max_tokens=32000`, the top tier in the codebase alongside `auto-fix`, because an entry in `test_files` is a whole file rather than a findings row. `review_estimate.MAX_OUTPUT_TOKENS` carries the same figure so the quoted ceiling matches what the call can actually emit
- `_budget_stats` is returned in the response

## Example

```bash
curl -X POST https://your-host/api/v1/agents/test-automation \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "requirement": "A clinician may export a patient visit history to CSV only if they are the treating clinician. Anyone else gets 403 and the attempt is audited.",
    "github_repo_url": "https://github.com/acme/patient-portal",
    "ref": "main",
    "levels": ["API/Integration"],
    "project_name": "Patient Portal API"
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "test-automation",
  "input_mode": "source",
  "levels_chosen": [
    {"level": "API/Integration", "rationale": "The requirement is about an authorization decision on an HTTP route; the repository already has an httpx-based client fixture, so this is checkable end to end without a browser."}
  ],
  "test_files": [
    {
      "path": "tests/test_visit_export_authz.py",
      "framework": "pytest",
      "level": "API/Integration",
      "prerequisites": ["running application", "database", "seeded test data"],
      "test_names": ["test_treating_clinician_can_export", "test_other_clinician_gets_403", "test_denied_attempt_is_audited"],
      "covers": ["treating-clinician-only export", "denied attempt is audited"],
      "content": "import pytest\n\nfrom app.models import AuditLog\n\n\ndef test_other_clinician_gets_403(client, patient, other_clinician):\n    client.force_login(other_clinician)\n    r = client.get(f\"/patients/{patient.id}/export\")\n    assert r.status_code == 403\n    assert \"csv\" not in r.headers.get(\"content-type\", \"\")\n"
    }
  ],
  "requirement_coverage": [
    {"requirement_ref": "treating-clinician-only export", "covered": true, "test_names": ["test_treating_clinician_can_export", "test_other_clinician_gets_403"]},
    {"requirement_ref": "denied attempt is audited", "covered": true, "test_names": ["test_denied_attempt_is_audited"], "note": "Asserts an AuditLog row is written; does not assert its retention."}
  ],
  "not_automated": [
    {"requirement_ref": "the export button is hidden for non-treating clinicians", "reason": "A visual/DOM assertion, and no template or component file was supplied. A UI/End-to-End test could cover it once the markup is in scope."}
  ],
  "assumptions": [
    {
      "assumption": "`client.force_login(user)` is the project's login helper, as used in tests/test_records.py",
      "why_unverifiable": "The helper is defined in a conftest.py that was included, so this is low risk.",
      "risk_if_wrong": "All three tests fail during setup rather than reporting a real defect."
    }
  ],
  "total_files": 1,
  "total_tests": 3,
  "prerequisites_present": ["database", "running application", "seeded test data"],
  "uncovered_count": 0,
  "execution_status": "not_executed"
}
```

## Frameworks / controls referenced

None declared in `AGENT_CATALOG`. This is a QA generation agent, not a compliance or security reviewer: it returns no `control_id`-tagged findings and no gate. `AGENT_SCREENS` marks the screen `gated: false` for that reason — `save_review` defaults an absent gate to Green, and rendering "Green — 0 findings" over a generated suite would be a claim of safety nobody made.
