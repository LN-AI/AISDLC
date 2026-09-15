# Application Test Generation

Explores a **running application** in a real browser and writes a Playwright suite from the
journeys it walked. It needs no source code — the running application is the input.

This is the sibling of [Test Automation Generation](test-automation.md), and the difference
is which thing the customer has. That product reads a repository and stops where a test needs
a live system: `test_runner_plan.CANNOT_SUPPLY` names `running application`, `database`,
`credentials` and `seeded test data` as prerequisites it cannot conjure. This product is the
case where the first of those stops being a gap and becomes the input.

| | |
|---|---|
| **Module** | `qa_intelligence` |
| **Product id** | `app-testing` |
| **Writes** | `reviews.review_type = "app_testing"` |
| **Endpoints** | `POST /api/v1/app-testing/sessions`, `.../step`, `.../finish`, `.../generate` |
| **Preflight** | `POST /api/v1/app-testing/preflight` (free, calls no model) |
| **Driver** | `scripts/explore_app.py` |

---

## Where the browser runs, and why it is not here

**The platform never opens a browser, never fetches your URL, and never receives your
credentials.** Exploration runs wherever the application is reachable — a developer's
machine, a CI runner, or a box inside your network — and talks to the platform through a
contract:

```
YOUR NETWORK                                  THE PLATFORM
  explore_app.py
    ├─ reads the page's accessibility tree
    ├───── POST /sessions/{id}/step ─────────►  screens the candidates
    │       (roles and names, no URL)            model picks one
    │◄──── { action, journey } ───────────────   screens its choice again
    ├─ screens a third time, then clicks
    └─ (loop, to a hard step cap)
```

Three consequences, and they are the reasons this shape was chosen over hosting a browser:

- **An application behind a VPN works.** The browser is already inside the network. A hosted
  fleet could never reach it, and UAT environments are usually *more* locked down than
  production, not less.
- **There is no SSRF surface.** A platform that never makes the request needs no guard for
  it.
- **D-05 is untouched.** Nothing compiles or runs on the API host, because no browser is
  launched by that process at all.

The cost is honest and worth stating: you run a command. There is no Run button on the
screen, because one would work for public URLs and silently fail for every internal one.

---

## What it will and will not click

An explorer that clicks *Delete account* causes damage no retry undoes. Three properties
bound it, and none of them is "the model was told not to":

1. **A closed action vocabulary.** Six verbs — `click`, `fill`, `select`, `press`, `goto`,
   `back`. Nothing else is interpreted, so there is nothing to escape into.
2. **The model never writes a selector.** It is given a numbered list of candidates and
   returns an *index*. It cannot name an element nobody offered it. This also means every
   locator in the generated suite is a role-and-accessible-name locator, which survives a
   redesign where a CSS path does not.
3. **Screened three times.** Dangerous candidates are removed *before* the model sees the
   list, its choice is re-checked on the way out, and `scripts/explore_app.py` checks a third
   time on the machine that will do the clicking. The first two mirror `finding_gate`
   flooring a model's verdict: the choice can only ever be narrowed.

Withheld by default are controls whose accessible name suggests they **destroy data**, **spend
money**, **contact real people** or **cannot be undone**, plus anything that ends the session
(`Log out`). `Cancel` on its own is allowed — it is how a person backs out of a dialog —
while `Cancel subscription` is not.

> **This is a seatbelt, not a wall.** It matches words in an accessible name. A button called
> *Finalise*, an icon with no accessible name, or a label rendered as an image will not match
> and *will* be offered to the model. The thing actually protecting you is that a session
> refuses to start without an attestation that the target is a test environment. Point it at
> one.

`POST /api/v1/app-testing/preflight` returns the full policy, so you can see the list before
committing to a session.

---

## The field that decides what the suite is worth

Supplying `requirement` is optional and it changes the product.

**Without it**, the explorer has only the application to go on, so it can only assert *what it
saw*. That produces **characterization tests**: "this is what the app does today". They catch
a regression well and catch a pre-existing bug not at all — if the application was wrong on
the day it was explored, the suite now asserts the wrong behaviour and goes green forever.

**With it**, an assertion can be checked against something.

Every test therefore declares its basis in `assertion_basis`:

