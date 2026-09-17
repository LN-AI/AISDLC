# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## How to explain your work

Every answer, every proposal and every finding needs four things. Not as headings to fill in —
as things the reader must not have to ask for.

**1. Plain language first.** Lead with what happened and what it means for the user, in words
that assume no knowledge of this codebase, of .NET, of PHP, or of the platform's own
vocabulary. "The converted app compiles but crashes the moment you start it" before
`AmbiguousMatchException`. Domain terms are fine *after* the plain sentence, never instead of
it. If a term is unavoidable, define it once in passing.

**2. The reasoning, not just the conclusion.** Say why this and not the alternative you
considered. A recommendation with no visible reasoning cannot be disagreed with, which makes
the user a rubber stamp rather than a decision maker — and they are the one who knows things
you do not.

**3. What could go wrong.** State the weaknesses of your own suggestion: what it does not
cover, what it assumes, what evidence it rests on and how thin that evidence is (`n=1` is worth
saying out loud). If you are choosing where to intervene in a chain of causes, say why that
point and not one further upstream. A proposal presented without its downsides is a sales
pitch.

**4. How you intend to build it, briefly.** A few sentences of mechanism — which files, what
shape, what the new thing does when it fails — before writing code. Enough that the user can
stop you if the approach is wrong, and not so much that it is the code written twice.

The same applies to explaining *findings*, not only proposals: lead with the consequence, then
cite `file.py:line`. And never use the platform's own conventions as though they were common
knowledge — a checkpoint, a gate, a unit, a seam and a component all mean something specific
here and nothing anywhere else.

## Read `DECISIONS.md` first

`DECISIONS.md` (repo root) is the durable memory across sessions: architectural decisions and
what they rejected, known landmines, and corrections to earlier analysis. Read it before
proposing architecture or diagnosing something that looks like a bug — several things that
read as defects are documented, deliberate trade-offs.

Add a row when you decide something non-obvious, hit a landmine, or find that earlier analysis
was wrong. Keep entries to one line, cite `file.py:line` rather than restating code, and delete
rows once they are resolved — the file is only useful while it stays scannable.

## Project overview

LM AI-SDLC ReviewOps Agent — an AI-powered governance/review platform covering the full SDLC (requirement → architecture → dev → QA → security → ops → governance) via ~25 specialized agents (Code Review, Code Generation, Security/Threat Model, Auto-Fix & Open PR, Issue Triaging, Compliance Mapper, Governance Report, etc.), organized into 9 "Intelligence" modules under `backend/agents/<module>/`. Backend is FastAPI + Python, orchestrating calls to Anthropic Claude. Frontend is a plain HTML/CSS/JS SPA with no build step, served as static files by FastAPI.

## Running the app

```
pip install -r requirements.txt
cp .env.example .env      # then set ANTHROPIC_API_KEY
python run.py
```

