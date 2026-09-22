# Application-based test automation — implementation plan

_Drafted 2026-09-11, branch `test_automation_for_qa_agents`. Companion to the decision brief
"Two Roads for QA Automation"._

## Context

The code-based test generator ships and is measured: a requirement plus a repository
produces runnable tests, delivered as a PR with a CI workflow, whose results come back and
are reconciled against what the model *claimed* it covered. Two eval cases, 100%
red-on-broken and green-on-fixed.

It has one structural limit. `test_runner_plan.CANNOT_SUPPLY` (`test_runner_plan.py:78-81`)
names it out loud: `("running application", "database", "credentials", "seeded test data")`.
The product reads source and stops where a test needs a live system.

**Application-based testing is the case where that first entry stops being a missing
prerequisite and becomes the input.** No source code. A running application, a test account,
and a browser. An agent explores it, records what a user can actually do, and generates a
Playwright suite that replays those journeys on every deploy.

It reaches a buyer the current product structurally cannot: vendor products, legacy systems
and third-party portals where nobody will hand over source. And the hard parts are real —
an explorer that clicks *Delete account* causes damage, and assertions written without a
specification agree with today's bugs.

**Decisions taken before planning** (2026-09-11):

| Question | Answer |
|---|---|
| Where the browser runs | **Engine first.** The explorer is a library module; `scripts/explore_app.py` is a thin wrapper. A hosted runner is Phase 4, a second producer of the same contract. |
| Hosted shape (worker vs API host) | **Deferred to Phase 4.** Phases 1–3 are identical either way, so it is settled with better information. D-05 is untouched by this plan. |
| Scope of this increment | Through delivery **and** the eval. |
| Destructive-action default | Denylist + test-environment attestation, screened on both sides of the model. |
| Pricing | **Pass-through, priced after the eval measures a session** — D-404's own precedent, which chose a fee and markup against a measured baseline rather than guessing. |

---

## The one idea the design rests on

D-62 says *the results contract is the interface, not the producer*. This plan applies the
same move one level out:

> **The observation/action contract is the interface, not the browser.**

The explorer sends what it sees; the platform returns what to do next. Where the browser
runs is then a deploy target, not an architecture. A script on a developer's machine, a job
in the customer's CI, and a worker we operate later are three producers of one contract —
exactly as `run_tests.py`, the generated Actions workflow and a future sandbox are three
producers of the results contract.

That is what makes "engine first, host later" an addition rather than a rewrite, and it is
why nothing here needs D-05 revisited.

```
CUSTOMER'S NETWORK                         OUR PLATFORM
  explore_app.py
    ├─ observe page (accessibility tree)
    ├──────── POST /sessions/{id}/step ────►  screen candidates
    │                                          model picks one
    │◄─────── {action, journey} ────────────   re-screen the choice
    ├─ re-screen locally, then execute
    └─ (loop, to a hard step cap)
                                              ▼
  the app, the credentials, the browser     journey graph (text only)
  never leave this side                       │
                                              ▼
                                       POST /sessions/{id}/generate
                                          one call → Playwright suite
                                              │
                                              ▼
                                       reviews row (review_type=app_testing)
                                          → deliver-tests → PR + CI workflow
                                          → results come back through the
                                            existing POST .../test-results
```

---

## What already exists that this stands on

Reuse first — these are not to be rebuilt.

| Need | Already built | Where |
|---|---|---|
| **Playwright runner, install, JUnit, CI job** | The only runner row declaring `"supplies": ("browser",)` | `test_runner_plan.py:199-212` |
| **Results ingest + coverage reconciliation** | `map_to_requirements` gives a `not_run` verdict for a claim nothing exercised | `test_results.py:237-288` |
| **The output schema for a generated suite** | `TEST_AUTOMATION_TOOL_SCHEMA` — `test_files`, `test_names`, `covers`, `requirement_coverage`, `assumptions`, `not_automated`, and a `prerequisites` enum that already contains `"running application"`, `"browser"`, `"credentials"` | `test_automation_agent.py:66-232` |
| **PR / branch delivery of a suite + workflow** | `POST /api/reviews/{id}/deliver-tests` | `main.py:2491` |
| **Degeneracy guard for a QA generator** | `_refuse_degenerate` (D-405) | `orchestrator.py:72-102` |
| **Per-unit metering of a long multi-call run** | Modernization meters per unit, not per run | `runner.py:17-24` |
| **Browser driving idiom** | `sync_playwright()` → `chromium.launch()`, `free_port()`, `check()`/`section()`, network capture | `ui_examine.py:59-234`, `:358-364` |
| **SSRF guard** (needed only in Phase 4) | `_refuse_private` checks *every* resolved address, re-checks before each send, follows no redirects | `notify.py:122-167` |

