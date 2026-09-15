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
   returns an *index*. It cannot name an element nobody offered it. The driver also works
   out how each control should be addressed and sends that along, so the generated suite
   uses a locator read off the page rather than one inferred from a role — which matters
   because `getByRole` resolves through the accessibility tree and would find nothing for a
   nav item built as an `<li>` with a click handler, however much it looks like a button.
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

## What it can see

Two things per page, and the second one is what makes an assertion possible.

**What the page offers.** Every interactive element, with the role and accessible name a
screen reader would read — plus anything the application has *made* clickable without making
it a control: an element with a click handler, a focusable element, or one styled with a
pointer cursor. That last part is not a nicety. Applications routinely build navigation from
plain list items with click handlers, and an explorer that cannot see those has nothing to
click: pointed at this platform's own interface it was offered two controls after signing in,
and spent the rest of its budget guessing URLs.

**What the page says.** A short digest of the page's landmarks — headings, live regions and
short identified text — each labelled with how to address it (`heading:`, `status:`,
`text:`). Without it a suite can assert which buttons exist and nothing about what the
application *did*, and most defects show up as a wrong value rather than a missing control: a
basket whose total is wrong offers exactly the same controls as one whose total is right.

It is a digest, not a copy of the page: capped in both the number of lines and their length,
by the platform on arrival rather than by the driver, because it is model input whose size
your application decides.

**Two things it never sends.** The URL of your application, and the value of any password
field. A password box reports only that something has been typed into it — which it has to,
because an explorer that cannot tell its own typing landed types it again instead of pressing
Sign In.

> **What the page says does reach us**, and that is a real change from "we only see button
> names". It is what makes an assertion about a value possible at all. The digest is landmark
> elements only and capped in both length and number, but nothing about it understands
> *content*: if your test environment is a copy of production, a page showing a customer's
> name will send that name.

Three things bound it, and you control two of them:

- **Encrypted where it rests**, the same way customer source code is.
- **Deleted on a clock you set** — 7 days by default, up to 90, and **0 means "wipe it the
  moment the tests are written"**. Nothing needs it after that, so zero costs you nothing but
  the ability to look back later at why a particular test exists.
- **Refusable entirely.** Turn it off and no page text leaves your machine at all. The
  driver stops sending it and the service drops it if sent anyway.

If you turn it off, the tests can check that a control is *there* and cannot check that a
value is *right*. The finished suite says so on its face rather than leaving whoever inherits
it to wonder why it only ever checks that buttons exist.

---

## What decides whether the suite is worth anything

**A session will not start without something to check the application against.** That is not
bureaucracy; it is the difference between a test that checks behaviour and one that checks
furniture. A session opened with nothing supplied produced eleven tests of which nine were
some form of *"the button is visible"* — and a test like that passes just as happily when the
feature behind the button is completely broken.

Any **one** of three is enough:

| Source | How to supply it |
|---|---|
| **A requirement in your own words** | The field on the screen, or `requirement` on the API |
| **A document** | Upload to Knowledge Base against this project, filed as **Requirement Review** or **QA / Test Standards**. Both shelves are read. |
| **An earlier Test Scenario or Test Case run** | `source_review_ids`, or the picker on the screen. Optional — most sessions will not have one. |

The third is the strongest brief when it exists, because a generated test case already carries
numbered steps with an expected result on each, which is most of a test. It is also the one
that needs care, which is why there are three words rather than two:

| `basis` | Means |
|---|---|
| `stated` | It checks something **a person wrote** — your requirement, or your document |
| `derived` | It checks a scenario or case **this platform generated** from what a person wrote |
| `observed` | Nothing said this should be true; it records what the application did |

**Why `derived` is separate from `stated`.** Our own test-case agent writes excellent briefs,
but it wrote them from somebody's requirement and nobody has checked that it understood. If a
test matching such a case could call itself `stated`, a machine's guess would become a
verified fact in two hops, and somebody would make a release decision on it. The word keeps
the chain visible.

**All of this is enforced in code, not asked for in the prompt.** What the session actually
had sets a ceiling, and any claim above that ceiling is pushed down to it with the reason
recorded on the row; the count is reported as `assertions_downgraded`. Requirement-coverage
rows claimed with nothing behind them are dropped entirely. This is `attest.coverage_claim`'s
rule — a model's claim about its own output is not evidence — applied to a new noun.

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
  reliably; it answers "has anything changed?", which is enough and is decidable. "Anything"
  includes what the page says and what is currently typed into its fields — a form being
  filled in is progress and changes nothing else, and a page that rewrites a total without
  reloading would otherwise look stuck while it was working.
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
code-based product's fee and markup against a measured baseline, and the measurement for this
one now exists — **$0.128 a session** across nine sessions on three applications, each six to
nineteen steps. What a session costs is dominated by how many steps you allow, not by how
large the application is, which is the opposite of the code-based product and is why the step
cap is the number on the quote.

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
