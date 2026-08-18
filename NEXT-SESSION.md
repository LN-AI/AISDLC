# Next session — harden the analysis layer so it fails loudly

**Written:** 2026-08-18 · **Branch:** `modernization_production_pipeline`
**Commits this work follows:** `269f549`, `a258fd8`, `a2852e0`

> **Read `DECISIONS.md` first.** Rows **D-80 … D-93** and caveats **C-68**, **C-70** carry the
> *reasoning* behind everything below, one line each, with `file.py` citations. This file carries
> the *state and the plan*: what happened, what it means, and what to build next.
>
> A written findings report for this session, with the before/after numbers, is published at
> <https://claude.ai/code/artifact/c60aa0ed-abd1-404a-be59-ba468cf085c4> (private to the owner).
>
> This file is untracked scaffolding — delete it once the plan is done.
> `MODERNIZATION-SESSION.md` in this directory is an **older, now stale** handoff from the
> 2026-08-13/14 session; ignore it or delete it.

---

## 1. What happened

A real customer-shaped PHP application was put through the modernization pipeline from the UI:
`employee-management-system.zip` — 45 files ingested, 41 PHP, a **layered PSR-4 app** with a
front controller, a route table, middleware, repositories over **PDO**, services and views.

Every existing PHP fixture in `scripts/smoke_modernization.py` was **procedural**: `mysqli_*` at
file scope, one page per URL, `include` for dependencies, 7 files, 2,716 characters. The
analyzers had been built and measured against that shape and no other.

**Fifteen defects surfaced. Thirteen are fixed, two are recorded as open.** The four that
mattered:

| | Defect | Why it mattered |
|---|---|---|
| D-80 | PDO was not recognised as data access (`db_apis: []`) | `playbooks.select` keys the data-access mapping table on that, so **the section on translating a query was withheld from the prompt** — on a CRUD app |
| D-81 | `->exec()` matched a shell-escape pattern (`\b` succeeds after `>`) | `shell_exec` is a **blocking** hazard, so a correct EF Core translation of the PDO wrapper *every repository depends on* was rejected and retried twice against an identical prompt |
| D-82 | `_sql_near` paired quotes with a 6-char minimum | `['id' => $id]` desynchronised the scan, so **every call site after the first short literal got a different statement's SQL at its own correct line** |
| D-93 | Project references written from the architect verbatim; entry project = `projects[0]` | Shipped `Application → Domain → Web → Application`. **MSBuild refuses a reference cycle**, so `dotnet build` failed before compiling a line — while the run reported 45/45 converted, 0 failed |

Final run: **45/45 files → 25 components, 4 projects, 76 artifacts, $2.73**, `claude-sonnet-4-6`.
Reconciliation honestly reported 2 failures (duplicate interfaces) and 9 warnings.

### Verification state — read this before trusting anything

* **Test-verified and run-verified:** D-80…D-88, D-92 (the ingest/evidence/analyzer fixes). The
  live run above ran on them.
* **Test-verified only:** **D-89, D-90, D-91, D-93.** They landed *after* that run started. The
  reloader logged a restart it never completed (the trap `b3406a3` documents), so the run
  predates them.
* **Therefore: task 0 below is to do one more full run.** Until then, nobody has seen the
  namespace fix or the acyclic project graph in a real export.

---

## 2. The diagnosis that should drive the plan

Do not read the list above as "fifteen bugs, now fixed". Read it as one bug fifteen times:

> **A recogniser encoded an assumption that was true for the fixture and false for the codebase,
> and the pipeline had no way to notice.**

You cannot fix that by writing better recognisers — there is always a next framework. The
tractable observation is this:

**The pipeline already held the contradiction and never looked at it.** In the same run,
`evidence.py` reported `datastore: PDO` and the analyzer reported `db_apis: []`. Both stored.
Nothing compared them. A `schema.sql` with four `CREATE TABLE` statements sat beside zero
data-access call sites. Fifteen routes in a file sat beside "1 route" in the report.

The analysis layer has **no self-doubt**: it reported "no database access" with exactly the
confidence it would have reported a correct finding. *Zero and unknown look identical.*

### Failure modes are not equally bad — and the safeguards are inverted

| Analysis is… | Converted result | How you find out |
|---|---|---|
| **Wrong** (asserts something false) | Confidently incorrect code | **Possibly never.** It compiles and answers 200 |
| **Missing** (says nothing) | Each unit improvises; output is internally inconsistent | Slowly, in review |
| **Noisy** (false positives) | Real findings buried; converter asked to "fix" correct code | Humans stop reading the report |
| **Over-strict** (false hazard) | Files refuse to convert; visible gaps | **Immediately** |
| **Structurally wrong** | Does not build — or builds and is wrong forever | Loud if it fails; silent if it builds |

