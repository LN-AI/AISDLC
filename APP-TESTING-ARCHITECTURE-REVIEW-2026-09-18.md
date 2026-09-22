# Critical evaluation — ten proposals for the app-based test automation platform

_2026-09-18. Written against `APP-TESTING-FINDINGS-2026-09-17.md`, `DECISIONS.md` (D-407…D-445,
C-94…C-109), and a read of the live code. Capacity assumed: **solo**._

---

## Context

The platform explores a running web application in a real browser and writes a Playwright suite
from what it walked. It works on a three-case benchmark (9 of 9 `discriminates`, $0.135 a
session) and does badly on real applications: a healthy SauceDemo session produced 21 tests of
which **10 failed against the working application**, and a ParaBank session produced 16 tests of
which **16 failed**. Both passed every static audit the platform has.

Ten proposals have been put forward to fix this. This document attacks them. The short version:
**three are right and under-specified, three are right and mis-sequenced, two are premature, one
is the thesis rather than a work item, and the highest-value idea available is not in the list.**

Two claims below I verified by running code rather than by reading it, because this project's own
rule is that a clean audit is a proxy and a proxy is not a result. They are in Appendix A and they
change the sequencing of Proposals 2, 6 and 8.

---

## 1. Executive summary

**The diagnosis in the brief is right but shallow.** "The explorer sees more than the generator
receives" is true — 240 controls seen, 12 delivered. The deeper fact is that **the platform never
stored the other 228.** `app_steps.candidates_offered` is an `INTEGER`. What the explorer saw was
discarded at the wire, not at generation time.

Three consequences, and they reorder the work:

1. The findings document's plan says *"the data already exists and is thrown away"* and proposes
   building the inventory first. The data does **not** exist. Nothing can be built or validated
   against stored sessions until capture starts.
2. Every "validate the signal against sessions already on disk" idea — the confidence gate, the
   coverage measure, the retrieval index — is blocked on the same missing capture.
3. Capturing observations is the **cheapest change in this entire document** and it unblocks four
   proposals. It should ship this week.

**The most important idea is missing from all ten proposals.** The platform already solved
"the model must not invent an element" — for *actions*. D-408: the model returns a candidate
index, never a selector, so a guessed selector is impossible by construction. For *assertions*
it solved nothing: the model writes free-form JavaScript and the platform audits it afterwards.
Every recurring defect — invented selectors (C-101), invented values (C-103), invented
credentials (C-109), wrong accessible names (D-427), headings nobody saw (D-438) — lives on the
generation side, where the constraint was never applied.

> **The single highest-leverage structural change is to apply D-408 to the writer: the model
> selects an element reference and an assertion kind; a deterministic compiler emits the
> Playwright. Six documented bug families become unrepresentable rather than detectable.**

**The proposals over-invest in output-side machinery and under-invest in constraint.** Proposal 5
(traceability), Proposal 9 (retrieval) and Proposal 10 (agent fleet) all add apparatus around a
generator that is still free to write anything. The findings document already reached this verdict
about the audits — *"I spent most of the day building checkers that inspect the output. The defect
is in the input"* — and Proposals 9 and 10 are the same mistake with a larger budget.

**Ranked verdict:**

| | Proposal | Verdict |
|---|---|---|
| P2 | Structured page inventory | **Implement now** — with region inference, which the proposal omits |
| P7 | Requirement-driven exploration | **Implement now** — cheapest real win; steer with the model, not a lexical scorer |
| P8 | Uncertainty management | **Implement now** — but computed in code; never ask the model for a confidence score |
| P4 | Separate explore / understand / generate | **Implement now as a data rule**, not as new orchestration |
| P5 | Evidence-backed generation | **Implement now for the free half**; postpone screenshots |
| P6 | Coverage-driven exploration | **Prototype first** — coverage is an objective function and two of the proposed metrics optimise for the failure being fixed |
| P3 | Application Knowledge Model | **Prototype the structural layer**; postpone the business-entity layer |
| P1 | Understanding-based generation | **Not a work item** — it is the thesis of P2+P3+P4 |
| P9 | Retrieval-based generation | **Postpone** — optimises the cheap half of the call; there is no corpus yet |
| P10 | Specialised agent fleet | **Reject as drawn** — four of the seven are arithmetic, not judgement |

---

## 2. Critique of each proposal

Each follows the eleven-point structure. Points 4–9 are tabulated to stay scannable.

---

### Proposal 1 — Action history → application understanding

**1. Summary.** Replace `Browser → Action Log → Generation` with
`Browser → Application Understanding → Generation`.

**2. Advantages.** It names the right target. The measured evidence is strong: the writer was
given five prices and no product names for a requirement that says *"six products, each with a
name, a description and a price"*, wrote the test anyway, and the test failed. That is not
carelessness; it is a task issued without its inputs.

**3. Disadvantages.** As stated it is **unfalsifiable and not schedulable**. "Understanding" is
not a component you can build, review or measure — it is the emergent result of P2 (inventory),
P3 (model) and P4 (separation). Keeping P1 on the board as its own item invites a milestone that
can be declared complete without anything being true.

There is a second, sharper problem. **P1 is a context fix, and a large share of the measured
failures are not context failures.** `locked_out_user` (C-109) appeared in a test for an account
the session never had. No amount of page structure prevents that — the model needed to be
*unable* to name a credential it was not given. Same for C-103's £8.00 where £4.00 was right: the
value was in the session, at a different step. These are **constraint** failures, and P1 does not
touch them.

| | |
|---|---|
| **4. Hidden assumptions** | That more input improves output. Unproven here and cuts both ways: the generation call is already **3,818 in / 11,764 out** — it produces three times what it is given. Adding input to an output-dominated call is not obviously the lever. The findings document itself flags prompt dilution as a risk that "has happened in this project before". |
| **5. Technical risks** | Attention dilution; a larger prompt that scores worse. Must be measured against the 9/9 baseline, not assumed. |
| **6. Product risks** | Declaring "understanding" shipped while suites still fail. |
| **7. Operational risks** | None directly. |
| **8. Cost** | Nil as a framing; large as an umbrella that hides P2+P3+P4's real cost. |
| **9. Complexity** | N/A. |

**10. Generalisation.** The framing generalises. The framing is all it is.

**11. Verdict.** **Reject as a work item; adopt as the stated north star.** Track P2, P3, P4
separately so each can be measured and each can fail visibly.

---

### Proposal 2 — Structured page inventory

**1. Summary.** Replace the flat list (≤60 controls + ≤28 text lines) with a hierarchical outline
preserving containment, cards, tables, forms and navigation regions.

**2. Advantages.** The strongest case in the set, and the only one already measured:

| | tokens |
|---|---|
| Flat list sent today (SauceDemo catalogue) | 1,713 |
| Structured outline of the same page | **677** |

**60% cheaper and strictly more informative.** It recovers the relationship that made requirement
5 impossible — name, description and price *nested*, so which price belongs to which product is
knowable. And it preserves D-408 intact: annotate interactive nodes with their candidate index
(`[#3]`), the model still answers with a number, safety property untouched.

The raw-DOM comparison in the findings kills the usual objection: on ParaBank the platform
already sends **more** (1,648) than the stripped DOM (950). Size was never the reason.