- Entry point is `run.py`, which runs `uvicorn.run("backend.main:app", ...)` on `0.0.0.0:8000`
  with the reloader watching only `backend/` (frontend static files don't need reload).
  `HOST`, `PORT` and `RELOAD=0` override all three — **use `RELOAD=0 python run.py` rather than
  a bare `uvicorn backend.main:app`**, because that changes the bind address from `0.0.0.0` to
  `127.0.0.1` and is how the collision below happens.
- **`run.py` refuses to start when something is already serving that port**, naming the PID,
  its command line and its start time. This is not politeness: `0.0.0.0` and `127.0.0.1` are
  different addresses to Windows, so a stale process on `127.0.0.1:8000` does *not* stop a new
  bind on `0.0.0.0:8000` — both listen, the log says "Application startup complete", and the
  browser (resolving `localhost` to `127.0.0.1`) keeps talking to the old one. A restart then
  appears to do nothing. See D-243 and X-07.
- There is no separate frontend dev server or build step — `frontend/` (`index.html`, `app.js`, `styles.css`, `logger.js`) is mounted directly at `/static` by `backend/main.py`.
- Interactive API docs available at `/docs` once running.

## Tests: smoke scripts, no framework

There is no test framework, linter/formatter config, or CI pipeline (no `.github/` either). Don't assume `pytest`, `ruff`, `eslint` etc. are available — check before suggesting a command that relies on them. **`pytest` is now installed in `venv`, and that is not an invitation**: it is there because `eval_test_automation.py`'s Python case needs a runner to execute the suite the model wrote for it. Nothing in this repo is a pytest test, and writing one would be the wrong shape (D-09). Tests are plain scripts that exit 0/1 and print a PASS/FAIL line per check (see D-09 in `DECISIONS.md`); new tests should follow that shape rather than introduce a framework. These ten scripts are the *entire* regression net — there is no CI and nothing else to fall back on, and the pass counts quoted in `DECISIONS.md`'s history table are this project's only record that a change was verified.

```
python scripts/smoke_products.py        # registry, entitlement, quota, metering, project scoping
python scripts/smoke_code_review.py     # chunking, gate floor, evidence verification, waivers
python scripts/smoke_review_sources.py  # zip/repo/branch intake, cost preview, per-file verification, VAPT substrate
python scripts/smoke_modernization.py   # run lifecycle, dependency order, crash/resume (much the longest)
python scripts/smoke_governance.py      # Core SDLC Review stages, reports, gate fan-in, swarm
python scripts/smoke_llm_providers.py   # admin LLM connections: key never returned, resolution order
python scripts/smoke_cli.py             # scripts/lmsdlc.py driven as a subprocess against a real server
node   scripts/smoke_render.js          # frontend result rendering (DOM-stubbed, no deps)
node   scripts/smoke_plan_picker.js frontend/app.js products.json    # Plans screen product picker
python scripts/smoke_app_testing.py     # app-testing: the action policy, step caps, assertion honesty
python scripts/ui_examine.py            # the real UI in a real browser — every review source, no model called
```

Six live drivers sit beside these. They are **not** part of the offline net: four of
them spend real money, and four write to a real repository — use a scratch one.
`verify_code_delivery.py` and `run_tests.py` are the odd ones: both write but call no
model, because the thing they verify is the delivery, not the generation.

```
python scripts/eval_code_review.py                       # accuracy, 3 runs per case (~$0.55)
python scripts/eval_code_review.py --repeat 1            # cheaper; cannot measure variance
python scripts/eval_test_automation.py                   # do the generated tests work (~$0.43)
python scripts/eval_test_automation.py --repeat 1        # cheaper; cannot measure variance
python scripts/eval_app_testing.py                       # explore an app, does the suite guard it
python scripts/eval_app_testing.py --self-check          # free: proves the harness, calls no model
python scripts/explore_app.py --url <app> --max-steps 40 # explore a running app, write a suite
python scripts/explore_app.py --url <app> --verify        # ...then run the suite against it
python scripts/explore_app.py --url <app> --dry-run      # ...what it would be offered, spends nothing
python scripts/explore_app.py --fixture                  # offline: no server, no key, no model
python scripts/verify_pr_review.py <pr-url>              # every GitHub write path, on a real PR
python scripts/verify_pr_review.py <pr-url> --dry-run    # ...the same, writing nothing
python scripts/verify_pr_review.py <pr-url> --cleanup    # remove what it posted
python scripts/verify_fix_apply.py <repo-or-pr-url>      # every Apply Fixes delivery, for real
python scripts/verify_fix_apply.py <url> --dry-run       # ...stopping at the preview
python scripts/verify_fix_apply.py <url> --cleanup --review-id <id>
python scripts/verify_code_delivery.py <repo-url>        # pushing generated code, for real
python scripts/verify_code_delivery.py <url> --dry-run   # ...refusals only, writes nothing
python scripts/verify_code_delivery.py <url> --cleanup
python scripts/run_tests.py --fixture                    # offline: no server, no key, no network
python scripts/run_tests.py --review <id> --into ../repo # write, run and report a generated suite
python scripts/run_tests.py --review <id> --dry-run      # ...printing every path and command only
python scripts/run_tests.py --review <id> --deliver <repo-url> --open-pr   # tests + CI workflow as a PR
python scripts/run_tests.py --review <id> --from-actions <run-url>         # pull what CI made of them
```

`eval_code_review.py` is the only thing that can tell an accuracy improvement from a
regression — the floors in it are measured, not guessed (D-245), so a prompt change that
looks better and scores worse gets caught. **A decision run is three runs per case**, and
the gate floor is applied only to cases that answered the same way every time: one case
returns Amber, Amber, Green on the same ten-line file, so pooling every run let a single
coin flip read as a regression (D-358, D-359). `--repeat 1` still works and says what it
cannot answer. `verify_pr_review.py` exists because the offline
suites stub the model and never touch GitHub: they cannot tell you that an inline comment
landed on the line intended, and the worst defect of the session was invisible until a real
diff went through it (D-244).

`eval_test_automation.py` is the only thing that can tell whether generated *tests* got
better or worse, and it asks the only question that means anything for a test generator:
does the suite go **red on broken code and green once it is fixed**. Every case therefore
ships twice — `source/` with a real bug, `fixed/` with the smallest correct fix — and the
same generated suite is run against both. Counting files rewards padding and diffing against
a reference suite rewards imitation; neither says whether the tests work. It needs a real key
and runs generated code, so it belongs on the same tier as `run_tests.py`, never the API
host. The floors are measured, not guessed (D-403), and it found a defect on its first run
that nothing offline could see (C-93).

Two cases, two languages: `ownership-node` (an HTTP API, an authorisation bug, `node:test`)
and `cancellation-python` (a service layer, a state-machine bug, pytest). The Python one
needs **pytest installed where you run the eval** — it is checked for before anything is
spent, because a missing runner and a suite that will not execute score the same. The spend
line reports **model cost, not what a customer would be billed**: since D-404
`usage_events.cost_usd` is the price, so comparing a baseline taken before that change with
one taken after would read as a nine-fold regression that never happened.

`run_tests.py` is the local half of the test-automation loop and the only driver that
runs *generated* code — on a developer's machine, never on the API host, which is the tier
D-05 named. It writes the generated files into a checkout (refusing any path a shell would
read, never overwriting without `--force`), runs them, and posts the JUnit XML to
`POST /api/reviews/{id}/test-results`, where the coverage the model claimed is checked
against what actually ran. The command it runs comes from `services/test_runner_plan.py`,
which also renders the GitHub Actions workflow — so a local green and a CI green mean the
same thing. `--fixture` is a real offline check and belongs in the net above in spirit: it
drives the write, run and merge path against a synthetic suite and compiles the report
script embedded in the generated workflow, which nothing else can see until a customer's
runner executes it — and even that is not enough on its own: two generated workflows were
rejected by GitHub before a single job started, for a reason no parser objects to (C-92).

`--deliver` and `--from-actions` are the two ends of the loop as it works **with no public
address**, which is the normal case for this deployment. `--deliver` pushes the tests *and*
the CI workflow to a new branch and opens a pull request; the workflow then saves its results
as its own artifact rather than posting them, and `--from-actions` fetches that artifact and
files it. Every call in that direction is outbound, so nothing has to be exposed and no
Actions secret has to be provisioned. Reading artifacts needs **"Actions: Read"** on the
token and writing `.github/workflows/` needs **"Workflows: Read and Write"** — both separate
from the Contents and Pull requests permissions everything else here uses, and a missing one
looks like a missing repository, because GitHub answers 404 for anything a credential cannot
see.

**Application-based testing is the sibling product, and it inverts the intake.**
`test_runner_plan.CANNOT_SUPPLY` names `running application` as a prerequisite the code-based
generator cannot conjure; `app-testing` is the case where that is the *input* and there is no
source at all. An explorer drives a real browser, the platform chooses one action per step,
and a Playwright suite is written from the journeys walked. The result has the same shape as
a code-generated suite on purpose — same renderer, same `plan_for`, same `deliver-tests`,
same results contract — so nothing downstream has a branch for it.

Four things to know before changing any of it. **The browser never runs here** (D-407): the
platform is sent page descriptions and answers with one action, so it never learns the URL,
never holds a credential, and an application behind a VPN works — which a hosted browser
fleet could not do. **The model picks from a screened list and never writes a selector**
(D-408): `services/action_policy.py` removes dangerous controls before the model sees them,
re-checks its choice, and is imported by the driver so the same rule applies where the click
lands; read that module before touching the denylist, because its first version allowed a
button called *Cancel subscription*. **A project can permit a named control the list
withholds** (D-429), which exists because measurement made it necessary: against a banking
demo the list withheld every money movement in three steps, and against a shop it withheld
`Remove` and `Checkout`, leaving six of fifteen supplied requirements untestable. Matching is
exact and never a substring, and the permitted list is **frozen onto the session and handed
to the driver** — the platform picks an index and the driver resolves it, so the two
sides screening to different lists is a click on an element nobody chose. **An assertion says what it rests on** (D-409): with no
requirement supplied a suite can only describe what the application already does, so a
`specified` claim made without one is downgraded to `observed` in code and the count is
reported.

**A session will not start without something to check the application against** (D-419):
a typed requirement, a Requirements or QA document on the project, or a finished Test
Scenario / Test Case run to cover — any one, and the third is optional and usually absent.
`services/app_basis.py` resolves it and `app_session.create` refuses beside the attestation.
Without this the product writes furniture tests: a live session opened with nothing supplied
produced eleven tests of which nine were "the button is visible". The same module decides
what a finished test may *claim* — `stated` for something a person wrote, `derived` for a
case this platform generated, `observed` for nothing (D-420) — applied as a ceiling in code,
because a machine-written case laundered into "we verified your requirement" is D-406 at one
remove. **What a page displays is encrypted, expires and can be refused** (D-418): the
`review_source_store` treatment, with 0 days meaning "wipe it when the suite is written", and
the consequence of refusing it stated on the screen *and* on the finished suite.

**A session can be watched, and stopped, from the screen.** `GET /sessions` lists sessions
rather than suites, which is the difference between seeing a run that produced nothing and
not knowing it happened; `renderAppSession` polls one session and says what it is doing now.
**A stop is served as the existing `done` reply** (D-423), checked before `require_open` and
before metering, so it needs no driver change, costs nothing, and is enforced by closing the
session row rather than by the driver agreeing. The loop detector in the browser (D-424) was
built because `app_session.stalled` could not see an A,B,A,B loop; **the server-side rule can
now** (D-441). Progress means reaching something the session has not reached before, rather
than the last five pages being identical — the old question could never be answered yes by a
session alternating between two known states, and a live banking run spent 22 of 26 steps
doing exactly that. Read D-441 before widening it: a signature carries each control's current
*value* so that filling a form reads as progress, which is precisely why the explorer could
always move the measure by typing something. What the denylist withheld is finally visible by name (D-426) — and
reads in both directions, because half a page withheld is a coverage problem, not a safety
win. An abandoned session is closed after six hours and its digest then expires on **the
window its customer chose** (D-425), which is also what repairs a D-418 promise whose SQL
excluded the only case its own docstring described.

**And an observation is what a page offers *and* what it says, in both cases read off the
DOM rather than inferred.** Three parts of `COLLECT_JS`/`TEXTS_JS` in `scripts/explore_app.py`
each exist because of a specific failure, so none of them is decoration. A page's landmark
text is collected and capped on arrival (D-416), because a suite written from controls alone
can assert which buttons exist and nothing about what the application did — a basket whose
total is wrong offers exactly the same controls as one whose total is right. Each candidate
carries **how to address it** and, for a submit control, **what request its form sends**
(D-415), both sanitised by the route, because `getByRole` resolves through the accessibility
tree and finds nothing for an `<li>` that merely behaves like a button. **The name it carries
has to be the one that tree computes** (D-427): the first two live sessions against public
applications each produced a suite in which every test timed out on the shared sign-in helper,
because an `<input type="submit">` was named from its `name` attribute rather than its `value`.
Read that row before touching the name chain — a name that looks plausible and resolves to
nothing is the whole failure, so the fixture asserts that Playwright finds exactly one element
rather than that the string looks right. What a page *says* had the same
problem, and `TEXTS_JS` now collects from **the element that renders the words** rather than
from whatever carries an `id` (D-431) — on a live catalogue the old selector matched 34
elements, none of them a heading, and sent the navigation run together as one line while
every product name and price went missing. Read that row before widening it again: inline
markup must not count as a child, candidates are excluded by the stamp `COLLECT_JS` already
leaves, and the driver's cap and `app_session.clip_texts` have to move together. And a clickable
element is a candidate even when it is not a control (D-414): pointed at this platform's own
interface, the explorer was offered **two** controls after signing in — the whole navigation
is `<li data-page=…>` with a click handler — and spent twenty-two of twenty-five steps
guessing URLs. Related: a password field reports *that* it holds a value and never what
(D-413), because an explorer that cannot tell its own typing landed types it again instead of
pressing Sign In, which is how a live run failed to get past a login form at all.

**What each action *did* reaches the suite writer from the step that reported it**
(D-430, and D-428 for the same fault on screen). An outcome is only knowable by looking at
the page again, so it is stored against the *following* step — which meant the generation
input told the model that clicking **Login** resulted in "filled Password; still on /".
Signing in went nowhere, as far as the writer knew, so it invented a landing condition for
the sign-in helper and **every test in the suite failed on that one line**. Relatedly,
`run_tests.py` now says when most failures share one call site (D-432): an all-red suite is
what a broken application looks like, and a helper that cries wolf costs the reader more than
a missing test.

**The session now runs the suite it wrote, against the application it wrote it from**
(D-435, `--verify`). This is the cheapest check in the product and it went missing for
months: *"green on the application you explored"* was a sentence in the prompt, and four
separate defects shipped suites that were red against a working application (D-427, D-430,
D-434, C-101), each found by a person running them by hand days later. The inference is what
makes it cheap — exploration only reaches generation because every action was answered, so a
test that fails now is wrong about an application that was working minutes ago. It shells out
to `run_tests.py` rather than reimplementing it, because a second copy of a delivery path is
how D-364 happened.

**The collector sees inside open shadow roots, and says when it cannot see into a frame**
(D-436). Measured on a four-control page before the change: **1 of 4** offered and none of
the text behind either boundary, reported as a success. Angular, Lit, Stencil and Salesforce
build whole interfaces out of custom elements, so the applications most worth testing were
the ones coming back emptiest. Read D-436 before touching `deepAll` — it is written out
twice on purpose, and frames are deliberately still out of scope because reaching into one
changes what an address *means* in this contract.

**Two audits read the finished suite, and they answer different questions.**
`locator_audit` (D-433) asks whether a locator could have come from the session — the
contract sends roles, names and page text and never a class, an id or a test id, so anything
else was recalled. Given the session's own collected text it also catches a **heading nobody
saw** (D-438), which is D-430's failure and is invisible to shape alone. `assertion_audit`
(D-437) asks whether a test could ever fail: one whose every matcher is `toBeVisible` or
`toHaveURL` passes against an application with the feature ripped out, which is worse than a
missing test because it is counted as coverage. Neither can see a *wrong value*, a wrong
credential, or an invented fixture — that is what `--verify` is for, and the three are
complements rather than layers. **Do not read two passing audits as a working suite**: a
healthy SauceDemo session scored 144 grounded locators and 0 invented while 10 of its 21
tests failed against the working application (C-109).

**A locator the session never observed is counted rather than forbidden** (D-433). The
observation contract is closed — a role, visible text, or `locator("input[name='…']")`, and
nothing else — so a class, an id or a test id in a finished suite was recalled from some
other application by construction, and `services/locator_audit.py` says how many. This exists
because the prompt had forbidden CSS selectors all along and a live suite contained nineteen
of them: a rule the model already has and does not follow is not fixed by repeating it. Read
that row before widening the check — the line is drawn at *application-specific* tokens and
not at CSS, because narrowing with `tbody tr` before asserting inside a row is what the
prompt itself asks for. One retry, refused if the second answer lost a file or a test, since
the cheapest way to stop using class selectors is to delete the tests that used them.

`eval_app_testing.py` asks the same question `eval_test_automation.py` does, aimed at the
narrower claim this product actually makes: explore the **working** application, then replay
the suite against both it and a broken build — green on one, red on the other. Its
`--self-check` runs a hand-written reference suite through the whole harness for free, so
"the harness cannot separate these variants" is never discovered after paying for three
sessions; it covers **every** case and **fails a case that has none**, so the free proof
cannot quietly stop covering new ones.

Three cases, chosen as three *shapes* rather than three samples. `task-ownership` is
server-rendered with an authorisation bug, and its separating assertions are negative ones —
"must be refused", "must not be offered". `basket-total` is client-rendered: the URL never
changes, every click rewrites part of the page, and the arithmetic bug is invisible at a
quantity of one, so an explorer that adds a single item writes a suite green on both builds.
`signup-wizard` is the depth case — a three-step form whose defect no single page shows,
because each step is correct in isolation and only a completed journey reaches the review
page that is wrong. Still a count and not a percentage (C-95), and note what none of them
measures: all three are ordinary semantic HTML, so the collector work in D-414 has the
fixture and a live run as its only evidence (C-97).

**The measured baseline is 9 of 9, and the headline number is the least informative part
of it.** 2026-09-17, PROMPT_VERSION 2026-09-17.3, three cases x three runs: `discriminates`
**9/9** at **$0.135 a session**, every session producing a suite, every case seeing the
break, and no test failing on the working application. It reached 8 of 9 twice earlier the same day, and every one of those misses was
`basket-total` — the only client-rendered case, now measured 3/3, 2/3, 2/3, 3/3 across four
runs. Read it as a case living around two-thirds to three-thirds rather than as one that has
been fixed. The runs that scored lower are the ones that earned the day: the first found
D-434, where a single session had **all eight** of its failing assertions wrong by exactly one
step, and the second still carried one noisy test.
**The denominator is every session attempted**, deliberately: counting only the sessions that
produced something is the scoreboard committing the flattering-number error the eval exists
to catch — and a single test failing on the good build is enough to lose the case, which is
the strictness this measure is for.

**The step counts are the part to read before assuming the collector made sessions dearer.**
More page text makes a signature more sensitive, so the no-progress rule fires later and a
session ought to run longer — and `basket-total` did drift from 7-8 steps to 9-10. But
`task-ownership` fell from **14-25 steps to 8-11**, because an explorer that can read a page
stops wandering to find out what is on it, and that is most of why the price per session went
*down* while the suites grew: `basket-total` writes 10-11 tests where it wrote 7-8, 7-9 of
them separating where 5-7 did. Floors stay at 0.66 on all three measures for all three cases —
one session below a measured 3/3. Raising them to 1.0 would fail the suite on a single flake.

The first baseline that morning was **33%**, and the gap is the work rather than a prompt
tidy-up. Each defect between the two was found by a run and none was visible offline: a
"must be refused" test that drove controls existing only on the *broken* build, so it timed
out on the good one; an explorer that could see no page text and therefore could not assert
any value (D-416); a login it could not get past at all, because a password field reported
itself empty however much had been typed in (D-413); a cleanup hook that signed in again and
hung; and a suite of **zero test files** stored as a success because its summary was
confident (D-417). Two more came from pointing it at this platform's own interface rather
than at the corpus: navigation invisible to the collector (D-414) and a session quote whose
ceiling sat *below* its own forecast (D-412).

Read the 9/9 as "nothing here is broken", not as a claim the product is finished. Three cases
is a count and not a percentage (C-95); none of them exercises the collector those numbers
depend on (C-97), which is exactly why D-427 and D-431 were found by a live run and not here;
and a live run with no requirement supplied still produced a suite that was nine-tenths "the
button is visible", which is C-66 wearing a new hat and is why `_HOLLOW` in the eval was
widened. Five findings from those live runs are open: C-100 and C-101 name what the collector and
the locator audit still cannot see, C-103 is the same mistake as C-101 aimed at a value
instead of a selector, C-104 is a frame's contents, and C-102 is the customer's call rather
than ours.

`ui_examine.py` is the only one that drives a browser (Playwright + Chromium, both already in
`venv`). It starts its own server on a spare port with its own database and **calls no model** —
every case stops at the free cost estimate. `--with-review` runs a full review with the gateway
stubbed, `--headed` shows the browser, `--shots DIR` saves a screenshot per case, and
`--url http://localhost:8000` examines a server you are already running. It exists because the
DOM-stubbed `smoke_render.js` cannot see whether a button is wired to a function, whether an
element id in `index.html` matches the one `app.js` reads, or whether a click reaches the
server — and that gap is where the defects reported on 2026-09-02 were.

**Windows note:** run the Python ones with `venv/Scripts/python.exe`, not a bare `python` — the
system interpreter has none of the dependencies installed. Add `PYTHONIOENCODING=utf-8` when you
need to read output containing the em dashes and emoji these scripts print.

The Python ones drive the real app through FastAPI's `TestClient` against a throwaway SQLite file with `llm_gateway.call_tool` stubbed, so they need no server, no API key and no network — they exercise routing, auth, entitlement, quota and persistence, but never the model itself. Two exceptions worth knowing before you run them:

- `smoke_llm_providers.py` stubs the *provider* functions (`_call_anthropic` / `_call_openai`) rather than `call_tool`, because the resolution logic under test lives inside `call_tool` — stubbing that would stub out the thing being tested.
- `smoke_plan_picker.js` takes a `products.json` argument (a saved `GET /api/v1/products` response) and **there is no such file in the repo**, so it dies with `ENOENT` until you capture one from a running server. `smoke_render.js` needs no argument — it defaults to `frontend/app.js`.

Two of the scripts are neither tests nor throwaway tooling, and both exist because a
caveat asked for evidence rather than an edit:

- **`scripts/measure_event_loop.py`** answers C-31. It starts a server whose model gateway
  sleeps, fires one review, and times `GET /` alongside it — so "does a long review freeze
  the server" has a number instead of an opinion. It found that it did (`GET /` blocked
  5.45s of a 6s review) and confirmed the fix (0.30s). Offline, no key. Re-run it before
  claiming anything about concurrency.
