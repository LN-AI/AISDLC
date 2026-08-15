# Legacy Modernization

Converts a **whole legacy codebase** to a target stack as a durable, resumable run. It reads the source before it writes anything: real parsers derive the dependency graph and record what the application actually contains, an architect decides what the source becomes and where each piece lands, and the work is priced per component before a single file is converted. Secrets are redacted before any model call, a run-scoped symbol registry keeps file 400 consistent with file 4, and the output arrives as a project — solution, project files, host, configuration — rather than a folder of source.

**Module:** Modernization Intelligence
**Product id (for scopes/plans/BYOL config):** `modernization`
**Base path:** `/api/v1/modernization`

## How this differs from Code Migration

`migration-assessment` and `migration-execute` are unchanged and still the right tool for what they do: assess one migration, translate one file. Neither has run identity, a dependency graph, resumability, or an artifact store, and `migration_target` is a single free-text string — so on a 90-file codebase every file independently re-guesses the architecture and nothing in the system can notice that file 1 used EF Core and file 40 used Dapper.

Modernization is a different shape, not a bigger version:

| | `migration-execute` | `modernization` |
|---|---|---|
| Unit of work | one string | a codebase, as rows in a table |
| Identity | none — a pure function over one request | a run row you can ask "how far along are we" |
| Architecture decisions | one free-text string, re-interpreted per file | a validated **charter**, byte-identical in every prompt |
| Order | none | topological, from deterministic parsers |
| Restart | the work is lost | resumes at the next unconverted file |
| Cross-file coherence | nothing compares two files | a symbol registry every later file is shown |
| Quality gate | `len(files) > 0` | structural + symbol + secret validation per file |
| Secrets | scanned *after* generation | redacted *before* ingest |
| Metering | once per call | once per converted file |

## The run lifecycle

```
ingest → parse → dependency graph → evidence          $0, synchronous, no model call
  └──▶ GATE 1  discovery_approval — "assess this for ~$4.20?"
comprehend  what the application *does* — one call per bundle of related
            files, each proposed business rule checked against the source
            it cites before it is allowed to influence anything
synthesize  one call merging every probe into a system portrait
design      N source files → M components, with an explicit
            output-directory and namespace map for the whole run
plan        per-component effort, risk and cost — computed, not guessed
  └──▶ GATE 2  plan_approval — the real number, per component
scaffold    .sln / .csproj / Program.cs / appsettings / PhpCompat.cs
            (no model call at all)
convert     per component, in dependency order
reconcile   duplicate types, namespace drift, unimplemented interfaces,
            stray files, entry-point coverage
  └──▶ build results POST back → failing units reopen → re-convert
            (bounded: 3 rework attempts per unit, 5 rounds per run, and a
             blocking stop when two builds report identical errors)
```

Ingest, redaction, dependency analysis and evidence extraction are synchronous and **cost nothing**. Three gates exist before the codebase is converted, all deliberate:

- **The charter** is refused if incomplete. Leaving those decisions to the model is what produces a codebase that disagrees with itself. It arrives **pre-filled**: `GET /runs/{id}/charter-suggestions` derives an answer for each field from the evidence already extracted, with the observation behind it attached — `mysqli` in use and three tables found means EF Core with a MySQL provider; six of seven files emitting markup means one controller action plus one view per page and a single shared layout; `session_start` means cookie auth, with a warning that legacy PHP password hashes cannot be rehashed without the plaintext. Every suggestion is editable free text and none of them is enforced.

  Those suggestions are **deterministic and free** — no model call is made anywhere on that path, which the smoke suite asserts by checking the call log is untouched. That is not an optimisation: the charter is set *before* Gate 1, so a suggestion that cost money would be the run's first spend with nothing yet approved.

  Fields the codebase makes urgent are promoted from *optional* to *recommended* in the form. Evidence is never allowed to make a field **required**, because a caller POSTing a charter that validates today must not start getting a 400 because the parsers learned something. It can only relax the requirement — a batch program with no database is not asked how it reaches one.
