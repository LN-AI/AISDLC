# Application-based testing — running notes

A working document, kept until this product is finished. Three things live here: **what we
have found**, **what we have not thought about yet**, and **what is suggested next**.

Everything below is written for a reader who does not work on the code. Where a decision is
yours rather than mine it says so.

Last updated: 2026-09-15.

---

## What this product is, in three sentences

You point it at a running website. It opens a real browser, uses the site the way a person
would, and writes automated tests from the journeys it walked. Those tests then run on every
release, for ever, with no AI cost.

The browser runs on **your** machine, not ours. We are sent descriptions of pages and we
answer with one action at a time, which is why your web address and your password never reach
us and why an internal system behind a VPN works at all.

---

## Where it stands today

| | |
|---|---|
| Measured score | **8 of 9** sessions across three test applications |
| Cost | **$0.144 per exploring session**; replaying the tests afterwards is free |
| Proven on a real application | Once — this platform's own interface |
| Ready to sell | **Not yet.** See the open items below |

The one miss is not a poor suite; it is **no suite** — see the open fault below. It is
counted as a failure because the customer paid for the exploration and received nothing.
Counting only the sessions that produced something would read 8 of 8, which flatters.

The measured score means: explore a *working* copy of an app, then run the generated tests
against both that and a deliberately broken copy. Green on one, red on the other. That is the
only question that matters for a regression suite, and it started the day at 33%.

---

## What we have found

### Findings that changed the product

**1. It could not get past a login screen.** It typed the password four times and gave up.
The cause was a good decision with a bad consequence: we deliberately never read the contents
of a password box, so the agent had no way to see that its own typing had landed, and kept
retyping. It now reports *that* a password has been entered without reporting what it is.
Since almost everything worth testing sits behind a login, this was the difference between a
product and a demo.

**2. It could only see a quarter of a normal page.** Pointed at this platform, it was offered
**two** things to click; there are forty-six. Most websites build menus from elements the web
does not formally recognise as buttons, and we were only looking for the formal ones. It now
also notices anything the site has *made* clickable. Before the fix it wasted 22 of 25 steps
guessing web addresses and wrote tests asserting that broken links redirect.

**3. It could see buttons but not values.** It could check that a *Total* box exists; it could
not check that the total was **right** — a shop with a wrong total has exactly the same
buttons as a shop with a correct one. It now reads a short digest of what each page displays.
This is the change that carries a data question, below.

**4. Its tests failed on working software.** The worst failure mode for a test suite, because
a team that gets false alarms switches the whole thing off. Cause: to test "this action must
be refused", it drove buttons that only exist *when the bug is present*. It now checks a
refusal the way a person would verify one, without needing the forbidden button to be there.

**5. It quoted prices that were arithmetically impossible** — "$1.31 expected, $1.18 maximum".
Two separate bugs in the pricing calculation, one of which would have misinformed us at
exactly the moment we set a price for this product.

**6. It once produced a confident summary and no tests at all** — and we stored that as a
success. A suite with no files in it is not a result, whatever the summary says.

### An open fault we have contained but not explained

**About one session in three on one test app, the AI finishes exploring and then returns no
tests at all** — a confident summary, a list describing tests, and no tests. Not a truncated
answer; a complete one that is simply missing the part that matters.

The expensive thing about this is *when* it happens: after the whole exploration, so
everything the customer already paid for produces nothing and the only remedy is to start
again. It now **retries once** and says on the result that it retried. That recovers the
session cheaply, and saying so is deliberate — a silent retry would turn a one-in-three fault
into a one-in-nine fault that nobody ever measures and nobody ever fixes.

The cause is still unknown. The platform now records what the AI returned when this happens,
so the next occurrence can be diagnosed instead of reproduced at a cost.

### The finding that is still open

**Without something to check against, the tests are near-worthless.** On this platform, nine
of eleven tests it wrote were some form of *"the button is visible"*. Those pass even when
the feature behind the button is completely broken. The fix is in (below) but has not yet been
measured on a real application.

### Smaller things worth remembering

- A test that says "the server refused this" can be satisfied by the server *accepting* it, if
  it is written carelessly — because success and refusal can look alike from the outside.
- Our own quality measure was too generous and scored those "button is visible" tests as
  meaningful. It has been tightened, and now reports 8 or 9 out of 10 rather than a flat 100%.

---

## What has just been built (2026-09-15, not yet measured live)

**The data choice.** What a page displays is now encrypted where it is stored and deleted
automatically — 7 days by default, and **0 days is a real option** meaning "wipe it the moment
the tests are written". A customer can also refuse it entirely, in which case the finished
suite states plainly on its face that it could only check that controls exist.