**3. Disadvantages — and the one the proposal does not mention.**

> **The measured win depends on the application having semantic structure, and the applications
> in the target list mostly do not.**

SauceDemo's catalogue is clean, shallow markup where DOM containment *is* logical containment.
A React/Material-UI, Tailwind or Angular-Material application is fifteen levels of anonymous
`<div>`. Serialising that tree produces a deep outline of unnamed containers — **larger than the
flat list and less useful.** The proposal says "store page hierarchy"; on modern applications
there is no hierarchy to store, only nesting.

The missing step is **region inference**: detect repeated sibling subtrees of identical shape
(hash the tag/role skeleton), collapse them into one labelled group with N instances, and name the
group from its heading or its nearest landmark. That is a deterministic algorithm, cheap, runs in
the page, and it is what makes `Products → {name, description, price} × 6` fall out of a div soup.
**Without it P2 is a SauceDemo optimisation.**

Two further constraints the proposal should state explicitly:

- **It must annotate the existing candidate list, not replace it.** If the outline and the screened
  candidate list are built by different passes, the two sides can disagree about what index 7 is —
  and D-429 records what that costs: the index is only safe while both sides screen to identical
  lists.
- **It must not carry class names or ids.** That is C-101's supply line. Structure is the valuable
  part of the DOM; the markup is the part that causes recall.

| | |
|---|---|
| **4. Hidden assumptions** | (a) DOM nesting reflects logical grouping — false on framework apps; (b) 677 tokens generalises — it is one page on one site, and Shoelace is **unmeasured**; (c) the cap problem is separate — it is not: C-107 measured **60 of 60** candidate slots consumed by a sidebar, so on that page an outline of the first 60 elements is an outline of a menu. |
| **5. Technical risks** | Outline explosion on deep trees; a per-page token cost that is unbounded where the flat list was bounded by construction (60 × 28). Needs its own cap, and a cap that degrades by *dropping depth* rather than truncating mid-page. |
| **6. Product risks** | A bigger prompt scoring worse on the 9/9 baseline — real, ~2¢ a session to find out. |
| **7. Operational risks** | Changes the function every eval number depends on; one re-baseline minimum. |
| **8. Cost** | Low. In-page JS + a serialiser + a cap. Days, not weeks. |
| **9. Complexity** | Low–moderate. Region inference adds moderate. |
| **10. Generalises?** | **Only with region inference.** As proposed, it generalises to server-rendered semantic HTML and degrades on exactly the frameworks named in the target list. |

**11. Verdict.** **Implement now**, with region inference in scope from the start, and gate
adoption on experiment E1 (outline size and quality on a React, an Angular and a
component-library application). Pair it with the candidate-budget fix (C-107) — they touch the
same function and separating them costs two re-baselines.

---

### Proposal 3 — Application Knowledge Model

**1. Summary.** A durable model: `Application → Login / Products{name, description, price} / Cart
/ Checkout`. Generate from discovered knowledge rather than step history.

**2. Advantages.** One argument for this is much stronger than the one given, and the proposal
does not make it:

> **A session cannot cover a real application, and today a session is the only container.**
> One session = one credential = one suite = one throwaway. A CRM has 200 screens; a 40-step
> session sees eight of them. Without a model that **outlives the session**, the platform can
> never accumulate coverage of a large application no matter how good exploration gets.

That — not entity naming — is why P3 matters. It is the difference between a product that tests
demos and one that tests an enterprise system.

**3. Disadvantages.** The example tree conflates two things with very different reliability:

| Layer | Example | How obtained | Confidence |
|---|---|---|---|
| **A — structural** | "screen `/inventory` contains a repeated group of 6 items; each has a link, two text nodes" | deterministic, from the DOM | high |
| **B — semantic** | "that group is **Products**; the second text node is a **price**" | inferred, by a model | low |

> **If Layer A and Layer B share a table, the knowledge model becomes a hallucination amplifier.**
> Today a bad guess produces one bad assertion that `--verify` catches. Stored in a model that
> reads as authoritative, it produces bad assertions in every future session against that
> application, with provenance that looks solid. C-103 at one remove — which is precisely the
> failure mode D-420 was written to prevent for assertion claims, arriving through a new door.

What should **not** be modelled: business rules; validation logic; workflows never completed;
anything from a single observation; anything about *why* the application does something. Those
are the fields a model will fill confidently and nobody can check.

| | |
|---|---|
| **4. Hidden assumptions** | (a) Entity semantics are inferable from the DOM — partly, and domain-specifically: "beneficiary", "policy", "encounter", "prior authorisation" will come back as "item" and "text" without a domain vocabulary; (b) a model built from one session generalises to the next — only if state identity is stable, which it is not on URL alone (see P6); (c) the customer wants this — it is a large new store of *their* application's content and data. |
| **5. Technical risks** | State identity is the hard problem, not the schema. `/orders/1043` and `/orders/1044` are one screen; `/dashboard` before and after login are two. Get this wrong and the model accumulates duplicates or merges distinct states, and every coverage number computed over it is wrong. |
| **6. Product risks** | Building a large abstraction whose consumer (generation) may do no better with it than with a good per-page outline. **That is unproven and is the thing to prototype.** |
| **7. Operational risks** | A durable per-application store holds more customer content, for longer, than D-418's 7-day page-text digest. New retention, encryption and disclosure surface. **This is a policy decision, not an engineering one** — see Appendix C. |
| **8. Cost** | High. Schema + writers + identity resolution + readers + retention + UI + migration. Weeks. |
| **9. Complexity** | High — the highest in the document. |
| **10. Generalises?** | Layer A yes. Layer B generalises structurally and **not** semantically; expect generic labels outside e-commerce until domain vocabularies exist. |

**11. Verdict.** **Prototype Layer A; postpone Layer B.**

Layer A falls out of P2 almost free — if the outline is already computed, persisting it per screen
with a stable identity key is a small increment, and it is what makes multi-session accumulation
possible. Layer B should wait until a measurement shows that generation does better from typed
entities than from a structural outline. **Do not build the tree in the proposal as the first
version; build the observation store underneath it.**

---

### Proposal 4 — Separate exploration from generation into four phases

**1. Summary.** Explore → Understand → Generate → Verify as distinct phases.

**2. Advantages.** Correct in substance.

**3. Disadvantages.** The proposal **misdiagnoses the coupling**. Three of these four phases
already exist and are already separate: N step calls, one generation call, then `--verify`
(D-435). What is coupled is not the orchestration — it is the *data*: generation reads the raw
step log directly, through `_journeys_markdown`, which reconstructs meaning from rows that carry
three different moments in time.

So the real proposal is narrower and better: **insert an Understand stage, and make generation
read only from it.** Stated that way it is a data rule, not new machinery, and it is enforceable
by inspection — if `_journeys_markdown` still exists, the rule is not implemented.

The risk the proposal does not mention: **every phase boundary is a new place to lose a paid
session.** D-440 already records this — a 26-step ParaBank session completed server-side and
stored a 16-test suite for $0.31 while the driver had already timed out and printed a stack trace.
The money was spent, the suite existed, and the human was told it crashed. Adding a stage adds
another such boundary.