- **Gate 1** approves a small, bounded assessment. It has to exist because pricing a conversion means understanding the codebase first, and understanding it costs money — the alternative was a gate that asked for approval of a number it could not compute.
- **Gate 2** is the one that matters. It carries one row per component — where it lands, how many source files it absorbs, its risk band and its estimated cost — plus a total with a **provable ceiling**: the provider cannot generate more than `max_tokens`, so that figure is an upper bound rather than a forecast.

### What a component is

The unit of conversion is a *component*, not a file. A legacy page-script is markup, a query and controller logic in one file and becomes three; thirty page-scripts sharing `header.php` become **one** layout. Neither is expressible one file at a time, which is why the architect runs first and why source files move to a `rolled_up` status once a component owns them — they are no longer work, but they are not converted either, so progress cannot claim work that has not happened.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/runs` | Create + ingest from `files[]` or `github_repo_url` |
| `POST` | `/runs/upload` | Create + ingest from a zip (multipart) |
| `GET` | `/runs` | List runs visible to the caller |
| `GET` | `/runs/{id}` | Manifest — status, progress, counts, open checkpoints |
| `GET` | `/charter-fields` | The charter's schema, so a UI form can't drift from what is enforced. Run-independent; carries stack-neutral `generic_options` |
| `GET` | `/runs/{id}/charter-suggestions` | A suggested answer per charter field, derived from this codebase, with the evidence behind each |
| `POST` | `/runs/{id}/charter` | Set the binding charter (400 if incomplete) |
| `POST` | `/runs/{id}/start` | 202 + `job_id`; begins converting |
| `POST` | `/runs/{id}/pause` | Stops after the file currently in flight |
| `POST` | `/runs/{id}/resume` | Continues from the last converted file |
| `POST` | `/runs/{id}/cancel` | Terminal; converted output is kept |
| `GET` | `/runs/{id}/units` | The inventory, in conversion order (`?kind=` for components/scaffold, `?kind=all` for everything) |
| `GET` | `/runs/{id}/units/{unit_id}` | One unit with its converted output |
| `GET` | `/runs/{id}/components` | The target architecture: what became what, and where |
| `GET` | `/runs/{id}/evidence` | What the parsers observed, each row with a path and a line |
| `GET` | `/runs/{id}/plan` | The costed conversion plan |
| `GET` | `/runs/{id}/reconciliation` | Cross-file consistency of the output |
| `GET` | `/runs/{id}/rules` | Business rules found in the source, each cited to a line (`?status=` — defaults to `confirmed`) |
| `POST` | `/runs/{id}/build-results` | Report compiler diagnostics — structured rows or a raw `dotnet build` log |
| `GET` | `/runs/{id}/builds` | Build rounds and their messages |
| `POST` | `/runs/{id}/github-pr` | Push the converted code to a branch and open a PR carrying the build workflow |
| `GET` | `/runs/{id}/symbols` | The run's symbol registry |
| `GET` | `/runs/{id}/redactions` | Where secrets were removed — path, line, category |
| `GET` | `/runs/{id}/checkpoints` | Decisions the run is waiting on |
| `POST` | `/checkpoints/{id}/answer` | Answer one |
| `GET` | `/runs/{id}/events` | Append-only run log |
| `GET` | `/runs/{id}/export` | Download the converted codebase as a zip |

## Guarantees, and what they cost

- **Secrets are redacted before any model call.** Credential *files* (`.env`, `*.pem`, `secrets.yaml`, anything under `secrets/`) are skipped entirely; credential *values* inside ordinary source are replaced in place, keeping the assignment so the converted code still has a config key where the literal was. The redaction map records **path, line and category — never the value.** Every entry becomes a configuration key somebody must supply, and they are listed in the export report.
- **Paths are rejected, never repaired.** An archive member that is absolute, has a drive letter, or contains any `..` segment is dropped — including one that would normalize back inside the root. Output paths the model proposes get the same treatment.
- **The dependency graph comes from parsers, not the model.** Python uses stdlib `ast`; PHP uses tree-sitter, with a regex analyzer as a fail-open fallback; Java/Kotlin/Scala, C#/VB.NET and Delphi/Pascal use regex over `package`/`import`/`using`/`uses`. An unknown language produces no edges and converts as a root — the safe wrong answer. A model-produced graph would be silently wrong in a way that corrupts conversion order, and therefore every file after the mistake. The same rule governs *evidence*: an invented entry point sends the architect down a decomposition nothing downstream can recover from.
- **Output paths are not the model's to choose.** The converter returns a bare file name; the directory and namespace were decided once, for the whole run, and are applied in Python. Drift across a long run — file 1 writing `Api/Controllers/X.cs` and file 200 writing `src/Api/Controllers/X.cs` — is not something we detect afterwards, it is something the model has no way to express.
- **The project skeleton is generated deterministically.** No model writes a `.csproj`: asked for one it produces package versions that look right and do not exist. The package set follows from decisions the charter already made.
- **Constructs with no safe equivalent are reported, never translated.** `eval`, variable variables, dynamic includes, `extract`, `unserialize` on request data and shell-outs must come back marked `// MIGRATION-BLOCKED:` — output that quietly resolved one instead **fails validation**. An `eval` turned into a `switch` over the three visible cases looks converted, passes review, and drops every case that was not visible.
- **Cycles are broken deterministically and never silently.** Strongly-connected components are detected; members share an `scc_group`, the intra-cycle edges are removed so the group can convert at all, each member is told which of its siblings does not exist yet, and a checkpoint records it. Cycle checkpoints are non-blocking — legacy code is full of them, and blocking on each would stall every real codebase at file zero — but they appear on the plan you approve.
- **One transaction per converted file is the checkpoint.** Artifacts, symbols, the unit's status and its dependents' readiness all commit together. A crash costs at most one file's tokens, and that boundary coincides exactly with the LLM call boundary. Restart recovery returns in-flight units to the queue and pauses the run; resume continues from the next unconverted file and re-converts nothing.
- **Validation is Tier 0 — thorough, and it executes nothing.** Placeholder detection (`// ... rest unchanged` is a failure, not a warning), bracket/string structural checks, exact `ast.parse` for Python output, unresolved-symbol checks against the run's registry, and a secret re-scan on every emitted file. **Nothing is ever compiled on this host.**
- **The compiler's verdict comes back into the loop.** Whatever built the code — a GitHub Actions job the PR workflow installs, or a developer pasting `dotnet build` output — reports diagnostics to `POST /runs/{id}/build-results`. Each message is mapped back to the unit that emitted the file, failing units return to `ready`, and the ordinary conversion loop re-converts them with the compiler's own errors in the prompt. Bounded on three axes so it cannot oscillate: three rework attempts per unit, five rounds per run, and an immediate blocking stop if two consecutive builds report an identical set of errors — because a model that cannot fix an error will regenerate the same file indefinitely.
- **Business rules are verified, not asserted.** The comprehension pass proposes rules, each quoting the source it claims to be reading. Every quote is checked against the real file; the model's line number is discarded and replaced with where the quote actually is; anything unverifiable is stored as `rejected` with a reason and **never reaches a prompt**. The rejects are kept and readable on `/runs/{id}/rules?status=rejected` — a verifier whose failures are invisible is indistinguishable from one that passes everything.
- **Metered per converted file**, so cost is real per file, budget accrues as it goes, and `budget_cap_usd` pauses the run rather than discovering an overspend at the end.

