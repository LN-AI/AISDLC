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
| `focus` | bool | no | Send only the files the requirement is about, plus what they import, instead of the whole codebase. Cheaper and less complete. Ignored when narrowing would not save anything, or when there is nothing to rank (a paste has no filename) |
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
| `input_mode` | `source` (whole codebase verbatim), `hybrid` (the files the requirement is about verbatim, beside a structural description of the rest) or `description` (structural description alone) |
| `input_selection` | Which files were quoted in full and **why each one was picked** — `selected[]` (`path`, `chars`, `score`, `why[]`), `selected_paths`, `selected_chars`, `considered`, `omitted_for_budget[]`, `source_chars`, `prompt_chars` |
| `test_conventions` | What was found about how this project already tests itself: `note`, `has_tests`, `test_files`, `test_paths`, `frameworks`, `located_by`, `exemplar_paths` |
| `code_brief` | Coverage facts from the structural analysis, present when `input_mode` is `hybrid` or `description` |
| `total_files`, `total_tests`, `levels_present`, `prerequisites_present`, `uncovered_count`, `assumption_count`, `not_automated_count` | Counted server-side from the returned arrays, not taken from the model |
| `execution_status` | `not_executed` on the generation itself. `GET .../test-results` reports `reported` once somebody has filed a result |
| `summary`, `_budget_stats`, `llm_used`, `created_at` | Standard |

## Why nothing is executed

D-05 forbids compiling or running code on the API host: executing model-written code derived from customer source is arbitrary code execution, and no human has reviewed it. Running a generated suite also needs things the platform does not have — the application up, a database in the state the test assumes, and credentials (C-63).

So the honest claim is **"these were generated"**, never "these pass", and `execution_status` says so rather than leaving a reader to infer it from the absence of results.

## Reporting what happened when you ran them

Run the suite wherever you can run it, then post the result back. This follows D-62 — the results contract is the interface, not the producer: a developer on a laptop, a GitHub Actions job and a future sandbox worker all fill the same one, and none of them appear in the platform's code.

```
POST /api/reviews/{review_id}/test-results
GET  /api/reviews/{review_id}/test-results
```

**Free.** Recording an outcome is not a generation: no model call, nothing metered.

| Field | Description |
|---|---|
| `junit_xml` | A JUnit XML report — what pytest (`--junitxml`), Jest (`jest-junit`), JUnit, Playwright and most runners already emit |
| `results[]` | Structured rows instead: `{name, status}` plus optional `file`, `classname`, `duration_ms`, `message` |
| `source_label` | Where it ran, in your words — "GitHub Actions run 412". Stored beside the counts |

Send one or the other, never both: two reports of one run cannot be reconciled, and guessing which is authoritative is how a green result gets stored over a red one.

`status` is `passed` / `failed` / `errored` / `skipped`. **`errored` is kept apart from `failed` on purpose** — a failed assertion means the code is wrong, an error means the test never got far enough to find out, which is usually a missing import or fixture. That is D-316's signature and the single most useful number this endpoint produces. An unrecognised status reads as `errored`, never as a pass.

### What comes back is the reconciliation, not an acknowledgement

Every requirement the generation reported, with what actually ran against it:

| `outcome` | Meaning |
|---|---|
| `passed` | Its tests ran and passed |
| `failed` | At least one failed or never loaded |
| `skipped` | Its tests ran but every one was skipped |
| **`not_run`** | **The generation claimed to cover it, and nothing in this report matches.** Read this one first |

`not_run` is the verdict that could not exist before. Until results came back, `requirement_coverage` was the model's *claim* that a rule was covered and nothing could contradict it.

Test names never match character for character — pytest reports `test_x`, Jest reports the whole `describe > it` sentence — so matching is deliberately loose. A missed match understates coverage, which is the direction that makes a reader distrust a real result.

Results that match no requirement are **counted, not dropped**: your own suite runs alongside the generated one, and a report broader than this generation should read as broader rather than as noise.

### These are claims, not proof

Nothing in the platform witnessed the run. A report is posted with your key, and `attested` is always `false`. Every report is stored with who filed it and where it ran, because a pass with nobody's name on it is not evidence of anything, and reports are **kept rather than overwritten** — "this used to pass" is a fact one snapshot cannot express.

A suite of nothing but skips is not a pass. A report with no test cases is refused rather than stored as an empty success.

## Getting them run in the first place

```
GET /api/reviews/{review_id}/runner
```

**Free.** Everything it returns is a lookup from the framework each generated file declared — no model call, nothing metered.