| | |
|---|---|
| **4. Hidden assumptions** | That phases are currently entangled (they are not); that separation is free (it costs a crash-resumable intermediate). |
| **5. Technical risks** | An understanding artefact that must be persisted, versioned and resumable. The modernization subsystem already solved durable multi-stage execution (claim-and-lease); reuse it rather than invent a second pattern. |
| **6. Product risks** | Low. |
| **7. Operational risks** | More state to store, encrypt and expire. Each boundary needs a "your work is safe, resume with X" path, which D-440 shows is not optional. |
| **8. Cost** | Low if done as a data rule; moderate if done as orchestration. |
| **9. Complexity** | Low–moderate. |
| **10. Generalises?** | Yes, and it is a precondition for multi-session accumulation. |

**11. Verdict.** **Implement now, as a data rule.** One sentence: *generation may read the
understanding store and nothing else.* Do not build a workflow engine.

---

### Proposal 5 — Evidence-backed test generation

**1. Summary.** Every assertion traceable: requirement → observed evidence → page → screenshot →
session.

**2. Advantages.** The right instinct, and the review benefit is real — today a reader holding a
failing suite cannot tell a wrong test from a broken application, which is C-108 open.

**3. Disadvantages.** As drawn, **it adds metadata the model writes about its own work, and this
platform has already learned three times that such metadata is worthless.**

- `assertion_basis` is exactly this, and the platform had to **downgrade it in code** because a
  model's claim about its own output is not evidence (D-406 → D-409 → D-420).
- D-417: a generation returned a *confident summary* with **zero test files** and was stored as a
  success.
- `eval_app_testing._HOLLOW` reads the model's `what_it_checks` sentence; `assertion_audit` exists
  because that sentence can caption a `toBeVisible` as "the total updates correctly" at no cost.

Five more self-reported fields are five more claims to distrust. **Traceability asserted by the
generator is decoration.**

The reframing that makes P5 valuable:

> **Traceability is near-free as a by-product of constrained generation, and worthless as an
> annotation.** If the model picks `element_ref: e_1043` and `matcher: toHaveText` and
> `value_ref: v_88`, the chain to page, session and observation is a foreign key. It cannot be
> wrong, and it cost nothing to produce.

That is the missing idea (M1 below). P5 should be folded into it rather than built alongside it.

**On screenshots specifically — reject for now.** The platform has **no binary storage anywhere**;
every byte path today is an in-memory single-use dict or Fernet-encrypted TEXT, and this was a
deliberate deferral (D-377). Adding image storage means storage infrastructure, encryption,
retention, serving, and a policy decision about screenshots of a test environment seeded from
production — which contain real names, balances and records. That is a large bill for review
ergonomics. Store a local path and a hash if anything; let the driver keep the pixels.

| | |
|---|---|
| **4. Hidden assumptions** | That evidence links reduce hallucination. They do not — they *document* it. Only a constraint reduces it. |
| **5. Technical risks** | Low for references; high for screenshot storage. |
| **6. Product risks** | A traceability UI that makes an ungrounded suite look rigorous is worse than no UI. |
| **7. Operational risks** | Screenshots are the single largest new PII surface in this document. |
| **8. Cost** | Near zero as a by-product of M1; weeks as an artefact store. |
| **9. Complexity** | Low / high, respectively. |
| **10. Generalises?** | Yes for references. Screenshots generalise poorly — a dashboard screenshot at 2am differs from one at 2pm and neither is evidence of anything. |

**11. Verdict.** **Implement the reference chain now, as part of M1. Postpone screenshots.**

---

### Proposal 6 — Coverage-driven exploration

**1. Summary.** Replace `max_steps = N` with "stop when coverage is sufficient".

**2. Advantages.** The problem is real and measured: ParaBank spent **22 of 26 steps** on one page
(C-105); SauceDemo alternated cart↔catalogue for eleven steps. A step budget is a cost control
wearing the costume of a stopping rule.

And the premise is **worse than the proposal states** — see Appendix A.2. The one signal the
platform has for "this session is getting nowhere" (`stalled`, D-441) is **structurally disabled
on any page carrying a live region or a clock**, because the page signature folds in page text
and `TEXTS_JS` collects `[role=status]` and `[aria-live]` by design. I verified this: four visits
to one unchanged page with a single ticking status line produce four distinct signatures; with the
status line removed, one. On a dashboard, a notification badge or a "last updated" stamp — the
target categories in the brief — **every session runs to `max_steps` regardless of progress.**

**3. Disadvantages — the critical one.**

> **A coverage metric is not a measurement. It is an objective function, and two of the five
> proposed metrics optimise for the failure being fixed.**

- **Navigation coverage is actively harmful as a target.** C-107 already measured that a sidebar
  consumes 60 of 60 candidate slots. Rewarding navigation coverage instructs the explorer to click
  all 200 sidebar items. It converts the worst observed failure mode into the scoring rule.
- **Page coverage is misleading on SPAs.** On `basket-total` the URL never changes and every click
  rewrites part of the page — a URL-keyed page count reads 1 forever. It must be *state* coverage
  over a stable state identity, which is P3's unsolved hard problem.
- **Requirement coverage is the only commercially meaningful one and the hardest to measure
  honestly.** "This journey covers requirement 3" is a judgement; if the model makes it, D-406
  applies and it is not evidence.
- **Form coverage is the underrated one** and the proposal treats it as an also-ran. Fields filled,
  submitted, **and at least one validation failure observed** is cheap, deterministic, and it is
  what separates a test that exercises a form from one that looks at it.

The second critical objection: **removing the step cap removes the only thing that makes a session
quotable.** The preflight quote is `steps × step_quote + generation_quote`; D-412 is an entire row
about getting that arithmetic wrong. Coverage must replace the **early stop**, never the **hard
bound**.

The metric I would add and the proposal omits: **information gain per step** — new states, new
elements, new distinct observed values per step, over a sliding window. Deterministic, free, and
it measures C-105 directly instead of by proxy.

| | |
|---|---|
| **4. Hidden assumptions** | (a) Coverage is measurable before understanding exists — it is not, it is defined over P3's store; (b) more coverage is better — a suite covering 200 nav links and no workflow is worse than one covering three workflows; (c) the explorer can reach what it should cover, which RBAC and multi-step preconditions make false. |
| **5. Technical risks** | Depends entirely on state identity (P3). Built on URL keys it will be confidently wrong. |
| **6. Product risks** | Unbounded sessions; a quote that no longer bounds the invoice. |
| **7. Operational risks** | Coverage targets become a support surface — "why did it stop at 60%". |
| **8. Cost** | Moderate, and mostly borne by P3. |
| **9. Complexity** | Moderate–high. |
| **10. Generalises?** | The *frame* generalises. Every specific metric proposed except form coverage degrades or misleads on at least one named target category. |

**11. Verdict.** **Prototype first.** Concretely: (a) fix the signature defect now — it is small and
it is the current early-stop; (b) add information-gain-per-step as an *observed* number for a
while, changing no behaviour; (c) only then let it stop a session, always under `max_steps` as a
hard ceiling. Do not adopt navigation coverage as a target at any point.

---

### Proposal 7 — Requirement-driven exploration