## Cost control

- **Entitlement** — the caller's plan or key scopes must include `modernization`, else 403. Checked at run creation, before an upload is accepted.
- **Budget** — the project's and key's monthly caps apply per file, so a run stops at the cap rather than blowing through it. `budget_cap_usd` on the run is a second, run-local stop.
- **Prompt caching** — the charter and instructions are the *system* block and are byte-identical across every unit in a run, so a 400-file run is one cache write and 399 reads of the most repeated part of the prompt. Dependency context is public signatures only, never bodies, which is what keeps the per-call prompt a constant size instead of one that grows with the codebase.

## Deliberately not here yet

- **Compiling *here*.** Running a build on generated code derived from customer source is arbitrary code execution on the API host. The build happens in your CI or on your machine and reports back — see the build loop above. What does not exist yet is an opt-in sandbox worker that would do it for you.
- **Running the legacy application.** No screenshots, no crawl of the old UI, no parity test that replays a recorded legacy response against the converted one. This is the strongest accept criterion in the field and it needs a container runtime, so it belongs with the sandbox worker.
- **Tracing a rule forward.** A confirmed business rule is cited back to its source line, but nothing links it to the converted file that implements it — so "was this rule preserved?" is still a manual read.
- **Parallel conversion.** `topo_level` already proves which components are independent, so widening it later needs no new analysis — but correctness comes before throughput here, and SQLite write contention is the most likely thing to bite first.
- **Language auto-detection beyond the file extension.**

