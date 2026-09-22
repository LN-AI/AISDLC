# App-based test automation — findings, 2026-09-17

A working handoff for fixing and course correction. Written after a day of live runs against
public applications. **Read §1 and §9 first**; everything else is evidence for them.

Durable architectural decisions live in `DECISIONS.md` (D-433…D-445, C-100…C-109). This file
is the narrative those rows do not carry: what was measured, what turned out to be wrong, and
what to do next.

---

## 1. The single most important finding

**The suite writer is starved. It is given 5% of what the session actually saw.**

Measured on a healthy 16-step SauceDemo run (session `1dbeb2ab`, review `bdce4588`):

| | |
|---|---|
| Controls shown to the model during exploration | **240** |
| Controls that survive into the suite-writing prompt | **12** |
| Ratio kept | **5%** |
| Product names it knew about | **1 of 6** |

`_journeys_markdown` builds the generation input from **actions taken**, one line per step.
Every element the explorer *looked at* but did not *click* is discarded before the suite is
written.

The consequence is not subtle. Requirement 5 says *"the catalogue lists six products, each
with a name, a description and a price"*. What the writer was given for that page was:

```
text: Swag Labs; text: Products; text: $29.99; text: $9.99; text: $15.99;
text: $49.99; text: $7.99
```

Five prices, no names, no descriptions, and no way to know which price belongs to which
product. It wrote the test anyway, and the test failed. **It was not careless — it was not
given the information the task required.**

The same mechanism explains `locked_out_user` appearing in a test: requirement 3 needs a
locked-out account, the session only ever had `standard_user`, so the model reached into
training memory for a real SauceDemo account name (C-109).

### Token economics of the same run

| call | in | out |
|---|---|---|
| each step call (×16) | ~1,423 | ~141 |
| the generation call (×1) | **3,818** | **11,764** |

The writing call **produces three times more than it is given**. 21 tests, 7 files, 15
requirements, from about a page and a half of clicking notes.

---

## 2. The fix that follows, and why it is cheap

A **structured outline** of each page — containment preserved — instead of a flat list.

Measured on the SauceDemo catalogue:

| | tokens |
|---|---|
| What we send today (flat: ≤60 controls + ≤28 text lines) | **1,713** |
| A structured outline of the same page | **677** |

It is **60% cheaper** and contains what was missing:

```
(div)
  <a> "View details for Sauce Labs Backpack"
  Sauce Labs Backpack
  carry.allTheThings() with the sleek, streamlined Sly Pack that melds...
  $ 29.99
```

Name, description, price — **nested**, so the relationship is knowable.

**Keep the index contract exactly as it is.** Annotate interactive nodes with their candidate
number (`[#3]`). The model still answers with a number, so D-408's safety property is
untouched. Only what it *sees* changes, not what it can *do*.

### Raw DOM was ruled out for the wrong reason

The assumed objection was size. **That is false for target applications**:

| page | raw HTML | stripped DOM | we send today |
|---|---|---|---|
| SauceDemo login | 686 | 232 | 164 |
| SauceDemo catalogue | 4,648 | 1,980 | **1,713** |
| ParaBank home | 1,373 | 950 | **1,648** ← we send *more* than the DOM |
| Shoelace docs (big) | 55,950 | 21,875 | 2,891 |

The real objections to raw DOM are different and still stand: it is mostly framework noise,
and it hands the model class names, which re-introduces C-101. **Structure is the valuable
part of the DOM, not the markup.**

---

## 3. Two recurring bug families

Nearly every defect in this product's history belongs to one of these. Each has now recurred
three or four times, because nothing structurally prevents them.

### Family A — "which moment is this?"

One database row holds three different moments: the page *before* the action, the action, and
the outcome of the *previous* action. Every place that renders that row is a fresh chance to
pair them wrongly.

| where | cost |
|---|---|
| D-428 — the session screen | cosmetic |
| D-430 — outcome column into generation | two live suites, **every test failed** |
| D-434 — page text into generation | one eval session, **8 of 10 assertions wrong by one step** |

Fixed three times at three render sites, never at the cause. A fourth is likely. The
structural fix (store `observed_before` / `observed_after` explicitly) is deferred — see §9.

### Family B — "did you see it, or remember it?"

The model is given a narrow observation and asked to write free-form code. Anything not
traceable to the observation was recalled from training.

| what was recalled | cost |
|---|---|
| D-427 — a button's accessible name | two live suites, zero tests ran |
| D-431 — where page text lives | 6 product prices never collected |
| C-101 — CSS class selectors | 3 wrong of 19; a correct app reported broken |
| C-103 — numeric values | a test asserting £8.00 where £4.00 is right |
| C-109 — credentials and fixture data | `locked_out_user`, never supplied |

**§1 is the cause of Family B.** The model invents because it is starved, not because it is
careless.

---