**1. Summary.** Requirements steer exploration: "verify fund transfer" makes the explorer seek
transfer/payment/beneficiary/account rather than wander.

**2. Advantages.** **Probably the best return per line of code in the whole set**, and the evidence
is unusually direct. On ParaBank a human had explicitly permitted `Transfer Funds` through the
allow-list (D-429, C-102). The session never reached it. It spent 22 of 26 steps on the account
activity page. The requirement was known, the target was known, the control was permitted — and
nothing connected the three.

**3. Disadvantages.**

- **Tunnel vision.** An explorer that follows only requirement keywords writes a suite covering the
  requirement and nothing else. The product's measured claim (D-411) is *regression-catching* —
  green on the app you have, red when it breaks. A narrow suite is a worse regression net. Needs an
  explicit split (e.g. ~70% directed / ~30% breadth) and the split should be a parameter, not a
  constant buried in a prompt.
- **The vocabulary gap, and this repo has already measured it.** C-90, verbatim: *"Lexical ranking
  cannot bridge a vocabulary gap, and no amount of weighting will."* A requirement saying "move
  money between my accounts" against a nav item saying "Funds Transfer" shares no token. A
  lexical affinity scorer will fail exactly where the customer's language differs from the
  vendor's, which is most enterprise software.
- **Requirements do not name entry paths.** "Verify users can transfer money" does not say it is
  behind Accounts → Activity → Transfer. Keyword affinity helps only on the last hop, and the last
  hop is not where the 22 steps were lost.

**The hybrid that follows from those two objections:** do **not** build a lexical ranker. The model
is already choosing an action every step and is already given the requirement. What it lacks is a
**ledger**: which requirement terms/intents have been seen anywhere in this session, and which have
not. Hand it "you have not yet seen anything resembling *transfer*, *beneficiary* or *payee*;
these are the candidates most likely to lead somewhere new" — that is a context change, nearly free,
and it degrades gracefully where lexical matching fails because the model does the matching.

| | |
|---|---|
| **4. Hidden assumptions** | That requirement vocabulary matches UI vocabulary (C-90 says no); that the target is reachable from where the explorer is; that one requirement maps to one journey — in banking it maps to three roles and two approval states. |
| **5. Technical risks** | Low for the ledger version. High for a ranker, which will be tuned against three benchmark apps and overfit. |
| **6. Product risks** | A suite that covers the requirement and misses the regression net the customer is buying. |
| **7. Operational risks** | "It never found the feature" becomes a support case that needs an answer — which is P8's refusal path. |
| **8. Cost** | Low. Days. |
| **9. Complexity** | Low. |
| **10. Generalises?** | The ledger version yes. A lexical ranker does not, and C-90 is the measurement. |

**11. Verdict.** **Implement now — the ledger version.** Validate offline against the stored
ParaBank session first (E6): would the signal have promoted `Transfer Funds` over the dropdowns?
If a lexical scorer fails that test — and C-90 predicts it will — that is the argument for model
steering settled before any code ships.

---

### Proposal 8 — Explicit uncertainty management

**1. Summary.** The model should recognise insufficient evidence rather than filling gaps.

**2. Advantages.** Directly addresses the residue that constraint cannot remove, and it matches a
decision already taken (findings §7.1): explore more rather than write a weak suite, **and** label
any weak tests that remain. Both.

**3. Disadvantages — the one that decides the design.**

> **A confidence score produced by the model is worthless here, and this platform has the receipts.**

D-417: a confident summary with zero files, stored as success. D-421: the same shape recurring on
roughly one session in three, with *"a confident summary, assertion rows describing tests"* and an
empty file list. D-420: `specified` claims had to be downgraded in code because self-report could
not be trusted. C-109: a clean audit — 144 grounded locators, 0 invented — on a suite where 10 of
21 tests failed.

**The model is at its most confident precisely where it is filling a gap.** Asking it to self-assess
samples the wrong distribution.

So every confidence input must be a fact the platform observed, not a number the model reported:

| Signal | Source | Cost |
|---|---|---|
| Was this element observed this session? | observation store | free |
| Was this value observed **at the state the test builds**? | observation store + state id | free — and it is C-103's fix |
| Did any requirement term appear anywhere in the session? | requirement ledger (P7) | free |
| Fraction of steps that produced no new state | information gain (P6) | free |
| How did the session end — `done`, `stalled`, or budget exhausted? | already recorded | free |
| Fraction of the page withheld by the policy | `refused_summary` (D-426) | free |

**4–9 and the operational design.** The failure mode to design against: **a refusal that fires
wrongly costs the customer the entire paid session and returns nothing** — strictly worse than a
weak suite they can read. So the ladder is:

1. Confidence low **and budget remains** → explore more. Never refuse while steps are affordable.
2. Confidence low **and budget exhausted** → generate, label the weak tests, say plainly what the
   suite does not rest on.