- **`scripts/lmsdlc.py`** is the CLI, and the thing CI actually runs. `--fail-on red` is
  what makes a review a build step; `--sarif` is how findings reach GitHub's Security tab
  without a GitHub App. `docs/ci/` has ready workflows for GitHub Actions and GitLab CI.
  `smoke_cli.py` drives it as a subprocess, which is the only way to check an exit code.

Everything else under `scripts/` is tooling, not tests; don't run it expecting a free offline check. `eval_code_review.py` measures the agent against a labelled corpus and **spends real money**. `build_run.py` compiles a converted codebase with the local .NET SDK and posts the verdict back — the app itself prints that command into modernization checkpoints and the UI (`build_results.py`, `runtime.py`, `frontend/app.js`), so it has to keep working. `drive_modernization.py` walks a run through the live HTTP API as the browser does; `audit_source_analysis.py` reports what the deterministic layer concludes about a zip before any spend.

## Architecture

- `backend/main.py` — FastAPI app entrypoint, mounts `/static` → `frontend/`.
- `backend/agents/` — per-module agent implementations (`architecture_intelligence/`, `developer_intelligence/`, `devops_intelligence/` [stub], `governance_intelligence/`, `modernization_intelligence/`, `operations_intelligence/`, `qa_intelligence/`, `requirement_intelligence/`, `security_intelligence/`), all extending `base_agent.py`.
- `backend/routes/` — FastAPI routers (one per concern: `agents.py`, `auth.py`, `swarm.py`, `v1_agents.py` [external API surface], etc.).
- `backend/services/` — business logic; `orchestrator.py` (`SDLCOrchestrator`) is the central coordinator. Also: `llm_gateway.py`, `github_service.py`, `jira_service.py`, `guardrail_service.py`, `agent_guard.py` (cost/budget control), `knowledge_base.py`, `crypto_service.py`.
- **The review pipeline.** Code Review and VAPT share one implementation, and it is worth
  knowing the pieces before changing either: `source_bundle.py` turns any input (paste, diff,
  zip, GitHub repo at a ref, PR, `base...head`) into one `Bundle`; `code_chunker.py` splits it;
  `backend/agents/chunked_review.py` fans the chunks out and assembles the result;
  `finding_pipeline.py` verifies each finding's quote against the file it names, de-duplicates,
  suppresses test/vendor paths and fingerprints; `finding_gate.py` floors the gate so a model's
  own verdict can only ever be tightened; `suppression.py` reads the repository's own
  `.lmignore` and inline `lm-ignore` comments, which is carried on the `Bundle` and applied
  at the same stage as the built-in test/vendor path filter — **an ignore rule lowers the
  gate and never removes the finding**, because otherwise a commit could turn a Red release
  Green and the audit trail would agree with it (`docs/suppression.md`). `review_estimate.py` prices a run before it happens,
  reusing `modernization/estimate.py`'s arithmetic so a quote and an invoice come from one
  price table. `sarif.py` exports a finished review in the format GitHub code scanning and
  most security dashboards read.

  **A push reviews only what the push added.** `workflows/review_pr.py` takes a
  `since_sha`, and `routes/github_webhook.py` supplies it on `synchronize` only — so an
  active pull request costs one slice per push rather than a full review per push. The
  property that makes it safe is `_carry_forward`: still-open findings from earlier reviews
  of **the same pull request** (`database.open_findings_for_pr`, keyed on `pr_number`) are
  appended to this run's findings, labelled `carried_forward`, and re-floored through
  `finding_gate.reconcile_gate` — because a run that never opened a file cannot see the
  Critical in it, and an absent finding otherwise reads as a fixed one. Read D-341 before
  changing any of it; the fallbacks are wide on purpose.

  **Every other assessment gets `attest`, and the judgement is which protections are
  universal.** `backend/agents/attest.py` gives a single-call agent the two things
  `chunked_review` provided and nobody else had: a degenerate response (no findings, no
  summary, non-trivial artifact) becomes an **error** rather than a stored clean result, and
  the gate is floored against the findings the agent itself reported. Applied in the three
  shared methods of `base_agent.py`, which between them cover the requirement, prompt,
  compliance, security and architecture products, plus the three regulated ones wired
  directly. **A subclass that overrides `_call_claude` is not reached by that edit** —
  `sensitive_data_agent` was exactly that case and needed its own (D-346); check before
  assuming a new agent is covered. `ai_provenance`, `issue_triage` and `migration` return
  no findings and no gate, so `attest` is a genuine no-op for them and they are left alone.

  **Evidence verification is opt-in and must stay that way.** Only
  `CODE_REVIEW_TOOL_SCHEMA` and `VAPT_TOOL_SCHEMA` demand a verbatim quote, and both already
  chunk. Every other schema asks for something else — `REVIEW_TOOL_SCHEMA` has no evidence
  field at all (`_code_review_schema` says why in as many words), and the Part 11 verifier
  asks for "File path / function / code pattern", a *description*. Running a substring check
  over a description discards a correct finding for being phrased as instructed. Turning it
  on for a product means changing that product's schema first, which changes what the model
  answers — measure it, do not assume it (D-344).

  **A regulated system too large for one call is now assessed in parts, not refused.**
  `cfr21_agent.verify` takes one call when the prompt fits — identical output, no extra
  cost for the common case — and otherwise goes through `chunked_review` with two things
  set differently from a code review: `verify_evidence=False`, because this schema asks for
  a *description* of evidence and a substring check over one discards correct findings
  (D-344); and `merge_extra`, because the four §11 assessments are objects and only this
  agent knows that one is applicable if **any** part found it applicable and compliant only
  if **every** applicable part said so. `review_estimate.for_regulated_files` prices it
  first, and both intake shapes have a free preflight — `/api/v1/agents/cfr21-verification/preflight`
  and `POST /api/review/cfr21-verification/preflight-zip`, the second because the browser
  uploads a zip and a quote only on the JSON API is a quote nobody sees. See D-354.

  **And no assessment may claim coverage it does not have.** `cfr21_agent` used to
  back-fill `files_scanned` from its own input when the model did not say what it read, so
  an FDA 21 CFR Part 11 report could state that 400 files were verified when nobody knew.
  `attest.coverage_claim` reconciles the claim against the input and states the gap
  instead — including paths the model named that were never supplied, which is usually an
  invented filename.

  **One screen reviews everything, including pull requests.** The separate GitHub PR screen
  is retired (D-267); its nav item opens the review screen with the source pre-set, and the
  post-back options appear only for a pull-request source. The *products* are still two: a
  run that will write to GitHub goes to `/api/github/pr/async` (`github-pr-review`), one that
  will not goes to `/api/review/code/source` (`code`) — merging the screens deliberately did
  not merge the entitlement, quota or budget.

  Two behavioural notes that surprise people: a review over four model calls returns **202 and
  a job id** (`/api/review/jobs/{id}` for progress) rather than blocking; and every screen
  asks for a **cost quote first** — `/preflight` is free, calls no model, and the run button
  names the ceiling it was approved against.

  Two more pieces sit behind a *finished* review. `review_source_store.py` records which
  files it read, which it did not and why (in its own two tables, never in `result_json` —
  see D-256), and keeps the source itself for a per-run retention window: 7 days by default,
  encrypted, and **never for a GitHub source**, which is re-read from the repository at the
  commit sha the review recorded. `fix_apply.py` is Apply Fixes — it starts from a review and
  the findings a person ticked, reuses the per-file fixer and *both* of Auto-Fix's guardrails,
  and delivers as a diff, a zip, a branch, a new pull request, or a commit into the pull
  request being reviewed, depending on what the source was.

  **It works from a VAPT assessment as well as a code review, and nothing about it is
  review-type specific** — `/api/reviews/{id}/fix` takes any review in scope. What differs
  is who re-reads the patch: `fix_apply._review_agent` maps the review to its own reviewer,
  and `_verify_patch(..., review_agent=)` sends a VAPT fix to the vulnerability assessor.
  That one mapping also picks the price basis, so the quote and the guardrail cannot
  disagree about which agent a review belongs to. Four things to hold onto before changing
  any of it: a fix costs **per file, not per finding**; both calls per file carry the whole
  file, so the quote sums **two half-quotes against two different agents**
  (`review_estimate.combine`); the re-review blocks only on a Critical the *original review
  did not report*, matched by fingerprint (D-303); and nothing in the review pipeline ever
  compiles or runs the code it produces.

  **That last point is not theoretical, and it has a specific shape.** A live fix closed six
  real vulnerabilities and produced a file that would not import, because
  `autofix_agent._fix_one_file` sends **one file per call and nothing else** — so the model
  invents the helper it needs rather than using the one already in the project (D-316). Read
  that row before changing the fixer's prompt. Relatedly, Apply Fixes' `pr` and `branch`
  deliveries were broken from the day they shipped (D-314, D-315) and no offline test could
  see it: every fix test stops at `plan`, which writes nothing. **`verify_fix_apply.py` is
  the equivalent for fixes** — it drives all five deliveries plus the preview against a real
  repository, and `smoke_review_sources.py` §31 fails if a delivery is added to
  `fix_apply.OUTPUTS` without a stage in it (D-362). `same_pr` is opt in: it commits onto a
  branch somebody may have checked out.

  **Generated code has its own way out, and it is deliberately narrower.** Codegen Scaffold
  and Migration Execute store their files on the review row, and
  `POST /api/reviews/{id}/deliver-code` pushes them through `services/code_delivery.py`: a
  **new** branch that must not already exist, a pull request only if asked for, and never a
  commit onto an existing branch. Apply Fixes may do the last of those because a review had
  already read that branch; nothing has read a scaffold unless `auto_review` was ticked, and
  the pull request body says so where whoever merges it will see it. `_DELIVERABLE_RUNS` in
  `main.py` is a deny-by-default list of review types, not a guess from the shape of
  `result`. The one thing shared with `fix_apply` is `resolve_base_branch`, because that is
  where D-314, D-315 and D-364 all landed and a second copy in a second writer is how they
  come back. **`verify_code_delivery.py` is its live driver** — offline tests of a write path
  stop before writing (D-317), so a new writer starts with exactly that exposure.

  **Its first live run found three more, and one of them had left the whole feature
  non-functional for eight commits** — `_available_outputs` called with two arguments
  against a one-argument definition, so every `apply` and `deliver` raised `TypeError`
  before reaching a delivery; a GitHub zipball carrying only **7 characters** of the commit
  sha, which `POST /git/refs` rejects with a bare 422; and a base branch derived by
  splitting a label on `@` that isn't there, which returns the repository name. All three
  are fixed and re-verified against a real repository, `smoke_products.py` §21 now guards
  the cheapest class of the first, and D-364 has the detail. The lesson is the one D-317
  recorded: **read that row before trusting an offline test about a write path.**