| `basis` | Means |
|---|---|
| `specified` | It checks a requirement you supplied, and names which one |
| `observed` | It records what the application did during exploration |

A `specified` claim made when **no requirement was supplied at all** is downgraded to
`observed` in code, not merely discouraged in the prompt, and the count of downgrades is
reported as `assertions_downgraded`. Requirement-coverage rows claimed with no requirement
behind them are dropped entirely. This is `attest.coverage_claim`'s rule — a model's claim
about its own output is not evidence — applied to a new noun.

---

## Running a session

```bash
pip install -r requirements-explorer.txt
python -m playwright install chromium

# See what it would be offered and what it would withhold. Spends nothing.
python scripts/explore_app.py --url https://staging.example.com --dry-run

# Explore, then generate.
python scripts/explore_app.py \
    --url https://staging.example.com \
    --requirement-file requirement.txt \
    --app-user demo --app-password demo \
    --max-steps 40 --shots out/ --trace out/trace.zip
```

`--fixture` is an offline self-check: it drives the observe / screen / act path against a
bundled page with no server, no key and no model, and exits 0/1 like the `smoke_*` scripts.
Run it before spending anything on a new machine.

**Screenshots, video and traces stay on the machine that ran the browser.** The platform
stores none of them — it has no binary storage at all, and video of a real login screen is an
unresolved data-handling question rather than a missing feature (the same reason D-377
deferred video for the code-based product). The driver prints the directory.

---

## When a session stops

Three bounds, two of which cost nothing to evaluate:

- **The step cap**, enforced by the platform rather than trusted from the driver. Every step
  is a model call, so this is what makes a session's price knowable in advance.
- **No progress** — if the last five steps reached no page the session had not already seen,
  it stops. The page signature generalises numeric and id-like path segments, so `/orders/41`
  and `/orders/93` are one page. It does not try to answer "is this the same page?"
  reliably; it answers "has anything changed?", which is enough and is decidable.
- **The model says `done`.**

A choice that gets refused does **not** consume the step budget.

---

## What a session costs

Priced as one small call per step plus one larger call to write the suite, quoted together by
`review_estimate.combine`. **Replaying the finished suite calls no model at all** — that is
the shape worth knowing: pay once to discover, then replay on every deploy for the price of
CPU.

As of 2026-09-15 this product has **no pricing row**, so it bills as pass-through model cost
like every other unpriced product. That is deliberate rather than forgotten: D-404 set the
code-based product's fee and markup against a measured baseline, and the same will be done
here once `scripts/eval_app_testing.py` has measured what a real session costs.

---

## Running the tests it writes

Exactly like the code-based product, because the result has the same shape:

```bash
python scripts/run_tests.py --review <review-id> --into ../your-checkout --install
```

`GET /api/reviews/{id}/runner` renders the command and a GitHub Actions workflow;
`POST /api/reviews/{id}/deliver-tests` commits the suite and the workflow to a new branch;
`POST /api/reviews/{id}/test-results` takes the JUnit XML back and reconciles it against what
the suite claimed to cover. The Playwright row in `test_runner_plan` installs its own browser,
so `browser` is the one prerequisite that never appears in `must_supply`.

The suite reads its address from `process.env.BASE_URL`. Nothing is hardcoded, because the
same suite has to run against staging and against a laptop without being edited.

---

## Guardrails applied

- **Entitlement, budget and quota** through `agent_guard`, on every step and on the
  generation.
- **The action policy**, screened on both sides of the model and again in the driver.
- **The attestation**, checked before a session row is written.
- **The degeneracy guard** (D-405) — a generation with no test files *and* no summary is an
  error, not a stored run that reads on screen as a suite above an empty list.
- **Row-shape filtering** (D-392) — every model-written array is filtered to objects before
  any field access touches it.

## What it cannot do

- See anything the interface does not expose. No source, no database, no logs.
- Test a desktop application, or anything without a web interface.
- Know that current behaviour is *correct*, unless you supply a requirement.
- Guarantee it will not click something consequential. See the seatbelt note above.
- Tell you its suite passes. Nothing here runs anything (D-05); `execution_status` is
  `not_executed` until somebody reports a real result.

## Frameworks / controls referenced

None. This product generates tests; it files no findings and asserts no compliance control.