Ranking: **wrong > missing > noisy > refusing.** Today's safeguards are strongest against the
*loud* modes and weakest against the *silent* ones. D-81 (`->exec`) screamed; D-82 (swapped SQL)
made no sound at all. **The design goal is not a complete analyzer — it is that incomplete
analysis degrades loudly, at a gate where a human is already standing.**

---

## 3. The plan

Ordered by value per unit of effort. 0–2 are the core of it.

### Task 0 — One clean run, to close the verification gap · ~40 min, ~$3

```bash
python run.py                                   # restart cleanly; do NOT trust the reloader
python scripts/drive_modernization.py \
    --zip "C:/Users/Rafe Athar Shaikh/Downloads/employee-management-system.zip" \
    --name "EMS verification run" --cap 8 --log run.log
```

**Accept when:** the export's `.csproj` files have no reference cycle; the entry project is
`*.Web` and is the only one with `Sdk="Microsoft.NET.Sdk.Web"` and a `Program.cs`; no namespace
ends in a digit (`Entities2`, `Repositories4`); `dotnet build` on the export succeeds.

```bash
python scripts/build_run.py --run <run_id>      # already exists; posts diagnostics back
```

### Task 1 — Cross-signal contradiction checks · no AI, no tokens, ~1 day

**The single highest-value item.** New module `backend/services/modernization/sanity.py`, called
from `runner.ingest_and_analyze` after evidence and graph are built. Output: a list of
`{code, detail, left, right}` stored in run stats and rendered **on the discovery gate**, where
the UI already loads parser findings unasked (`40a322c`). Non-blocking warnings, not errors.

Candidate invariants — each is a pair of numbers already in the database:

| Check | Contradiction | Would have caught |
|---|---|---|
| `datastore_without_api` | `datastore` evidence exists, `db_apis` empty | **D-80** |
| `tables_without_queries` | tables named in SQL, 0 `db_sites` | **D-80** |
| `ui_without_routes` | renders UI, >10 files, ≤1 route | **D-83** |
| `entrypoint_ratio` | entry points > max(3, 20% of files) | **D-86** |
| `blocking_hazard_ratio` | >15% of files refuse to convert | **D-81** |
| `unresolved_import_ratio` | >30% of `requires` resolve to nothing | **D-85** |
| `playbook_section_omitted` | a corpus section is withheld — state which and why | **D-80** |
| `ddl_without_tables` | a `.sql` file has `CREATE TABLE`, 0 tables extracted | the DDL guard bug |
| `config_files_without_keys` | `config*`/`.env` present, 0 config keys | **C-70** |
| `project_cycle` | the proposed project graph has a cycle | **D-93** |
| `shared_output_dir` | two components claim one directory | **D-91** |

A working prototype of these already exists as the `SUSPICIOUS` block at the end of
`scripts/audit_source_analysis.py` — **lift the logic from there**, it is already calibrated
against this codebase.

**Accept when:** a smoke section feeds the *pre-fix* EMS facts and gets ≥5 warnings, feeds the
*post-fix* facts and gets ≤1 (the known `config_files_without_keys`), and a tiny well-understood
fixture gets 0. Zero false positives on the existing fixtures is the bar — a noisy check is
switched off within a week (`semantics.py` makes this argument for `strlen`).

### Task 2 — Show denominators, not just findings · ~half a day

The assessment says "1 route". It should say "1 route found; 41 PHP files parsed; 0 files the
parser could not read". Absence is only visible when there is a slot for it. Mostly a
presentation change on the gate payload plus the same numbers `audit_source_analysis.py` prints.

### Task 3 — Make building part of the normal flow · ~1 day, mostly wiring

`scripts/build_run.py`, `POST /runs/{id}/build-results`, `backend/workflows/modernization_pr.py`
and `GitHubService.commit_tree` **all already exist and nothing routine uses them.** One build
per run would have caught D-93 in thirty seconds instead of by reading a zip by hand. Decide:
run it automatically at the end of `reconcile`, or make it a one-click action on the run screen.

### Task 4 — Let the model audit the extraction · one call, cents

The general-purpose net for frameworks nobody wrote a rule for. Show it the file tree, sampled
source, and **the extracted facts**, and ask exactly one thing: *"what did this extraction miss
or get wrong? cite `path:line`."* Then **verify every claim mechanically** — grep the cited line,
confirm or discard, adopt only what survives. That keeps D-04 intact (no guessed edges) while
covering shapes no regex anticipates. Runs before the discovery gate, alongside comprehension.

### Task 5 — A diverse fixture corpus · ~2 days