**Reusing `TEST_AUTOMATION_TOOL_SCHEMA` for the generate call is the largest single
saving.** The suite comes out in a shape the existing renderer draws, `plan_for()` plans,
`deliver-tests` delivers and `map_to_requirements` reconciles — all unchanged. Only the
*system prompt* and the *input* differ: journeys instead of source.

---

## Phase 1 — the explorer engine and its driver

### `backend/services/action_policy.py` (new)

Pure, stdlib only, imports nothing from `backend` — the same rule `test_runner_plan.py`
follows (`:45-47`), because `scripts/explore_app.py` imports it too and a second copy of a
safety rule is how the rule comes back.

- `ACTION_KINDS = ("click", "fill", "select", "press", "goto", "back")` — a **closed
  vocabulary**. There is nothing to escape into.
- `screen(candidates) -> (allowed, refused)` — drops any candidate whose accessible name or
  role matches the destructive denylist (`delete`, `remove`, `cancel`, `deactivate`,
  `revoke`, `pay`, `purchase`, `checkout`, `confirm order`, `unsubscribe`, …), plus any
  form whose method or action looks irreversible.
- `permits(action, candidates) -> (ok, reason)` — re-checks a chosen action against the same
  rules **and** that its target is one of the candidates offered.

**The model never authors a selector.** An action targets a *candidate index*, and the
driver built each candidate from Playwright's own role/name query. Two consequences, both
load-bearing: a guessed selector is impossible by construction, and the "generated selectors
must survive a redeploy" problem is solved by construction too — every locator is
`get_by_role(role, name=…)`, which is the durable kind.

**Screened three times, on purpose.** Before the model sees the list; after it chooses;
and again on the machine that will actually perform the action. The first two mirror
`finding_gate` flooring a model's verdict — *the model can only ever be narrowed*. The third
matters most, because it runs where the consequences land.

### `backend/services/app_session.py` (new)

Session lifecycle and its own DDL, via `init_schema(cursor)` called from
`database.init_db()` — following `review_source_store.py:66-132`.

- `app_sessions` — `id, project_id, review_id (NULL until generated), target_label (host
  only, never the full URL), goal, requirement, mode, max_steps, steps_used, status,
  attested_test_env, started_at, finished_at, last_error, created_by`
- `app_steps` — `id, session_id, step_index, journey, url_path, page_signature,
  action_json, observation_chars, outcome, refused_count, created_at`

**No binaries are stored.** The platform has no `BLOB` column anywhere and every byte path
today is either an in-memory single-use dict or Fernet-encrypted TEXT. Screenshots, video
and traces stay on the machine that ran the browser; the driver prints the directory. This
is the deferral D-377 already made for video, for the same reasons, and it keeps Phase 1
free of new storage infrastructure.

**Termination** is three bounds, not a judgement call: a hard `max_steps` the platform
decrements and enforces; a model-returned `done`; and a **no-progress** rule — if the last
K steps produce no `page_signature` the session has not seen, stop. The signature is the
sorted set of `(role, name)` over interactive elements plus a URL path template. It does
not need to answer "same page?" perfectly; it needs to detect *no progress*, which is the
failure the brief flagged as hard.

### `backend/agents/qa_intelligence/app_explorer_agent.py` (new)

Two roles, one module:

1. **`NEXT_ACTION_TOOL_SCHEMA`** — small, one call per step. Required: `action_kind`
   (enum `ACTION_KINDS`), `candidate_index`, `journey` (the name of the flow this step
   belongs to), `rationale`; optional `value`; plus `done` and `done_reason` for
   "I have seen enough". `max_tokens` ≈ 1500.
2. **The suite generation prompt**, reusing `TEST_AUTOMATION_TOOL_SCHEMA` unchanged at
   `max_tokens=32000`. The system prompt differs: it is given journeys and observed
   outcomes, not source, and it is told the assertion-labelling rule below.

**Assertions are labelled `observed` or `specified`.** `specified` only where the assertion
traces to a requirement the caller supplied. With no requirement, every assertion is
`observed` and the summary must say so in plain words. This is `attest.coverage_claim`'s
rule applied to a new noun: a characterization test is a real and useful thing, and calling
it a verified requirement is not. It is also the mitigation for the accuracy ceiling the
brief identified — supplying requirements alongside the application is what converts one
into the other.

### `backend/routes/app_testing.py` (new)

| Route | Metering |
|---|---|
| `POST /api/v1/app-testing/sessions` | free — creates the session, refuses without the test-environment attestation |
| `POST /api/v1/app-testing/sessions/{id}/step` | `agent_guard.guarded_call` **per step** (the modernization precedent, `runner.py:17-24`) |
| `POST /api/v1/app-testing/sessions/{id}/finish` | free |
| `POST /api/v1/app-testing/sessions/{id}/generate` | `guarded_call`, one call, writes the `reviews` row |
| `GET /api/v1/app-testing/sessions/{id}` | free — progress, journeys, refused actions |
| `POST /api/v1/app-testing/preflight` | free — `check_scope` only, no budget enforcement, matching every other preflight |

The preflight quote is `review_estimate.combine(step_quote_×_max_steps, generate_quote)` —
`combine` exists for exactly this, "a run whose calls do not all go to the same agent"
(`review_estimate.py:195-233`). The step cap is what makes a session quotable at all.

### `scripts/explore_app.py` (new)

The driver, modelled on `run_tests.py` (flags, auth, `--dry-run`, `--fixture`) and on
`ui_examine.py` (browser launch, screenshots, network capture). It authenticates, opens a
session, loops observe → step → execute, finishes, then generates. It saves screenshots,
video and traces locally and prints where. `--fixture` is an offline self-check against a
bundled demo app with the decision endpoint stubbed — no server, no key, no model.

**Playwright is used by a shipped script and declared nowhere.** It is installed in `venv`
(1.62.0) and absent from `requirements.txt` — `ui_examine.py` already has this problem
today. It goes in a new `requirements-explorer.txt` rather than `requirements.txt`, because
`backend/` does not import it and installing a browser toolchain on the API host to satisfy
a script would be the wrong trade.

---

## Phase 2 — the product surface and delivery

- **`backend/products/registry.py`** — one row: `id="app-testing"`, `kind=AGENT`,
  `module="qa_intelligence"`, `review_type="app_testing"`, `sources=("running_app",)`,
  `endpoint="POST /api/v1/app-testing/sessions"`. The id is persisted in
  `api_keys.scopes` and the cost ledger, so it is a data migration to change later
  (`registry.py:20-30`).
- **`backend/services/orchestrator.py`** — `generate_app_suite(...)` following
  `generate_test_automation` (`:809-916`) exactly: `_rows()` filtering before any field
  access (D-392), `_refuse_degenerate("Application suite generation", …)` after filtering,
  the same derived counters, `execution_status="not_executed"`, and `save_review`.
- **`backend/services/test_results.py`** — `ACCEPTS_RESULTS += ("app_testing",)`. It is
  deny-by-default (`:84`), so results are refused until this is added. Same for whatever
  gates `deliver-tests` on review type.
- **`backend/services/review_estimate.py`** — `MAX_OUTPUT_TOKENS["app-testing"] = 32000`
  and `["app-testing-step"] = 1500`, plus `_frame_tokens` branches for both. (Note in
  passing: there is no `test-automation` branch either, so it currently prices against the
  code reviewer's prompt — real, small, and a separate change because it moves an existing
  quote.)