- `backend/services/modernization/` — the one subsystem with its own schema and its own durable
  execution model: whole-codebase conversion runs (ingest → dependency graph → charter → claim-and-lease
  worker loop → Tier-0 validation). Its DDL lives in `store.py` and is called from `database.init_db()`.
  Read its package docstring before changing anything in it.
- `backend/database.py` — raw SQLite data layer, no ORM, no migration tooling. DB file defaults to `sdlc_agent.db` at repo root.
  **New columns go in by `ALTER TABLE` inside `init_db`, wrapped in `try/except
  sqlite3.OperationalError`** — `CREATE TABLE IF NOT EXISTS` never revisits a table that
  already exists, so a column added to the DDL alone is missing on every existing database.
  There is also **no scheduler in this deployment**, so anything time-based runs at startup
  *and* on the read path that depends on it: `lapse_expired_waivers` (before every baseline
  lookup), `review_source_store.sweep_expired` (on every inventory read), and
  `notify.dispatch_pending` (at startup, kicked after a review saves, and on every read of
  the delivery log). Notifications are the clearest case of what that costs:
  `save_review` writes the delivery row on **its own cursor**, so it commits with the
  review and cannot be lost, and a background thread then attempts it — a row in flight is
  `sending` and `notify.recover_sending()` puts it back at startup, the same way
  `recover_orphaned_jobs` reaps job rows. What none of that buys is promptness after a
  crash, and `backend/services/notify.py` says so at the top rather than leaving it to be
  discovered.