## 4. What shipped today

18 commits, 16 files, +4,424/−82. All offline suites green.

```
859d0a9 Record that two passing audits do not mean a working suite
8bdf9b0 Ask whether the application moved before blaming the suite
aab20ab The closed loop cannot tell a wrong test from a broken demo
4a42386 Stop the closed loop from announcing a green it never saw
da40b45 See a locator that is the right kind and still addresses nothing
b2bc71e Record a one-off flake, and the harness mistake that hid it
d1a2189 Stop calling an absent heading an invented one; baseline back to 9 of 9
a93df19 Only warn about repetition that is getting nowhere
0bf6fe8 Record why the no-progress rule could not see a loop
467fa48 Notice a session going round in circles
a68bc7f Stop losing a suite the platform already paid to write
a76a537 Name the tests that would not notice, and the heading nobody saw
e953809 Run the suite against the app it was written from, and see inside components
8824719 Record that a shadow root or an iframe is invisible to the collector
729d13f Keep both of today's baseline runs, not just the good one
c06c52d Re-measure the baseline, and say what the number hides
5b85ca3 Say which side of an action the page text was read on
f55038b Count the locators a suite could not have seen
```

**New machinery:**

- `backend/services/locator_audit.py` — counts locators that could not have come from the
  session. Four kinds: recalled selectors (class/id/test-id/xpath), a **heading nobody saw**
  (D-438), and a locator that **contradicts the record** (D-442).
- `backend/services/assertion_audit.py` — counts tests that could never fail (D-437).
- `explore_app.py --verify` — runs the suite against the application it was written from
  (D-435), reads its verdict from the platform's record (D-444), and re-checks the entry page
  before blaming the suite (D-445).
- Shadow-root traversal in the collector (D-436); frames announced, not walked.
- Set-based no-progress rule + a repetition nudge (D-441).

**Baseline: 9 of 9 at $0.135 a session**, PROMPT_VERSION `2026-09-17.3`.

---

## 5. The uncomfortable verdict on §4

**Static analysis of a generated suite cannot substitute for running it.**

A healthy SauceDemo session produced 21 tests. The audits gave it a **clean bill** — 144
grounded locators, **0 invented**. **10 of the 21 failed against the working application**,
across 7 files with no shared cause.

I spent most of the day building checkers that inspect the output. The defect is in the
input (§1). The audits are worth keeping — they are cheap and they catch real things — but
**two passing audits say nothing about whether half the suite works**.

---

## 6. Open caveats

| id | one line | status |
|---|---|---|
| C-100 | collector gaps: de-duplicated strings, document-order cap, 120-char line limit | open |
| C-101 | invented CSS selectors | mitigated (audit + retry); residuals named |
| C-102 | allow-list judgement is the customer's; **never driven a money-moving control** | **needs a policy decision** |
| C-103 | recalled *values* — £8.00 where £4.00 is right | open, unfixed |
| C-104 | frames invisible (shadow roots now walked) | narrowed |
| C-105 | explorer wanders — 22 of 26 steps on one page | mitigated (D-441), not solved |
| C-106 | one unreproduced flake in `smoke_review_sources` (1 of 1147) | open, undiagnosed |
| C-107 | **the 60-candidate cap is eaten by navigation** — all 60 were sidebar on a real site | open, high value |
| C-108 | `--verify` cannot tell a broken app from a bad suite behind a login | partially narrowed |
| C-109 | invented credentials/fixture data; audits blind to it | open |

---

## 7. Decisions the user made (2026-09-17)

1. **Weak tests** → *explore more instead of writing a weak suite* **and** *still label any
   weak tests that remain*. Both, not either.
2. **Scorecard** → *more repeats on the unstable case* **and** *add a fourth sample
   application*. Cost rises from ~£1.22 to ~£1.90 per baseline.
   - Suggested shape for the fourth case: an application whose navigation is large enough to
     eat the entire candidate budget, so the scorecard can finally see C-107.
3. **Next work** → do the first three items together; confirmed they are complementary.

## 8. Decisions still open

- **Where the test-environment attestation of record lives.** `explore_app.py` hardcodes
  `attested_test_env: True`; the browser tick only controls whether the command is displayed.
  Compliance-adjacent.
- **Whether `/basis` returns the first ~2000 chars of the customer's own documents** behind a
  disclosure, or only counts and skip reasons.
- **C-102** — permitting a money-moving control is the customer's judgement, and nothing in
  the platform can verify that an address is a test environment.
- **`APP-BASED-TEST-AUTOMATION-PLAN.md`** is still untracked, now that the plan is fully
  built.

---

## 9. What to do next, in order

The order matters: items 2–4 each change what a measurement *means*, so doing them together
costs **one** re-baseline (~£1.90) instead of three (~£5).