- **`frontend/app.js`** — an `AGENT_SCREENS` row (`gated: false`, no `inventory`) and
  `renderAppTesting(targetId, r)`, which draws the journey list and the observed/specified
  assertion split, then delegates the file/coverage/assumption sections to the parts
  `renderTestAutomation` already draws. §26 of `smoke_review_sources.py` fails without the
  row, and also fails unless `index.html` carries `id="app-testing-result"` and
  `id="app-testing-runs"` and `app.js` carries `function renderAppTesting(`.
- **`frontend/index.html`** — nav item with `data-page="app-testing" data-product="app-testing"`,
  the screen, the target/goal/requirement inputs, the attestation checkbox, and the
  session progress panel.
- **`docs/agents/app-testing.md`** — required, or `agent_docs.NO_DOC` with a reason.

---

## Phase 3 — the eval

`scripts/eval_app_testing.py`, with `evals/app_testing/<case>/{spec.json, broken/, fixed/}`
where each variant is a **runnable web application** rather than a source tree. Each is a
stdlib-only `http.server` app serving real HTML forms — dependency-free for the same reason
D-403 gives: a red result must mean the tests failed, not that an install did.

The headline metric is the one that means anything for a regression suite:

1. Serve `fixed/` on a spare port. **Discover against the working app** — that is what a
   customer does.
2. Generate the suite.
3. Replay it against `fixed/` (**must be green**) and against `broken/` (**must be red**).
4. `discriminates = red on broken AND green on fixed`, plus `ran`, and
   `assertion_labels` (the observed/specified split).

Discovering against the working app and replaying against a break measures the claim the
product actually makes — *this catches regressions* — rather than the claim it does not
make. A second mode supplies the requirement during discovery against `broken/` and asks
whether any assertion is `specified` and fails; that is the harder claim and the number to
watch, not the headline.

Preflight for the runner before spending anything, the way `eval_test_automation.py` checks
for pytest (D-403): node and `npx playwright` must be present, because a missing runner and
a suite that will not execute score identically.

**Floors are set one run below the measurement, never at it** (D-403) — a floor at the
measurement fails on a flake and then gets lowered.

---

## Deferred, deliberately

| Item | Why not now |
|---|---|
| **Hosted browser runner** | Phase 4. The contract makes it an additional producer. Building the fleet before the eval says whether journeys are worth anything is the sandbox mistake in a new costume. |
| **Video / trace storage and serving** | Nearly free at the point of capture, and the platform has no binary storage at all. D-377 already deferred video for the code-based product; the reasons have not changed, and one of them is a policy question about recording real passwords and PII. |
| **Flake policy** — retry, quarantine, flake scoring | Real, and it decides whether anyone trusts the suite in six months. It needs run history to act on, which does not exist until suites have been replayed repeatedly. |

---

## Verification

Offline — no key, no network, no server:

```
venv/Scripts/python.exe scripts/smoke_app_testing.py       # new: sessions, screening, caps
venv/Scripts/python.exe scripts/smoke_products.py          # registry row, entitlement, preflight
venv/Scripts/python.exe scripts/smoke_review_sources.py    # §26 AGENT_SCREENS parity
venv/Scripts/python.exe scripts/smoke_governance.py
node scripts/smoke_render.js                               # renderAppTesting
venv/Scripts/python.exe scripts/ui_examine.py              # the button is actually wired
venv/Scripts/python.exe scripts/explore_app.py --fixture   # write/observe/act loop, model stubbed
```

`smoke_app_testing.py` follows D-09 exactly — plain script, `check()`/`section()`, exit 0/1,
`TestClient` against a throwaway SQLite file, `llm_gateway.call_tool` monkeypatched with the
signature `(system_prompt, user_content, tool_schema, llm_override=None, max_tokens=None)`
**and calling `llm_gateway._record_usage(...)`**, or all metering is invisible (D-312). No
pytest. Sections to cover, at minimum:

1. A destructive candidate never reaches the model, and a destructive choice is refused
   even if the model returns one — checked **behaviourally**, by driving the endpoint with
   a stub that tries to click *Delete account*, not by grepping the denylist.
2. The step cap is enforced by the platform, not trusted from the driver.
3. A session with no attestation is refused.
4. A suite generated with no requirement labels every assertion `observed` and says so.
5. `_refuse_degenerate` fires on an empty generation and nothing reaches the DB.
6. Results posted for an `app_testing` review are accepted and reconciled.

Then live, spending real money, on the tier `run_tests.py` occupies and never the API host:

```
venv/Scripts/python.exe scripts/explore_app.py --url <app> --max-steps 40 --shots out/
venv/Scripts/python.exe scripts/eval_app_testing.py            # red/green, 3 runs per case
venv/Scripts/python.exe scripts/eval_app_testing.py --repeat 1 # cheaper; cannot measure variance
```

And because no offline test can see a write path (D-317, D-364), `deliver-tests` for an
`app_testing` review must be driven against a **scratch** repository by hand before it is
trusted.

---

## Rows for `DECISIONS.md`

Next free ids: **D-407**, **C-94**. (`D-406` and `C-93` are current highs. Note `D-188`–`D-192`
are each duplicated in the file already; do not follow that precedent.)

- **D-407** — The observation/action contract is the interface, not the browser. D-62 one
  level out: the explorer is a library module, `scripts/explore_app.py` is one producer, a
  hosted worker is a later one, and nothing here touches D-05.
- **D-408** — The model picks from a screened candidate list and never authors a selector.
  Screened before it sees the list, after it chooses, and again on the machine that
  executes. Same shape as `finding_gate` flooring a verdict: narrowed only, never widened.
  It also removes guessed selectors and selector brittleness by construction.
- **D-409** — Assertions are labelled `observed` or `specified`; a suite generated without a
  requirement says so. `attest.coverage_claim`'s rule for a new noun.
- **D-410** — `app-testing` is deliberately unpriced (pass-through) until the eval has
  measured a session, following D-404's own precedent of measuring before setting a fee.
- **C-94** — Page content is untrusted input to the model. The closed action vocabulary and
  the candidate-index targeting bound what an injected instruction can achieve; they do not
  eliminate it, and a page that renames a destructive control evades a name-based denylist.
- **C-95** — One eval case is a count, not a percentage. Say so wherever the number appears.

## Needs a policy decision (the product owner's, not the implementer's)

- **The pricing basis**, once the eval has measured a session. Deferred on purpose, not
  forgotten — the preflight quotes but does not charge a fee until this lands.
- **Whether we ever store video or traces**, which capture real passwords and PII. D-377
  deferred this once; app-based testing is where it becomes valuable, so it will be asked
  again.
- **Whether a hosted runner may hold a live credential to a customer's running system.**
  Categorically different from a GitHub read token: an app login can place an order or
  delete a record. Phase 4.

---

## What could go wrong

- **The denylist is a heuristic over accessible names.** "Archive", "Finalise", "Send" or an
  icon-only button with no accessible name will not match it. The attestation and a
  dedicated test environment are doing more of the safety work than the denylist is, and
  the plan should be read that way rather than as a technical guarantee.
- **Discovery quality is entirely unmeasured** — this is n=0, where the code-based product
  is n=2. The eval is what turns it into a number, which is why it is in this increment
  rather than after it.
- **A journey graph is not a user journey.** The explorer will find what is reachable, which
  is not the same as what matters. The four-part honest report the brief specified — real
  flows versus noise, meaningful assertions versus "the page loaded" — is the check on that,
  and it is a judgement a person has to make on the first run.
- **The step-per-call cost is unbounded by prompt size.** An accessibility tree for a dense
  page is large, and 40 steps of it is real money. The step cap bounds it; prompt caching
  would help and is not in this plan.
- **Reusing `TEST_AUTOMATION_TOOL_SCHEMA` couples two products.** A future field added for
  code-based generation arrives in app-based whether it fits or not. It is worth the saving
  today; it is the first thing to revisit if the two outputs diverge.
