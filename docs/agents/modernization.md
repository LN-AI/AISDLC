# Legacy Modernization

Converts a **whole legacy codebase** to a target stack as a durable, resumable run: ingests a repository (redacting secrets before any model call), derives a dependency graph with real parsers, converts file by file in topological order against a charter your team approves once, and keeps a run-scoped symbol registry so file 400 stays consistent with file 4.

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
create ──▶ awaiting_charter ──▶ awaiting_approval ──▶ converting ⇄ paused ──▶ completed
             (ingest done,        (charter set)         (approved)    (cap reached,
              nothing spent)                                           checkpoint, restart,
                                                                       or your request)
```

Ingest, redaction and dependency analysis are synchronous and **cost nothing** — no model is called until you set a charter and approve the plan. Two gates exist before any spend, both deliberately:

- **The charter** is refused if incomplete. Leaving those decisions to the model is what produces a codebase that disagrees with itself.
- **Plan approval** is blocking. "523 files, these 4 circular dependencies, $0 spent so far" is information you should have *before* the spend.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/runs` | Create + ingest from `files[]` or `github_repo_url` |
| `POST` | `/runs/upload` | Create + ingest from a zip (multipart) |
| `GET` | `/runs` | List runs visible to the caller |
| `GET` | `/runs/{id}` | Manifest — status, progress, counts, open checkpoints |
| `GET` | `/charter-fields` | The charter's schema, so a UI form can't drift from what is enforced |
| `POST` | `/runs/{id}/charter` | Set the binding charter (400 if incomplete) |
| `POST` | `/runs/{id}/start` | 202 + `job_id`; begins converting |
| `POST` | `/runs/{id}/pause` | Stops after the file currently in flight |
| `POST` | `/runs/{id}/resume` | Continues from the last converted file |
| `POST` | `/runs/{id}/cancel` | Terminal; converted output is kept |
| `GET` | `/runs/{id}/units` | The inventory, in conversion order |
| `GET` | `/runs/{id}/units/{unit_id}` | One unit with its converted output |
| `GET` | `/runs/{id}/symbols` | The run's symbol registry |
| `GET` | `/runs/{id}/redactions` | Where secrets were removed — path, line, category |
| `GET` | `/runs/{id}/checkpoints` | Decisions the run is waiting on |
| `POST` | `/checkpoints/{id}/answer` | Answer one |
| `GET` | `/runs/{id}/events` | Append-only run log |
| `GET` | `/runs/{id}/export` | Download the converted codebase as a zip |

## Guarantees, and what they cost

- **Secrets are redacted before any model call.** Credential *files* (`.env`, `*.pem`, `secrets.yaml`, anything under `secrets/`) are skipped entirely; credential *values* inside ordinary source are replaced in place, keeping the assignment so the converted code still has a config key where the literal was. The redaction map records **path, line and category — never the value.** Every entry becomes a configuration key somebody must supply, and they are listed in the export report.
- **Paths are rejected, never repaired.** An archive member that is absolute, has a drive letter, or contains any `..` segment is dropped — including one that would normalize back inside the root. Output paths the model proposes get the same treatment.
- **The dependency graph comes from parsers, not the model.** Python uses stdlib `ast`; Java/Kotlin/Scala, C#/VB.NET and Delphi/Pascal use regex over `package`/`import`/`using`/`uses`. An unknown language produces no edges and converts as a root — the safe wrong answer. A model-produced graph would be silently wrong in a way that corrupts conversion order, and therefore every file after the mistake.
- **Cycles are broken deterministically and never silently.** Strongly-connected components are detected; members share an `scc_group`, the intra-cycle edges are removed so the group can convert at all, each member is told which of its siblings does not exist yet, and a checkpoint records it. Cycle checkpoints are non-blocking — legacy code is full of them, and blocking on each would stall every real codebase at file zero — but they appear on the plan you approve.
- **One transaction per converted file is the checkpoint.** Artifacts, symbols, the unit's status and its dependents' readiness all commit together. A crash costs at most one file's tokens, and that boundary coincides exactly with the LLM call boundary. Restart recovery returns in-flight units to the queue and pauses the run; resume continues from the next unconverted file and re-converts nothing.
- **Validation is Tier 0 — thorough, and it executes nothing.** Placeholder detection (`// ... rest unchanged` is a failure, not a warning), bracket/string structural checks, exact `ast.parse` for Python output, unresolved-symbol checks against the run's registry, and a secret re-scan on every emitted file. **The output is never compiled and no tests are run.** Build it in your own CI before treating it as done — the export zip says so too.
- **Metered per converted file**, so cost is real per file, budget accrues as it goes, and `budget_cap_usd` pauses the run rather than discovering an overspend at the end.

## Cost control

- **Entitlement** — the caller's plan or key scopes must include `modernization`, else 403. Checked at run creation, before an upload is accepted.
- **Budget** — the project's and key's monthly caps apply per file, so a run stops at the cap rather than blowing through it. `budget_cap_usd` on the run is a second, run-local stop.
- **Prompt caching** — the charter and instructions are the *system* block and are byte-identical across every unit in a run, so a 400-file run is one cache write and 399 reads of the most repeated part of the prompt. Dependency context is public signatures only, never bodies, which is what keeps the per-call prompt a constant size instead of one that grows with the codebase.

## Deliberately not in v1

- **Compiling.** Running a build on generated code derived from customer source is arbitrary code execution on the API host, and a real build needs the real SDK anyway. The correct home for it is your CI, reached by pushing a branch.
- **Writing to your git.** Export is a zip.
- **Parallel conversion.** `topo_level` already proves which files are independent, so widening it later needs no new analysis — but correctness comes before throughput here, and SQLite write contention is the most likely thing to bite first.
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

# 3. Approve the plan, then start
curl -X POST https://your-host/api/v1/modernization/checkpoints/$CP/answer \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{"decision":"approved"}'
curl -X POST https://your-host/api/v1/modernization/runs/$RUN/start -H "X-API-Key: lmsdlc_xxx"

# 4. Watch, then download
curl https://your-host/api/v1/modernization/runs/$RUN -H "X-API-Key: lmsdlc_xxx"
curl -OJ https://your-host/api/v1/modernization/runs/$RUN/export -H "X-API-Key: lmsdlc_xxx"
```

## Verification

`python scripts/smoke_modernization.py` — offline, free, deterministic. 206 checks covering path safety, redaction, the analyzers, graph ordering and cycle detection, charter validation, Tier-0 validation, claim-and-lease, a full run end to end (asserting every file converted after everything it depends on), a simulated crash and resume (asserting nothing already converted is re-converted), failure isolation, the budget cap, and the HTTP surface.