The root cause was one fixture shape. Add 5–8 per language: procedural, layered/PSR-4 (done —
section 27), Laravel, Symfony, CodeIgniter, WordPress plugin, API-only. Assert expected counts.
**This stops regressions; it does not stop novel shapes** — which is why it comes after 1 and 4.

### Task 6 — Parity on one real application · the only thing that proves behaviour

Everything is built: `parity.py`, `parity_tools.py`, the recorder, the replayer, the
adjudicator, the gate. **They have never run against a real app.** The final run's own log says
so, in its own words:

> `parity_not_recorded` — *No golden cases were recorded for this run, so nothing verified that
> the converted application behaves like the legacy one.*

Be blunt about the trade: better analysis reduces cost and rework. **Only parity tells you the
behaviour survived.** Every silent defect in the table in §2 — the swapped query, the improvised
style, the truncated salary — shows up as a divergence. `GET /runs/{id}/parity-tools` serves the
two programs; `PUBLIC_BASE_URL` must be set for them to be carried inside the export.

---

## 4. Tools left behind

| Path | What it does |
|---|---|
| `scripts/audit_source_analysis.py` | **Point it at any zip or directory.** Prints what the deterministic layer concludes — data APIs, engine, tables, routes, hazards, which files will refuse to convert, which playbook sections get sent — then a `SUSPICIOUS` block of cross-signal checks. Offline, no key, one second. Run it on every new codebase *before* converting. |
| `scripts/drive_modernization.py` | Drives a run end to end over the API, as the browser does. Approves the two spend gates, notes non-blocking checkpoints, **stops on a blocking one**. One line per state change and per event. |
| `scripts/smoke_modernization.py` §27–§30 | 79 new regression checks (675 → **754**) covering everything in §1, with the layered PDO fixture. |

---

## 5. Landmines

* **The reloader cannot be trusted.** `WatchFiles` logged a reload on a `runner.py` edit and
  never completed it — requests were served continuously across the restart. A stale module once
  made a correct fix look broken (`b3406a3`). **Kill the process and restart by hand** after any
  `backend/` edit, and never edit `backend/` while a run is in flight.
* **The active DB is `demo.db`**, not `sdlc_agent.db` — `DATABASE_PATH` in `.env` points there.
  Runs, units, artifacts and events for the runs discussed here are in it.
* **Every API call needs `X-Project-Id`** in this deployment, or you get a 403
  `project_binding_denied` that says nothing about the header. Project `employee-legacy` =
  `446d0389-1cd6-4625-ad9a-082c38151173`. Login `admin` / `Admin@1234`.
* **The OmniRoute connection cannot serve this pipeline** (C-68). Measured: a 650-token forced
  tool call returned in 6.4 s; a 16,000-character one returned **503 after 72 s**; the in-flight
  probe simply hung. With no per-connection `timeout_seconds`, the global 900 s and two retries
  apply — up to 45 minutes of silence. The Anthropic connection works. Set
  `llm_connections.timeout_seconds` per connection; do **not** lower `LLM_TIMEOUT_SECONDS`
  globally (`config.py:69` explains why).
* **`.inc` belongs to Delphi**, not PHP — `register()` is last-write-wins over an alphabetical
  import.
* **Nothing unit-specific may go in the system block** — it is the cached prefix (D-06).
* **Tree-sitter query syntax has no `#` comments** (`;` starts one, `#` is a predicate). An
  explanatory comment inside `_QUERY_SOURCE` is a `QueryError` at load. Explain above the string.

---

## 6. Key files for this work

```
backend/services/modernization/
  evidence.py        deterministic extraction — hazards, tables, routes, datastore, engine
  analyzers/php.py   two engines behind one analyze(): tree-sitter, then a real regex fallback
  graph.py           resolve_edges, soft_requires, _RUNTIME_TYPES, Tarjan/Kahn
  architecture.py    normalize_proposal / normalize_projects / materialize
  playbooks/         the mapping corpus; `select()` decides what reaches the prompt
  advice/            deterministic charter suggestions — `_engine`, `_PROVIDERS`
  validation.py      Tier 0, BLOCKING_HAZARDS, summarize_for_prompt
  reconcile.py       whole-codebase consistency, incl. _project_reference_cycles
  runner.py          stage driver, claim loop, gates, set_charter, _drain
  sanity.py          ← task 1 goes here
scripts/
  audit_source_analysis.py   ← run this first on any new codebase
  drive_modernization.py     ← run a conversion headlessly
  build_run.py               ← compile an export (--fixture or --run)
  smoke_modernization.py     ← 754 checks; §27–§30 are this session's
```

---

## 7. The one-sentence version

The analysis layer is free, deterministic, and decides everything downstream — what the model is
told, which files may convert, and what a customer approves. It will never be complete, so stop
trying to complete it and **make it say when it does not understand a codebase.**