The endpoint above will take a result from anyone. That is not the same as anyone sending one: "run them and post the result" is three unstated decisions — which command, which JUnit flag, which URL — and each one is a reason it does not happen. The platform knows all three, so it answers them.

| Field | What it is |
|---|---|
| `local_command` | `python scripts/run_tests.py --review <id>` — writes the files into a checkout, runs them, posts the result |
| `workflow_yaml` | A GitHub Actions workflow to commit beside the tests. Runs on every push and reports back |
| `plan.jobs[]` | Per framework: the exact `argv`, the shell form, where its JUnit report lands |
| `must_supply` | What no CI runner has by default and you must provide |
| `blocked_reason` | Why no workflow could be rendered, when none could |

Pass `?callback_url=https://...` when the deployment's `PUBLIC_BASE_URL` is not an address a CI runner can reach. A loopback or private address is **refused** rather than rendered: the job would run, fail to post, and look exactly like a suite nobody triggered.

### The local command

```
python scripts/run_tests.py --review <id> --into ../their-repo --user admin --password ...
python scripts/run_tests.py --review <id> --dry-run        # prints every path and command, touches nothing
python scripts/run_tests.py --review <id> --install        # also installs the reporter packages
python scripts/run_tests.py --review <id> --workflow       # also writes the CI workflow
python scripts/run_tests.py --fixture                      # offline self-check, no server, no key
```

It writes model-written files into a real repository, so: a path that is absolute, contains `..`, or carries anything a shell would read is **refused rather than sanitised**; every write is re-checked to land inside `--into`; and an existing file is never overwritten without `--force`. The run command is executed with **no shell**, because the one model-written part of it is the file paths.

### The workflow

One job, one step per framework, and a report step that runs `if: always()` — the failing run *is* the payload, and a workflow that stops at the first red step reports nothing.

**A suite that never ran still reports.** If no JUnit XML was produced, the report says so as one `errored` row. Silence would leave every requirement holding its unchallenged claim of coverage, which is the thing this whole loop exists to end.

### Two ways the results come home, and you do not need a public address

`report_mode` on the runner endpoint says which one you get.

| `report_mode` | What the job does | When |
|---|---|---|
| `callback` | Posts the report itself, using your platform API key from the repository secret `LM_API_KEY` | The platform has an address a GitHub runner can reach |
| `artifact` | Saves the report as its own workflow artifact and sends nothing | Anything else — a laptop, a private network, a deployment behind a firewall |

**`artifact` is not a lesser mode.** Same file, same run, same merged report — only the courier changes. Nothing inbound is needed and no secret has to be provisioned, because every call is one the platform makes:

```
POST /api/reviews/{review_id}/test-results/from-actions
{"github_repo_url": "https://github.com/owner/repo", "run_id": "optional", "branch": "optional"}
```

Omit `run_id` and it takes the most recent **completed** run of the generated workflow — an in-progress run has no artifact yet, and reporting "no results" for one still running would read as "it produced none". From the command line:

```
python scripts/run_tests.py --review <id> --from-actions <run url>
python scripts/run_tests.py --review <id> --from-actions latest --repo https://github.com/owner/repo
```

Needs **"Actions: Read"** on the GitHub token — a separate permission from the Contents and Pull requests ones everything else uses. Without it GitHub answers 404, the same as it does for anything a credential cannot see, so the error message names it.

Artifacts expire (90 days by default, and a repository can disable them). That is the one failure this mode has and the direct callback does not: the report is only retrievable while GitHub still holds it. The refusal says so rather than reporting an empty result.

### Putting the tests in the repository

```
POST /api/reviews/{review_id}/deliver-tests
{"github_repo_url": "...", "open_pr": true, "include_workflow": true}
```

**Free — no model call.** A **new** branch that must not already exist, a pull request only if asked for, and never a commit onto a branch somebody may have checked out. That is the same door generated code goes through and deliberately not a wider one: generated tests have had no more review than generated code.

The workflow is committed alongside by default, so the suite runs on push and its results come back without anyone remembering to ask. Writing `.github/workflows/` needs **"Workflows: Read and Write"** on the token; without it GitHub refuses that one file, the tests are committed anyway, and the response says which happened rather than leaving it in an array nobody reads.

The pull request body leads with **"never run, not reviewed"**, lists the assumptions the model declared with their stated risk, names the requirements the suite does not cover, and states what no CI runner will supply. The failure mode being designed against is a reviewer reading a generated test as a verified one.

### What it cannot do for you