**Something to check against is now compulsory.** A session will not start without at least
one of:

1. a requirement typed in your own words;
2. a **Requirements or QA document** uploaded against the project — this reads two document
   shelves, because a specification is filed under Requirements and is the single most useful
   thing a test writer can be handed;
3. an earlier **Test Scenario** or **Test Case** run to cover — optional, and most sessions
   will not have one.

**Tests now say who they are answering to.** Three words instead of two:

| Word | Meaning |
|---|---|
| **stated** | Checks something a person wrote — your requirement, or your document |
| **derived** | Checks a case *this platform* generated from something a person wrote |
| **observed** | Nothing said this should be true; the test records what the app did |

`derived` exists for an honesty reason worth understanding. Our own test-case agent produces
near-perfect briefs — numbered steps, each with an expected result. It is tempting to treat
those as a specification. But they were written by a machine, from somebody's requirement, and
nobody has checked them. Letting a test claim it "verified the requirement" on that basis
would turn a machine's guess into a verified fact in two hops. The word keeps the chain
visible.

**This is enforced in code, not asked for in the prompt.** The AI can claim whatever it likes;
the platform pushes the claim down to whatever the session could actually support.

---

## What we have not thought about yet

Roughly in order of how likely each is to bite.

**Logging in through SSO or two-factor.** Enterprise customers mostly sign in through
Microsoft or Google, or with a code sent to a phone. Completely untested. This could be a hard
blocker for a whole customer segment and we would not currently know.

**Who maintains the tests.** In six months the app changes and some tests break legitimately.
Somebody has to tell "the app broke" from "the test is out of date". No product here helps
with that, and it is where most test-automation efforts quietly die.

**Flaky tests.** Web tests fail intermittently for timing reasons. One flaky test poisons
trust in the whole suite. We have no retry policy, no quarantine, no flake score.

**How much of the app gets covered.** A 40-step session sees a handful of screens; a real
application has hundreds. We cannot currently answer "what percentage of my app is tested?",
and it is the second question every buyer asks.

**Parts of a page we still cannot see.** Content inside an embedded sub-page (an `iframe`),
and a newer technique that hides parts of a page from outside inspection ("web components").
Both are fixable; neither is done.

**Apps drawn as pictures.** Maps, charts, design tools, anything on a drawing canvas. There is
nothing readable there and probably never will be. Worth knowing so we do not promise it.

**Test data.** The agent explores whatever data happens to exist. An empty test environment
gives it nothing to work with; a production copy raises the data question above.

**What happens when a customer's app changes mid-session.** A deploy during exploration would
produce a suite describing two different versions of the application.

**Cost at enterprise scale.** $0.128 a session is measured on small applications. A dense
enterprise page sends more to the model per step. Unmeasured.

---

## Decisions taken so far

| Decision | Choice | Date |
|---|---|---|
| Where the browser runs | On the customer's machine, not ours | 2026-09-11 |
| Destructive actions | Word-based blocklist plus a signed statement that the target is a test environment | 2026-09-11 |
| Page text | Encrypt it, auto-delete after N days (0 allowed), and let the customer refuse it | 2026-09-15 |
| Something to check against | At least one of three sources is compulsory | 2026-09-15 |
| Machine-written cases | Usable, but labelled `derived` — never `stated` | 2026-09-15 |
| Which documents to read | Requirements + QA shelves; not everything | 2026-09-15 |
| Pricing | Still open — now has a measured number behind it ($0.128/session) | — |

---

## Suggested next steps

**1. Re-measure, then point it at real customer-style applications.** *(Half a day, a few
pounds.)* Everything significant so far was found by running it against something real, and we
have only ever run it against one real application — our own. Ideally three: one React or
Next.js app, one with a proper login, and one older PHP-style system. This answers the
framework question with evidence rather than argument.

**2. Decide pricing.** Short conversation; the measured number now exists.

**3. Test an SSO login.** The highest-risk unknown for enterprise buyers.

**4. Then iframes and web components.** Known gaps, known fixes. Not worth the time until
step 1 says they matter for real applications.

**Deliberately not yet:** hosting the browsers ourselves. Real infrastructure, and it only
helps customers whose applications are on the public internet — which, for regulated buyers,
is few. The design already leaves the door open.

---

## Where the detail lives

- `DECISIONS.md` — every decision with its reasoning, D-407 to D-417 and C-94 to C-98
- `docs/agents/app-testing.md` — the reference guide for someone using the product
- `scripts/eval_app_testing.py` — how the score is measured, and the measured numbers
- `evals/app_testing/results/` — the raw measurements, including the unflattering first one