- `backend/config.py` — env var loading (see below).
- **What a run cost reaches the browser two different ways, and you need to know which.**
  `agent_guard.guarded_call` attaches `_budget_stats` to the response *body* — that covers
  the `/api/v1` surface and every 202 job route. The synchronous `/api/review/*` routes
  cannot: `legacy_guard` meters them *around* the handler, so by the time the cost is known
  `call_next` has already serialized the body. Those report it in `X-Spend-*` response
  headers, which `apiFetch` folds into `_budget_stats` (body wins if both are present).
  Either way the figure comes from `agent_guard._price_usage`, the same arithmetic that
  writes `usage_events` — derive a display cost any other way and the screen will disagree
  with the invoice. `renderReviewTransparency` is the one renderer that draws it, and every
  result renderer calls it.

  **A third way, for a run that has already finished.** Neither of the above can reach a
  *stored* run: the cost is not known until the model calls end, which is after
  `save_review` has written the row. So it is not stored on the review at all — it is read
  back out of the billing ledger by `database.get_review_spend`, which `GET /api/reviews`
  and `GET /api/reviews/{id}` serve as `spend`, and `openAgentRun` folds into the same
  `_budget_stats` shape so the same renderer draws it. The link is
  `usage_events.review_id`; `legacy_guard` learns which review to name via
  `context.collect_saved_reviews()`, which is the one contextvar in `backend/context.py`
  that reports **back up** rather than down — and it has to hold a mutable list, because a
  synchronous endpoint runs in a worker thread with a copy of the context (D-311). A run
  with no ledger row reports **no cost rather than zero**.