**1. Build the page inventory / structured outline (§1, §2).** Highest value available. The
data already exists and is thrown away; the outline is *cheaper* than what we send today.
Behind a flag, measured before adoption.

*Risk:* a bigger prompt can dilute attention — that has happened in this project before. It
is ~2¢ a session, so measure rather than assume. The 677-token figure is one page on one
site; Shoelace has not been measured.

**2. Fix the candidate cap (C-107).** On a real component site all 60 slots went to the
sidebar and the model never saw the page content. Not a bigger cap — 314 candidates is a
prompt nobody should pay for — but an ordering that prefers content over chrome, or a
per-region budget.

**3. Scorecard changes** (user decision 2): more repeats on `basket-total`, plus a fourth
sample application with oversized navigation.

**4. One re-baseline** covering 1–3.

**5. The "explore more instead of a weak suite" work** (user decision 1). Needs a
pre-generation quality signal that does not exist yet. **Validate the signal against the
sessions already on disk before wiring it to anything** — it must separate the 22-step
ParaBank loop from the 7-step `signup-wizard` that legitimately finishes early.

**6. Deferred deliberately:** the Family A structural fix (store before/after explicitly).
Do it when a fourth instance appears, or when already working in that code.

---

## 10. Measured numbers — do not re-measure these

| what | value | where |
|---|---|---|
| Controls seen vs. given to the writer | 240 → 12 (**5%**) | session `1dbeb2ab` |
| Step call | ~1,423 in / ~141 out | server log |
| Generation call | 3,818 in / 11,764 out | server log |
| Structured outline vs. flat list (catalogue) | **677 vs 1,713 tokens** | probe |
| Shadow walk on a real component site | 258 → **314** candidates | Shoelace |
| Candidate cap consumed by navigation | **60 of 60** | Shoelace |
| ParaBank wandering | **22 of 26 steps** on one page | session `90ecaa05` |
| SauceDemo verify | 21 tests, **10 failed**, audits clean | review `bdce4588` |
| ParaBank suite (never run until today) | **0 passed, 16 failed** | review `a7638fa6` |
| Baseline | **9/9 at $0.135/session** | `2026-09-17.3` |
| Spend for the day | ~$5.20 | |

---

## 11. Process failures worth not repeating

These cost real time today. They are recorded because the pattern is more useful than the
individual bugs.

**Reading a proxy instead of the result — three times.**
- `--verify` reported "Every test passed" three lines below "0 passed, 13 failed", because it
  read the runner's **exit code**, which reports whether *reporting* succeeded (D-444).
- A before/after comparison returned **60 both ways** and was nearly reported as "no effect";
  60 was the **cap**, and both runs had saturated it (D-443).
- A **"0 invented locators"** score was quoted approvingly for a suite that failed 16 of 16.

> **Rule: an exit code, a clean audit and an identical number are all proxies. Read the
> result.**

**Testing new checkers with invented inputs.** All three defects introduced today were in new
*checking* code, and all three passed the tests written alongside them — because the fixtures
were synthetic and shaped by the same assumption as the code. Every one broke the moment a
**stored real trace** was replayed, and those traces were already on disk.

> **Rule: replay a real trace first, synthetic fixtures second.**

**Escaping damage in patch scripts — three separate times**, once shipping a dead function
(`def _verify` ended up inside a comment) that every offline check passed over.

> **Rule: backslash-heavy edits go through the file tools, never a shell heredoc.**

**A sweep that discarded its own evidence.** The full-net script piped each suite through
`grep` for the summary line, so when one check failed the failing line was destroyed as it
was produced (C-106). It is still undiagnosed.

> **Rule: a regression net that reports a failure without the evidence cannot be acted on.**

---

## 12. Commands

```bash
# offline net (no key, no network) — keep full output
venv/Scripts/python.exe scripts/smoke_app_testing.py      # 311 checks
venv/Scripts/python.exe scripts/explore_app.py --fixture  # 53 checks
node scripts/smoke_render.js
venv/Scripts/python.exe scripts/eval_app_testing.py --self-check   # free

# a live session, with the closed loop
RELOAD=0 venv/Scripts/python.exe run.py                   # NEVER a bare uvicorn (D-243)
LM_PASSWORD=... venv/Scripts/python.exe scripts/explore_app.py \
  --url <app> --api http://127.0.0.1:8000 --user admin --project <id> \
  --requirement-file <file> --app-user <u> --app-password <p> \
  --max-steps 16 --attest --verify --verify-into <dir-with-node_modules>

# free reconnaissance — spends nothing, opens no session
venv/Scripts/python.exe scripts/explore_app.py --url <app> --dry-run

# the scorecard (~£1.22 today, ~£1.90 after the agreed changes)
venv/Scripts/python.exe scripts/eval_app_testing.py
```

**Restart the server after any `backend/` change** — a stale process serves old code silently
and both addresses can bind at once (X-07, D-243).