## Example

```bash
# 1. Ingest (free — no model call)
curl -X POST https://your-host/api/v1/modernization/runs/upload \
  -H "X-API-Key: lmsdlc_xxx" \
  -F file=@legacy.zip -F name="Billing → .NET 8" \
  -F target_stack=".NET 8 / ASP.NET Core" -F budget_cap_usd=50

# 2. Charter (refused if incomplete)
curl -X POST https://your-host/api/v1/modernization/runs/$RUN/charter \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{"charter":{"target_stack":".NET 8 / ASP.NET Core",
        "project_layout":"one project per layer",
        "data_access":"EF Core 8, repository per aggregate",
        "error_handling":"global exception middleware",
        "config_strategy":"IOptions from appsettings; secrets from environment only"}}'

# 3. GATE 1 — approve the assessment (small, bounded, quoted)
curl https://your-host/api/v1/modernization/runs/$RUN/checkpoints -H "X-API-Key: lmsdlc_xxx"
curl -X POST https://your-host/api/v1/modernization/checkpoints/$CP/answer \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{"decision":"approved"}'
curl -X POST https://your-host/api/v1/modernization/runs/$RUN/start -H "X-API-Key: lmsdlc_xxx"

# 4. GATE 2 — read the costed plan, then approve and resume
curl https://your-host/api/v1/modernization/runs/$RUN/plan -H "X-API-Key: lmsdlc_xxx"
curl -X POST https://your-host/api/v1/modernization/checkpoints/$CP2/answer \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{"decision":"approved"}'
curl -X POST https://your-host/api/v1/modernization/runs/$RUN/resume -H "X-API-Key: lmsdlc_xxx"

# 5. Watch, check consistency, then download
curl https://your-host/api/v1/modernization/runs/$RUN -H "X-API-Key: lmsdlc_xxx"
curl https://your-host/api/v1/modernization/runs/$RUN/reconciliation -H "X-API-Key: lmsdlc_xxx"
curl -OJ https://your-host/api/v1/modernization/runs/$RUN/export -H "X-API-Key: lmsdlc_xxx"
```

## Verification

`python scripts/smoke_modernization.py` — offline, free, deterministic. 456 checks covering path safety, redaction, the analyzers, graph ordering and cycle detection, charter validation, Tier-0 validation, claim-and-lease, a full run end to end (asserting every unit converted after everything it depends on, that both gates carry real numbers, that every artifact landed under its component's directory, and that the scaffold cost zero model calls), a simulated crash and resume (asserting nothing already converted is re-converted), failure isolation, the budget cap, the HTTP surface, the PHP language pack — whose assertions run **twice**, once with tree-sitter forced off, so the fail-open path is exercised rather than merely intended — the charter suggestions (zero model calls, byte-identical on a second pass, no redacted value in any option or citation, and an unrecognised language pair still answering every required field with nothing .NET-specific in it), the build loop (real MSBuild output parsed in both slash directions and de-duplicated across referencing projects; a fingerprint that survives line-number churn but not a changed error; all five ingest outcomes over HTTP; a reopened component's members ceasing to count as converted files; and a unit at the rework limit being named rather than looped on), and comprehension (a rule with a genuine quote confirmed *and its line corrected*, an invented one rejected with a reason and kept out of the prompt, and — the check worth the most — that a re-conversion prompt actually differs from the first one, without which the entire fix loop is a no-op that trips its own stuck guard on round two).

What none of it does is compile anything or call a real model. The build loop is verified in every part and has never been run end to end against a real compiler; see C-42 in `DECISIONS.md`.

`node scripts/smoke_render.js` — the browser rendering, against a DOM stub with no dependencies. Includes the charter form: that it arrives pre-filled from the recommended option, that its evidence is on screen rather than only in a tooltip, and that a failed suggestions fetch still leaves a form the user can fill in by hand.