A workflow cannot conjure a running application, a database, credentials or seeded rows. Those are listed in `must_supply`, stated at the top of the generated file, and left as marked places for you to fill in — a commented `services:` block and an `env:` section. A guessed database version with guessed credentials fails on run one and is harder to debug than an absent one.

Two more things are named rather than guessed at:

- **A framework nothing here knows** is reported in `unknown_frameworks` and left out of the command. Those files still run your own way, and the results endpoint takes them either way.
- **JUnit XML is not free in every framework.** Jest, Go, RSpec and .NET need an extra reporter package (`extra_packages` says which). Without it the runner works, writes nothing, and the suite reads as never run.

## What the agent is shown, and why it matters

Two deterministic descriptions are assembled before the call. Both are free — no model, no cost — and both are also returned by the preflight so their limits are visible *before* deciding to spend.

- **`test_context_brief`** — how this project already tests itself: framework, runner config, shared fixtures, where the client is constructed, and **one or two of its real test files quoted verbatim**. This is the D-316 mitigation. A generated test that imports a helper the project does not have fails at collection time and proves nothing, and the strongest defence against that is showing the agent a test that already works.
- **The codebase itself, in one of three modes.**
  - **`source`** — it fits, so all of it goes verbatim and the scripts can reference real signatures and routes. The default whenever it is available: narrowing a codebase that already fits would trade accuracy for a saving nobody asked for.
  - **`hybrid`** — it does not fit, so the files the requirement is about **and what they import** go verbatim inside a budget, beside `code_brief`'s structural description of everything else. The agent is told plainly that a route, signature, response shape or selector it needs from a file it cannot see in full is a guess belonging in `assumptions`.
  - **`description`** — nothing could be ranked, so the structural description travels alone. Reachable only when the source genuinely does not fit; when it fits, it is sent.

A suite generated from a description is a weaker artifact than one from source. `input_mode` exists so a reader can tell them apart instead of trusting both equally.

### How the files are ranked

Deterministically, with no model. Each requirement word is first weighted by **how rare it is in this codebase** (normalised inverse document frequency): a word in half the files cannot tell one from another, a word in two files almost always can. That is measured per codebase rather than kept as a stop list — "model" is uninformative in a platform that calls an LLM on every route and highly informative in an app about fashion photography. Words the codebase never uses are dropped entirely rather than scored as rare.

On top of that weighting: filename and directory matches against the requirement's own words (split on camelCase and snake_case, and pluralised, because requirements say "task" and files are called `tasks.js`), route literals pulled apart so `PUT /tasks/:id` contributes `tasks`, capped body-term frequency so a long file cannot outrank a short one by length alone, a bonus for the application entry point a test needs to make a request against, and a penalty for existing tests, which `pick_exemplars` already quotes.

**Then one dependency hop.** Whatever a selected file imports comes with it, resolved textually against the bundle's own paths. This is the half that matters most: the middleware a route file imports usually matches no word in the requirement and scores zero, and it is exactly the file whose absence makes a generated import line a guess (D-316). Resolution is textual rather than parsed on purpose — `code_brief`'s dependency graph has real edges for Python, PHP, Java, C# and Delphi and **none for JavaScript** (C-24).

**No single file may take more than 40% of the budget**, the top-ranked file excepted — measured here, a 66,000-character file ranked second on a filename match and took 55% of the budget, pushing out four smaller files that were closer to the requirement. Anything skipped is named in `omitted_for_budget`.

Whole files only. A truncated file is worse than an absent one: the model cannot tell it is reading half of something, so it reasons about the missing half as though it had seen it.

**What ranking cannot do.** It matches words. A requirement whose vocabulary does not appear in the code — "stored as an error" when the file is called `attest.py` — will rank the right file low however the weights are set, and this is a measured limit rather than a tuning problem (C-90). For an abstract requirement, pass an explicit `files[]` list: you choose, instead of a heuristic choosing.

`input_selection` returns the ranking — every file, its score and the reason it was picked — on the **free preflight** as well as the result, so a wrong pick is visible before any spend rather than inferred from bad tests afterwards.

**`focus` is the cost lever**, and it is opt-in rather than automatic because it is a real trade: a file that turns out to matter and was not selected is invisible except in the description. It is declined, with the reason stated in `input_mode_note`, when narrowing would not actually save anything — a structural description can be longer than the small codebase it replaces, and a cost control that raises the cost is a control that lies. **An explicit `files[]` list or a PR source is still the sharpest way to narrow**, because you choose the files rather than a heuristic choosing them.

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