3. Refuse outright only on the degenerate case already handled (D-417's zero files).

| | |
|---|---|
| **4. Hidden assumptions** | That the signal can be validated before it is wired up. The findings document demands exactly this — *"validate the signal against the sessions already on disk"* — and **it cannot be done today**, because the sessions on disk contain a count of what was seen, not what was seen. Appendix A.3. |
| **5. Technical risks** | A threshold tuned on three benchmark apps. Mitigate by reporting the number for weeks before it gates anything. |
| **6. Product risks** | Paying for exploration and receiving a refusal. Ladder above exists to prevent it. |
| **7. Operational risks** | Extra steps cost money; the confidence rule must be inside the quoted ceiling. |
| **8. Cost** | Low — every input is already computed or nearly free. |
| **9. Complexity** | Low. |
| **10. Generalises?** | Yes. Observation-derived signals are application-independent. |

**11. Verdict.** **Implement now — computed, reported first, gating later.** Never add a
`confidence` field to the tool schema.

---

### Proposal 9 — Retrieval-based generation

**1. Summary.** Replace the single large prompt with `Requirement → Retrieval → Evidence →
Generation`.

**2. Advantages.** Correct eventually. Necessary at enterprise scale.

**3. Disadvantages.** Three objections, each sufficient on its own to postpone.

**(a) It optimises the cheap half of the call.** The generation call is **3,818 in / 11,764 out**.
It is output-dominated. Retrieval reduces input. The economics do not support it as a cost measure,
and the brief lists cost implications as a criterion.

**(b) There is no corpus.** Retrieval answers "how do I find the relevant 5% of a large body of
evidence". Today the body is one application, one session, ~10 screens, ~1.5 pages of notes. There
is nothing to retrieve *from*. Building an index over that is infrastructure in search of a
problem.

**(c) This repository has already measured that the naive version fails.** C-90: asked for the file
implementing a specific behaviour, lexical ranking put the correct file **22nd**, with the right
reason attached, because a filename match on a common word outscored body evidence on three rare
ones. The verdict in that row — *"deliberately not tuned further: with no eval set any further
constant-fiddling is unfalsifiable and would be overfitting"* — transfers here exactly. And the
alternative, semantic retrieval, means adding embeddings and a vector store to a codebase that
deliberately has neither: `finding_pipeline.py` states the property in as many words — *"no
embeddings, no vector store — so they are free, repeatable, and testable offline."* Breaking that
is a real architectural cost, not a library import.

**The alternative that gets most of the benefit now, for much less:** **shard the generation call
by journey.** One call per test file, given only that journey's evidence plus the requirement it
serves. No index, no embeddings. It bounds the *output* (the expensive half), it narrows what each
call can hallucinate from, and it makes D-421's empty-files failure partial instead of total —
today one bad response loses the entire paid session.

| | |
|---|---|
| **4. Hidden assumptions** | That input size is the constraint (it is not); that relevance is computable without a knowledge model (it is not); that lexical retrieval works (C-90 measured otherwise). |
| **5. Technical risks** | Retrieval misses → the model fills the gap → the exact failure being fixed, now with a plausible-looking provenance trail. |
| **6. Product risks** | Months of infrastructure with no measurable quality change. |
| **7. Operational risks** | A vector store is a new stateful dependency, a new backup surface and a new offline-testability problem. |
| **8. Cost** | High, and aimed at the wrong half of the bill. |
| **9. Complexity** | High. |
| **10. Generalises?** | Yes — but only once an application model spans hundreds of screens. That is the trigger. |

**11. Verdict.** **Postpone.** Do journey-sharding now. Revisit retrieval when a single
application's model exceeds roughly what one prompt can hold — a threshold worth writing down
rather than eyeballing.

---

### Proposal 10 — Specialised agent fleet

**1. Summary.** Explorer, Inventory, Knowledge, Coverage, Test Strategy, Test Generator,
Verification agents.

**2. Advantages.** Clear responsibility boundaries; independently testable contracts.

**3. Disadvantages.** **Four of the seven are arithmetic, not judgement.**

| Proposed agent | What it actually is |
|---|---|
| Inventory Agent | DOM parsing and serialisation — **deterministic code** |
| Coverage Agent | counting sets — **deterministic code** |
| Verification Agent | running a test suite and reading JUnit — **already deterministic code** (`--verify`, `run_tests.py`) |
| Knowledge Agent | graph writes with identity resolution — **mostly deterministic code** |
| Explorer Agent | choose one action given a page — **genuinely a model**, exists |
| Test Generator Agent | write test code — **genuinely a model**, exists |
| Test Strategy Agent | decide what is worth testing — **genuinely a model**, does not exist |

Turning a computation into an agent does not just cost money and latency. **It converts a fact into
a claim.** This platform's entire recent history runs the other way: `finding_gate` floors a
model's verdict deterministically; `attest.coverage_claim` reconciles claimed coverage against
actual input; `assertion_audit` reads the code rather than the model's description of the code,
explicitly because *"a claim is not evidence"* (D-406). An Inventory Agent that reports what it saw
is a claim about what it saw, and nothing checks it.

Second objection, decisive at solo capacity: **observability**. Today a bad suite means reading one
session row, N step rows and one result. Seven agents means seven traces to correlate. This
project's debugging story is already thin — C-106 is an unreproduced flake whose identity was
*destroyed by the harness that found it*.

Third: **each boundary is a place to lose a paid session** (D-440 again). Seven boundaries, seven
resumption paths, or seven ways to spend a customer's money and return nothing.

| | |
|---|---|
| **4. Hidden assumptions** | That specialisation improves quality (unevidenced — the measured failures are grounding failures, which no amount of role separation fixes); that agent count correlates with capability. |
| **5. Technical risks** | Error amplification down a chain; each hop can confabulate its input. |
| **6. Product risks** | Cost per session multiplies with no measured quality gain. |
| **7. Operational risks** | Debuggability collapse; metering across seven call sites; seven retry policies. |
| **8. Cost** | High recurring (model calls) **and** high one-off. |
| **9. Complexity** | Highest in the document after P3. |
| **10. Generalises?** | Neutral — it is an implementation style, not a capability. |

**11. Verdict.** **Reject as drawn.** Accept the half that is real: **separate the stages and their
data contracts (P4), keep one model role per stage where a model is genuinely needed.** Revisit a
Test Strategy agent only if measurement shows generation failing at *selection* rather than at
*grounding* — today every measurement says grounding.

---

## 3. Missing opportunities

Ranked. **M1 and M2 are more important than eight of the ten proposals.**

### M1 — Constrained assertion generation (apply D-408 to the writer) ★ highest value

The platform solved "the model must not invent an element" for actions: it returns a candidate
index, the driver resolves it, a guessed selector is impossible. For assertions it solved nothing —
free-form JavaScript, audited afterwards.

**Proposal:** the generator returns structured assertions —
`{element_ref, matcher, value_ref | literal, basis, requirement_ref}` — and a deterministic
compiler emits Playwright. The compiler refuses any `element_ref` not in the store and any
`value_ref` not observed at the state the test builds.

What this makes **unrepresentable rather than detectable**:

| Defect | Today | With M1 |
|---|---|---|
| Invented CSS selectors (C-101) | audited, retried once, residual named | cannot be expressed |
| Invented values (C-103) | open, unfixed | `value_ref` must resolve to an observation |
| Invented credentials (C-109) | invisible to every audit | fixtures come from the session or fail |
| Wrong accessible name (D-427) | cost two live suites every test | compiler uses the observed addressing |
| Heading nobody saw (D-438) | audited | cannot be expressed |
| Contradicts the record (D-442) | **audit is inert — Appendix A.1** | cannot be expressed |

It also makes P5 free: the evidence chain is a foreign key, not an annotation.

**The honest risk, and it is the reason to prototype rather than commit:** expressiveness. Some
legitimate tests need real code — the negative-assertion pattern the prompt teaches
(`page.request.post(...)` asserting 4xx), loops, conditional setup. If fewer than ~70% of
assertions in the existing suites can be expressed as triples, the compiler becomes a hybrid and
much of the benefit leaks back out. **That is experiment E3, it is free, and it should run before
anything else in this document is built.**

### M2 — Store what was observed, not a count of it ★ cheapest, unblocks four proposals

`app_steps.candidates_offered INTEGER`. The platform records *how many* controls it saw and
discards *which*. The findings document's plan assumes otherwise (*"the data already exists and is
thrown away"*), and every offline validation proposed — the confidence signal, the coverage
measure, the retrieval index, an inventory A/B against history — is blocked on it.

Days of work. It is a prerequisite for P2's evaluation, P3, P6, P8 and every experiment in §9
below. **Nothing else should start first.**

Note the obligation it carries: observations are page content, so they fall under D-418's regime —
encrypted, expiring, refusable — not a new store with new rules.

### M3 — Fix the temporal model (bug family A) ★ prerequisite, currently scheduled last

One `app_steps` row holds three moments: the page **before** the action, the action, and the
outcome of the **previous** action. Three separate render sites have been patched to shift the
pairing (D-428 cosmetic, D-430 two live suites with every test failing, D-434 eight of ten
assertions wrong by one step). The cause has never been fixed.

**The findings document defers this to item 6 of 6. That ordering is wrong, and the proposals are
why.** P2, P3, P5 and P8 each add a new consumer of that row. Adding four readers to an ambiguous
record before disambiguating it converts a three-instance bug family into a seven-instance one.
Two explicit columns — `observed_before`, `observed_after` — cost a schema change and a migration.
Do it before the inventory, not after.

### M4 — Authentication and RBAC as first-class ★ largest generalisation gap

The target list names banking, insurance, healthcare, government, CRM, HR and "role-based access
control". Today **one session = one credential = one role**. The eval's own `task-ownership` case
is an authorisation bug whose separating assertions are negative ("must be refused") — and the
session architecture cannot express "explore as A, assert refusal as B".

Absent from all ten proposals. It is the difference between a demo product and one that sells into
the named markets. Needs: multiple credential sets per session or per application model, a role
dimension on every observation, and generated suites that parameterise the persona.

### M5 — Iframes

C-104, deliberately out of scope (D-436). Salesforce, SAP, payment steps, consent dialogs and
embedded legacy portals are all frames. For a target list containing CRM and insurance portals this
is a blocker, not a caveat. It is genuinely hard — a frame is a separate document with its own
locator scope, so it changes what an *address* means in the observation contract — which is why it
belongs on the 6–12 month plan with a real design, not on a backlog.

### M6 — Test data, state reset and independence

Generated tests share one application. A test that adds to a basket changes what the next test
sees. The prompt asks for independent tests; nothing verifies it, and on an application with
persistent data this is the most common reason a real suite becomes flaky. No proposal addresses
setup/teardown, fixture data, or ordering. This is what decides whether a suite survives its
second week.

### M7 — Human-in-the-loop at the point where it is cheap

Today the human sees the suite after paying for exploration **and** generation. A far better
intervention point: **after exploration, before generation** — show the discovered map and let a
person tick the journeys that matter. One free call. It uses the stop channel that already exists
(D-423), and it directly answers the risk the original plan named and never closed: *"a journey
graph is not a user journey."*

### M8 — Deterministic crawl for enumeration; the model for judgement

At 16 steps, step calls are cheap. At 200 they dominate. Enumerating reachable states is a graph
walk, not a judgement — a deterministic frontier crawler can do most of it, with the model spent on
*which frontier to expand* and *what is worth testing*. Worth a cost prototype before coverage-driven
exploration makes sessions much longer.

### M9 — Learning across sessions: yes per application, no across applications

Per-application accumulation is P3's real value. **Cross-application learning should be rejected,
or restricted to structure.** C-109 is literally what cross-application memory looks like: the
model supplied `locked_out_user`, a real SauceDemo account, because SauceDemo is famous. Learning
"a login form usually has two fields and a submit" is safe; learning values, credentials or
selectors across customers is the defect with a roadmap.

### M10 — Suite maintenance and flake policy

Deliberately deferred in the original plan, correctly, because it needs run history. At the
6–12 month horizon it is what decides renewal: a regression net that goes red on every deploy for
unrelated reasons gets switched off, and takes the next one with it (D-435 makes this point about
a single crying-wolf suite; it applies to the product).

---

## 4. Recommended architecture

```
CUSTOMER'S NETWORK                              PLATFORM
────────────────────                            ────────────────────────────────

 browser (Playwright)
   │
   │ 1. OBSERVE
   │    collect: regions → controls → values
   │    (semantic pass + repeated-subtree
   │     inference; shadow roots walked)
   │
   ├── POST /step ──────────────────────────►  screen candidates (action_policy)
   │      { outline, candidates[i], texts }         │
   │                                                ▼
   │                                          ┌──────────────────────┐
   │                                          │  OBSERVATION STORE   │ ◄── M2
   │                                          │  screens, regions,   │
   │                                          │  elements, values,   │
   │                                          │  transitions         │
   │                                          │  observed_before /   │ ◄── M3
   │                                          │  observed_after      │
   │                                          └──────────┬───────────┘
   │                                                     │
   │                                          model: choose ONE action
   │                                          (index only — D-408)
   │◄─── { action_kind, candidate_index } ───────────────┘
   │
   │ 2. re-screen locally, execute, loop
   │    stop on: coverage target │ no-progress │ max_steps (hard)
   │
   └── POST /generate ──────────────────────►  ┌──────────────────────┐
                                               │  UNDERSTANDING       │ ◄── P3 layer A
                                               │  state graph,        │     P4 data rule
                                               │  workflows, entities │
                                               └──────────┬───────────┘
                                                          │ per journey (sharded)
                                                          ▼
                                               model returns STRUCTURED assertions
                                               { element_ref, matcher,
                                                 value_ref, basis, req_ref }   ◄── M1
                                                          │
                                                          ▼
                                               ┌──────────────────────┐
                                               │ ASSERTION COMPILER   │  deterministic
                                               │ refuses unknown refs │  ← C-101/103/109
                                               └──────────┬───────────┘  ← D-427/438/442
                                                          ▼
                                               Playwright suite + evidence chain
                                                          │
                                               reviews row (review_type=app_testing)
                                                          │
   ◄────── --verify ──────────────────────────────────────┘   D-435, unchanged
```

**What is preserved and why — these are not up for revision:**

| Decision | Why it survives |
|---|---|
| **D-407** — the contract is the interface, not the browser | No SSRF surface, no credential leaves the customer, an app behind a VPN works. A hosted fleet could never reach the environments most worth testing. |
| **D-408** — index, never a selector | The entire safety story. M1 extends it rather than replacing it. |
| **D-418 / D-425** — encrypted, expiring, refusable page content | The observation store inherits this regime rather than inventing a new one. |
| **D-420** — claim ceilings enforced in code | The template for everything in P8. |
| **D-419** — no session without a basis | Prevents the furniture-test failure at the door. |
| **D-429** — allow-list frozen onto the session, exact match | The index is only safe while both sides screen identically. |
| **D-435** — run the suite against the app it came from | The only check that catches wrong values, wrong credentials and invented fixtures. |
| **Deterministic checks over model self-report** | D-406, and the reason to reject P10. |

**What should be revisited:**

| Decision | Why |
|---|---|
| `page_signature` includes live-region text (D-411/D-416) | Disables the no-progress rule on dashboards — Appendix A.2 |
| `MAX_CANDIDATES = 60` in document order | C-107: 60 of 60 to a sidebar |
| `_journeys_markdown` as the generation input | P4's data rule deletes it |
| Reuse of `TEST_AUTOMATION_TOOL_SCHEMA` | The original plan called this "the first thing to revisit if the two outputs diverge". M1 diverges them. |
| One session → one suite | M4 and P3's multi-session accumulation both need this relaxed |

---

## 5. Recommended implementation sequence

Strictly serial — solo capacity, and each step moves numbers the next one is measured against.

```
E3 (free)      Can assertions be expressed as triples?  ── decides whether M1 is the strategy
   │                                                        or a partial optimisation
   ▼
M2 + M3 + A.1 + A.2  ── store observations; split before/after; two verified defects
   │                    offline-testable, no baseline movement expected (confirm it)
   ▼
E1 (free)      Outline on React / Angular / component-library apps
   │           ── decides whether P2 needs region inference before it ships
   ▼
P2 + C-107     Structured outline + per-region candidate budget, behind a flag
   │           ── ONE re-baseline, plus the agreed fourth eval case (oversized navigation)
   ▼
M1             Constrained generation + compiler, behind a flag
   │           ── the big bet; re-baseline; keep free-form as fallback until it wins
   ▼
P7 + P8        Requirement ledger; computed confidence, reported not gating
   │           ── validate against stored sessions (possible only after M2)
   ▼
P4 + P3(A)     Understanding store; generation reads only from it; multi-session identity
   ▼
P6             Coverage as early-stop, max_steps retained as hard ceiling
   ▼
M4 → M5 → M7 → M6/M10      RBAC, frames, human-in-the-loop, flake policy
   ▼
P9 / P3(B) / P10           only if a measured trigger fires
```

---

## 6. 90-day roadmap

| Weeks | Work | Effort | Impact | Risk | Success metric |
|---|---|---|---|---|---|
| 1 | **E3** — express existing suites' assertions as triples. Free, no code. | 1d | Decides the strategy | none | ≥70% expressible → M1 proceeds as designed |
| 1–2 | **M2** observation capture; **M3** `observed_before`/`observed_after`; **A.1** + **A.2** defects | 1w | Unblocks everything | migration on live rows | offline net green; re-baseline **unchanged** (9/9, ±0.01 $/session) |
| 3 | **E1** — outline probe on React / Angular / Shoelace | 2d | Decides P2 shape | none | outline ≤1,500 tok and names ≥80% of content groups |
| 3–6 | **P2** structured outline + region inference + **C-107** per-region budget, behind a flag | 2–3w | Highest single-change | prompt dilution | ≥9/9 retained; **green-on-explored** improves on a live SauceDemo run |
| 6 | Fourth eval case (oversized navigation) + extra repeats on `basket-total` | 3d | Makes C-107 visible | — | scorecard fails when the cap regresses |
| 7–10 | **M1** constrained generation + compiler, behind a flag | 3w | Highest ceiling | expressiveness | invented locators → **0 by construction**; ≥9/9 retained |
| 11–12 | **P7** requirement ledger; **P8** computed confidence, reported only | 1.5w | Directly targets C-105/C-102 | tunnel vision | ParaBank reaches `Transfer Funds`; wasted-step ratio < 0.4 |

**90-day exit criteria:** a live run against one non-benchmark application produces a suite that is
**100% green against the application it was written from**. That is the number the current
benchmark cannot see and the one the product lives or dies on — today it is 11/21 on SauceDemo and
0/16 on ParaBank.

**Spend:** roughly 3 re-baselines at ~£1.90 plus live runs — call it £15–25 total. Trivial against
the engineering time; the constraint is serial validation, not money.

---

## 7. 6-month roadmap

| Work | Effort | Impact | Risk | Depends on |
|---|---|---|---|---|
| **P4** data rule — generation reads only the understanding store; `_journeys_markdown` deleted | 1w | Structural | regression in a working path | M2, M3, P2 |
| **P3 layer A** — durable per-application store, state identity, multi-session accumulation | 3–4w | Unlocks large applications | state identity done wrong poisons all coverage | P4 |
| **P6** coverage as early-stop under a hard `max_steps` | 2w | Cost + quality | metrics as objectives | P3(A) |
| **M4** RBAC / multi-persona sessions | 3w | Largest market unlock | session model change | P3(A) |
| **M7** post-exploration human review of the discovered map | 1w | High value per line | — | P3(A) |
| Journey-sharded generation (the P9 alternative) | 1w | Bounds output; partial failure | more calls per session | M1 |

**Metrics at 6 months:** green-on-explored ≥90% across five real applications; requirement reach
≥80%; wasted-step ratio <0.25; a suite can be accumulated across ≥3 sessions of one application.

---

## 8. 12-month roadmap

| Work | Trigger for starting it |
|---|---|
| **M5** iframes — extend the addressing contract to frame scope | a named customer application blocked on it |
| **P3 layer B** — business entities, domain vocabularies | measurement shows typed entities beat structural outlines |
| **M10** flake policy, quarantine, self-healing | ≥3 suites with ≥10 replay runs of history |
| **M6** test data / state reset | first customer suite that fails on ordering |
| **P9** retrieval | one application's model exceeds what a single prompt can hold |
| Hosted browser runner | only for customers whose environment is publicly reachable; D-407 keeps it additive |
| **P10** Test Strategy agent | measurement shows failures of *selection*, not grounding |

**12-month target architecture:** the diagram in §4, at multi-session and multi-persona scale —
a durable per-application understanding store fed by many sessions and many roles; generation that
can only reference what was observed; and a verification loop that runs the suite against the
application it came from on every write. Retrieval and specialised agents remain **out** unless a
written trigger fires.

---

## 9. Validation experiments before investment

Every one is free or near-free. Three of them can falsify a top recommendation, including mine.

| # | Experiment | Cost | Falsifies |
|---|---|---|---|
| **E1** | Serialise a structured outline for a React, an Angular and a component-library page. Measure tokens and whether content groups are nameable. | free | **P2.** If outlines exceed the flat list or degenerate to anonymous containers, P2 needs region inference before it ships — or is a benchmark optimisation. |
| **E2** | Replay a stored session: rebuild the generation input from the observation store and regenerate. Compare against the 21-test SauceDemo suite. | ~£0.15 | **P1/P2.** If a richer input does not reduce failures, the diagnosis in the findings is wrong. *(Blocked until M2.)* |
| **E3** | Take the existing SauceDemo and ParaBank suites. Express every assertion as `(element_ref, matcher, value_ref)`. Count what will not fit. | free | **M1 — my top recommendation.** <70% expressible and the compiler is a hybrid, and most of its guarantee leaks away. |
| **E4** | Replay stored page signatures against a synthetic live region. | free | **A.2.** Confirms the no-progress rule's blind spot at the scale it occurs. *(Partially blocked until M2.)* |
| **E5** | Simulate a navigation-coverage reward over the stored ParaBank and Shoelace traces. | free | **P6.** If it drives the explorer into the sidebar, navigation coverage is disqualified as a target — as predicted. |
| **E6** | On the stored ParaBank session, would term affinity have ranked `Transfer Funds` above the dropdowns? | free | **P7.** If lexical fails — C-90 predicts it will — model steering is settled before code. |
| **E7** | Point the current explorer at a Salesforce/SAP-style app. Count what is behind frames. | free | **M5's priority.** Turns "frames are a caveat" into a number. |

---

## 10. Success metrics

**Keep.** `discriminates` (9/9 today), cost per session ($0.135), and — most importantly — the rule
that **the denominator is every session attempted.** A session that produced nothing is a failure,
not an exclusion.

**Add.** The first is the one the current scorecard structurally cannot see.

| Metric | Definition | Today | 90-day | 6-month |
|---|---|---|---|---|
| **Green-on-explored** ★ | % of sessions whose suite is 100% green against the application it was written from | 0/2 live (10/21 and 16/16 failing) | ≥1 app at 100% | ≥90% over 5 apps |
| **Grounding rate** | % of assertions whose element *and* value resolve to a stored observation | unmeasurable | measurable | ≥98% |
| **Requirement reach** | % of supplied requirements with ≥1 journey touching a screen containing their concepts | unmeasured | ≥60% | ≥80% |
| **Wasted-step ratio** | steps yielding no new state ÷ total steps | 0.85 (ParaBank) | <0.4 | <0.25 |
| **Withheld-page fraction** | controls refused ÷ controls offered, per page | recorded, unreported | reported | <0.15 |
| **Suite survival** | % of tests still green 7 days later on an unchanged app | no history | — | ≥95% |

**Two anti-metrics — track them so they cannot be gamed:** *tests generated* (padding is free) and
*locators grounded* (C-109: 144 grounded, 0 invented, 10 of 21 failing). Neither may ever appear as
a headline.

---

## Appendix A — three things verified, not assumed

### A.1 — `contradicts-record` is inert in production ★ live defect

D-442 (shipped 2026-09-17, commit `da40b45`) counts a suite that writes `getByRole` where the
session was told to use a raw attribute selector. It was built from a ParaBank run that failed
16 of 16 for exactly that reason.

`COLLECT_JS` emits three addressing forms; one is `locator("input[name='username']")`. The route's
sanitiser (`backend/routes/app_testing.py:405`) accepts **only** `^getBy(Role|Text|Label)\(…\)$`.
So that form is stored as `""`, `observed_locators` drops it (empty locators are filtered), and
the check at `backend/services/locator_audit.py:306` — `if not offered or not
offered.startswith("locator(")` — can never pass.

Verified by execution against the real module:

```
stored=''  <- emitted locator("input[name='username']")

A) observed_locators as the route actually builds it:   invented: 0  kinds: {}
B) observed_locators as a hand-written fixture builds it: invented: 1  kinds: {'contradicts-record': 1}
```

Two consequences. The detector cannot fire on the case it was written for. And — worse —
**`_journeys_markdown` emits its "address it with" clause only when a locator is present, so for
`input[name=…]` fields the suite writer is told nothing about addressing and must guess.** The
D-442 *cause* is fully intact; only the symptom detector was added, and it is dead.

This is the process failure the findings document names in §11 — *"testing new checkers with
invented inputs"* — reproducing within a day of being written down. A fixture that hands
`locator_audit.audit()` a hand-built list passes; production cannot.

### A.2 — the no-progress rule switches itself off on dashboards ★ live defect

`page_signature` folds page text into the hash (D-416, deliberately — on a client-rendered page the
text is the only thing that changes). `TEXTS_JS` collects `[role=status]` and `[aria-live]` — live
regions. So any page with a clock, a "last updated" stamp, a notification count or a toast produces
a **new signature on every visit**, and `app_session.stalled` — which asks whether the recent
signature window is a subset of what was seen before — can never answer yes.

Verified against the real function:

```
Same page, revisited 4 times, one live region:   4 distinct signatures
Same page, revisited 4 times, no live region:    1 distinct signature
```

C-99 anticipated this for the *browser-side* countdown as an n=1 observation. What it shows is that
the **server-side** rule (D-441) — the platform's only defence against C-105's measured 22-wasted-steps —
is disabled on precisely the "dashboard-heavy applications" named in the brief. Every such session
runs to `max_steps`.

This is why P6 is not merely an improvement: the current stopping behaviour is worse than the
brief describes.

### A.3 — the observations were never stored

`app_steps.candidates_offered` is an `INTEGER`. The 240 controls the explorer saw were not
truncated on the way to the writer — they were **never persisted**. What survives is a count, the
chosen action, and an encrypted 28×160 page-text digest.

The findings document's plan (*"the data already exists and is thrown away"*) and its instruction to
*"validate the signal against the sessions already on disk"* both rest on data that is not there.
This is why M2 must precede everything, and why three of the seven experiments in §9 are blocked
until it lands.

---

## Appendix B — proposed `DECISIONS.md` rows

Next free ids: **D-446**, **C-110**. (D-445 and C-109 are the current highs; C-91, C-98 and C-110
do not exist.)

- **D-446** — Observations are stored, not counted. `app_steps.candidates_offered` recorded a
  number and discarded the controls, so no offline analysis of a past session was possible and the
  suite writer's 5% was a storage decision rather than a prompt one.
- **D-447** — A step records `observed_before` and `observed_after` explicitly. Three render sites
  patched (D-428, D-430, D-434), cause never fixed; the inventory work adds four more readers.
- **D-448** — The generator selects element references and assertion kinds; a compiler emits the
  code. D-408 applied to the writer, which is where every recalled-information defect lives.
- **C-110** — `contradicts-record` (D-442) cannot fire: the route's locator sanitiser drops the one
  input shape that triggers it. Verified by execution; see Appendix A.1.
- **C-111** — `page_signature` folds live-region text in, so the no-progress rule is off on any
  page with a clock or a status line. Verified; see Appendix A.2.
- **Amend C-105** — "deferred twice" is superseded by D-441, and D-441's mitigation does not apply
  to the class of page in C-111.
- **Amend C-101(a)** — closed by D-438; the row still reads "was not done here".
- **Amend D-424** — states the loop detector is browser-side because fixing `stalled` would move
  D-411's floors; D-441 then changed `stalled`. The row was never updated.

---

## Appendix C — decisions that are yours, not mine

Per `CLAUDE.local.md`, these are surfaced rather than decided:

1. **Screenshot and trace storage (P5).** Screenshots of a test environment seeded from production
   contain real names, balances and records. The platform has no binary storage by deliberate choice
   (D-377). Building it is a privacy commitment, not a feature.
2. **Knowledge-model retention (P3).** A durable per-application store holds more customer content,
   for longer, than D-418's 7-day digest. What window, what disclosure, and whether a customer may
   refuse it and still use the product.
3. **Pricing basis (D-410).** Still deliberately unpriced. Coverage-driven stopping (P6) makes
   session length variable, which changes what a quote means — settle the basis before P6 ships.
4. **C-102 — permitting money-moving controls.** Unchanged and still unproven: a permitted
   `Transfer Funds` has never actually been driven, and nothing in the platform can verify that an
   address is a test environment.
5. **Whether a hosted runner may ever hold a live credential** to a customer's running system.
   Categorically different from a GitHub read token.

---

## Verification

Nothing above changes code. When implementation starts, each increment is verified by:

```
venv/Scripts/python.exe scripts/smoke_app_testing.py          # 311 checks — keep full output
venv/Scripts/python.exe scripts/explore_app.py --fixture      # 53 checks
node scripts/smoke_render.js
venv/Scripts/python.exe scripts/eval_app_testing.py --self-check   # free harness proof
venv/Scripts/python.exe scripts/smoke_review_sources.py       # §26 AGENT_SCREENS parity
```

then, for anything that moves a measured number:

```
RELOAD=0 venv/Scripts/python.exe run.py                       # never a bare uvicorn (D-243)
venv/Scripts/python.exe scripts/eval_app_testing.py           # ~£1.90 after the agreed changes
LM_PASSWORD=... venv/Scripts/python.exe scripts/explore_app.py \
  --url <app> --api http://127.0.0.1:8000 --user admin --project <id> \
  --requirement-file <file> --max-steps 16 --attest --verify --verify-into <dir>
```

**Two rules this document's own findings enforce.** A sweep must keep full output — C-106 is an
undiagnosed flake whose evidence the harness destroyed as it was produced. And every new checker
must be replayed against a **stored real trace** before a synthetic fixture: A.1 is the third
instance of a checker that passed its own tests and could not fire in production.