- **`AGENT_SCREENS` in `frontend/app.js` is where an agent screen says what it is.** One row
  per screen keyed by its nav `data-page`, giving the product id, the `reviews.review_type`
  it writes, its result container id, its renderer function and what to call its runs. The
  generic machinery reads it: the per-agent "Previous runs" list, which renderer reopens a
  stored run, whether the Apply Fixes checkbox may appear, and whether the retention control
  is telling the truth. **A new agent screen must add a row** — `smoke_review_sources.py`
  section 26 fails otherwise, and also fails if a row disagrees with `GET /api/v1/products`
  or with the nav's `data-product`. Three screens are deliberately absent, each for a stated
  reason in the table's own comment (modernization and the governance report have their own,
  richer histories; the GitHub PR screen is retired).
- `docs/` — per-agent reference docs and integration guides (`n8n-workflows/`, `vscode-integration/`).

## Environment variables

Required: `ANTHROPIC_API_KEY`. **`GITHUB_TOKEN` is now a fallback, not the only option** —
per-project credentials live in the `github_credentials` table (encrypted, managed at
`/api/v1/github-credentials`, settable from the review screens) and take precedence over it;
see D-236. Optional: `GITHUB_TOKEN` (fine-grained PAT, "Pull requests: Read and Write" — needed for GitHub PR/Auto-Fix features; public repos/PRs/issues work without it. **Also "Contents: Read and Write"** if you use the modernization PR workflow, which pushes a converted tree to a branch rather than only commenting), `DATABASE_PATH` (default `sdlc_agent.db`), `CLAUDE_MODEL` (default `claude-sonnet-4-6`), `MAX_TOKENS`, `APP_ENV`, `SECRET_KEY` (JWT/crypto — must be changed for production). Also present in `backend/config.py` but not in `.env.example`: `JIRA_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN`/`JIRA_PROJECT` (optional Jira integration), `DEFAULT_ADMIN_PASSWORD` (defaults to a hardcoded value — flag this if touching real deployment config), `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY` / `GITHUB_APP_PRIVATE_KEY_PATH` (optional — the only credential GitHub accepts for a **check run**, which is the only way a finding lands on a diff line; unset means the commit-status path, unchanged. `docs/github-app.md`, D-343), `INCREMENTAL_PR_REVIEW` (default **true** — a push to an open pull request reviews only the commits since the last review; see D-341 for the trade it makes and why the gate stays correct), `COST_OPTIMIZATION_ENABLED`, `MAX_AGENT_INPUT_CHARS`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `PUBLIC_BASE_URL` (only for the modernization build loop — the address a GitHub Actions runner posts diagnostics back to; it cannot be derived from the request behind a proxy).

## Windows-specific note

`requirements-explorer.txt` holds Playwright and is **deliberately separate from**
`requirements.txt`: `backend/` imports none of it, and installing a browser toolchain on
the API host would be paying for a capability D-05 forbids that host from using. It covers
`scripts/explore_app.py` and `scripts/ui_examine.py`, which had the same undeclared
dependency long before the first of those existed.

`requirements.txt` deliberately pins `litellm<1.92.0` — from 1.92.0 onward litellm ships a Rust extension with no prebuilt Windows wheel, which would otherwise require an MSVC toolchain to install. Don't bump this pin without checking Windows wheel availability. `headroom-ai` (cost optimization) also compiles a Rust extension; if it fails to install, cost-optimization compression fails open (no-ops) rather than breaking the app.
