# Next session — one conversion run, with everything watching

**Written:** 2026-08-19 · **Branch:** `modernization_production_pipeline`
**Commits:** `72cf820` … `0290956`

> **Read `DECISIONS.md` first.** Rows **D-94 … D-108** carry the *reasoning* behind everything
> below, one line each, with `file.py` citations. This file carries the *state*: what the plan
> was, what happened to it, and the one thing left.
>
> This file is untracked scaffolding — delete it once the run below is done.
> `MODERNIZATION-SESSION.md` beside it is an **older, stale** handoff; ignore or delete it.

---

## 1. Where the plan got to

The seven-task plan from 2026-08-18 is **done**, except one live run the owner deferred.

| | Task | State |
|---|---|---|
| 0 | One clean verification run | **Done, and it earned its keep** — see §2 |
| 1 | Cross-signal contradiction checks | **Done** — `sanity.py`, 10 checks (D-94…D-96) |
| 2 | Denominators on the assessment | **Done** — `sanity.render_counts` (D-97) |
| 3 | Building as part of the normal flow | **Done** — `build_results.gate` (D-102) |
| 4 | Model audits the extraction | **Done** — `extraction_audit.py` (D-101) |
| 5 | A diverse fixture corpus | **Done** — five frameworks (D-104…D-108) |
| 6 | Parity on a real application | **Half done** — the *asking* is built (D-103); nobody has replayed a real app yet |

**754 → 967 modernization checks.** Plus 164 products, 130 code review, 86 governance, 66 render.

---

## 2. What the live run found, which is the point of having done it

A real 45-file PHP application converted end to end: **45/45 files, 0 failed, 20 components,
78 artifacts, $2.24**. Every offline check was green. Then `dotnet build` on the export failed
during **restore**, with MSB4006, before compiling a line — two projects referenced each other.

Two defects, both in `_wireup_one`, neither reachable by any test that existed:

* **D-99** — `wireup` rebuilt the reference cycle `architecture.normalize_projects` had removed.
  D-93 makes the *proposed* graph acyclic; wireup then rewrites the same references from the
  types the converted code actually names, and adding those on top closed the cycle again.
  `reconcile` caught and reported it correctly — after it had shipped.
* **D-100** — `entry_project` was "whichever project the first component lives in". Components
  are ordered topologically, so on any layered codebase that is the *bottom* layer. It resolved
  to `Acme.Ems.Domain` on a run whose host was `Acme.Ems.Web`, so `program_path` named a file
  that does not exist and **the deterministic host wiring silently did nothing at all** — the
  service registrations whose absence compiles clean and 500s on every request.

With the cycle edge removed, **the whole 78-file export compiles to 3 errors in one file**, all
CS0246 from a single missing `using Microsoft.EntityFrameworkCore.Diagnostics`. That is the
pipeline's real output quality; the cycle was hiding it.

Everything else the run verified: the sanity layer fired live with exactly the one contradiction
predicted offline; the gate rendered its denominators; five controller components shared
`Acme.Ems.Web.Controllers` with no `Controllers2` (D-90); the entry project correction was
recorded (D-93); and D-98 — written that morning — reported four directory collisions that had
previously been silent.

---

## 3. The one thing left

**One conversion run, with all of it switched on.** Nothing below has been seen in a live run:
the wireup cycle fix, the extraction audit, the build ask, the parity invitation, the five
framework recognisers.

```bash
python run.py                                   # restart by hand; see the reloader landmine
python scripts/drive_modernization.py \
    --zip "C:/Users/Rafe Athar Shaikh/Downloads/employee-management-system.zip" \
    --project 446d0389-1cd6-4625-ad9a-082c38151173 \
    --name "EMS full-stack verification" --cap 8 --log run.log
```

**Accept when:**

* the run log carries `analysis_checked`, `extraction_audited`, `build_not_checked` and
  `parity_invited` — the four things that now speak up
* no `reference_cycle_broken` event, **or** one that names an edge no code justified
* `dotnet build` on the export gets past restore (the cycle is what it could not survive)
* the extraction audit's confirmed rows are all `source=model_audit`, and its assessment reads
  like something a human would act on

```bash
python scripts/build_run.py --run <id> --user admin --password Admin@1234 \
    --project 446d0389-1cd6-4625-ad9a-082c38151173
```

That now needs no API key. It posts the compiler's own errors back, reopens the failing units,
and a resume re-converts them — so the 3 CS0246s above should close themselves.

### Before that run, one setting

**`PUBLIC_BASE_URL` is not set.** Without it the export does not carry the parity tools
(`routes/modernization.py:805`) and `POST /github-pr` refuses. For a local run
`PUBLIC_BASE_URL=http://localhost:8000` is enough to let the recorder upload; a GitHub Actions
callback needs a real address (`_unreachable_from_ci` rejects loopback, deliberately — a
workflow that runs and reports nothing looks exactly like one nobody triggered).

### And the half of Task 6 that is still open

Parity has never run against a real application. Everything is built and the run now *asks* at
the plan gate — but the answer still requires somebody to run the recorder against the legacy
PHP for a few minutes before converting, and the replayer against the converted app after.
`php -S` and the .NET SDK are both on this machine, so it is possible; it has just never
been done. **Only parity tells you the behaviour survived.** Every silent defect this pipeline
has ever had — the swapped query, the truncated salary, the improvised style — shows up as a
divergence and as nothing else.

---

## 4. Tools

| Path | What it does |
|---|---|
| `scripts/audit_source_analysis.py` | Point it at any zip or directory. Prints what the deterministic layer concludes, the denominators, then the cross-signal checks — the same ones the run shows, from the same module. Offline, no key, one second. **Run it on every new codebase before converting.** |
| `scripts/drive_modernization.py` | Drives a run end to end over the API, as the browser does. Approves the two spend gates, notes non-blocking checkpoints, stops on a blocking one. |
| `scripts/build_run.py` | `--fixture` compiles a synthetic scaffold offline; `--run <id>` downloads an export, builds it, and posts the diagnostics back. Takes `--user/--password` now. |
| `scripts/smoke_modernization.py` §31–§36 | 213 new checks: the cross-signal checks, the wireup graph, the extraction audit, the build ask, the parity invitation, and the five-framework corpus. |

---

## 5. Landmines

* **The reloader cannot be trusted, and it bit again on 2026-08-19.** A server started *after* a
  commit still served the pre-commit `_run_view` — the new `build` key was missing over HTTP and
  present when the same function was called in-process. Kill by PID, confirm port 8000 is free,
  *then* start. Never edit `backend/` while a run is in flight.
* **The active DB is `demo.db`**, not `sdlc_agent.db`.
* **Every API call needs `X-Project-Id`**, or you get a 403 `project_binding_denied` that says
  nothing about the header. Project `employee-legacy` = `446d0389-1cd6-4625-ad9a-082c38151173`.
  Login `admin` / `Admin@1234`.
* **There is no `pgrep` on this machine.** A background waiter built on it exits instantly and
  reports success, which looks exactly like the thing it was waiting for having finished.
* **The OmniRoute connection cannot serve this pipeline** (C-68). The Anthropic connection works.
* **`.inc` belongs to Delphi**, not PHP — `register()` is last-write-wins over an alphabetical import.
* **Nothing unit-specific may go in the system block** — it is the cached prefix (D-06).
* **Tree-sitter query syntax has no `#` comments** (`;` starts one). And in PHP source, `#` opens
  a comment *except* before `[`, which is an attribute — that one character cost Symfony every
  route it has (D-105).
* **Capping a candidate expansion by candidate count truncates it mid-name.** Cap by name (D-107).

---

## 6. Key files

```
backend/services/modernization/
  sanity.py            cross-signal contradictions + the denominators the gate renders
  extraction_audit.py  the model's claims, and the quote check that decides which survive
  evidence.py          deterministic extraction — hazards, tables, routes, datastore, engine
  analyzers/php.py     two engines behind one analyze(): tree-sitter, then a real regex fallback
  wireup.py            project files corrected from the converted code; keeps the graph acyclic
  architecture.py      normalize_proposal / normalize_projects / materialize
  build_results.py     compiler diagnostics in, rework out — and the "nobody built this" gate
  parity.py            the comparator, the gate, and the invitation at the plan gate
  runner.py            stage driver, claim loop, gates, set_charter, _drain
backend/agents/modernization_intelligence/
  extraction_audit_agent.py   one call: what did the extraction miss?
```

---

## 7. The one-sentence version

The analysis layer now says when it does not understand a codebase, the run now asks whether
anyone compiled it and whether anyone recorded what it used to do — **and the only thing that
still proves the conversion works is a parity replay nobody has run yet.**
