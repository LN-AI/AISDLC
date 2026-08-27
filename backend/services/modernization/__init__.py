"""Legacy modernization — a whole-codebase conversion run with durable state.

This is deliberately a separate product from `migration-assessment` / `migration-execute`
(see DECISIONS.md D-01). Those two are a good single-file translator and a good planning
document; neither has run identity, a dependency graph, resumability, or an artifact store,
and adding those to them would mean one product id with two incompatible behaviours.

The shape here is different in one specific way, and everything else follows from it:

    the durable unit of work is a row in a table, not a Python closure.

`job_service` holds work in a closure, so nothing about an in-flight job survives the
process that started it. A 500-file conversion cannot be built on that — a restart at file
390 must not mean starting over, and must not mean re-converting the 389 files already
done. So a run's state lives entirely in `modernization_units`: a worker claims one row
under a lease, converts it, and commits the result in one transaction. That transaction is
the checkpoint. Worst-case crash loss is one file's tokens.

Modules
-------
- `store`      — the schema and every query. Claim-and-lease lives here.
- `repo_ingest`— zip/file/GitHub ingestion, path safety, and secret redaction *before* any
                 model call.
- `analyzers`  — deterministic per-language dependency extraction. Never the LLM (D-04).
- `graph`      — Kahn ordering, SCC (cycle) detection.
- `validation` — Tier-0, in-process, zero execution (D-05).
- `runner`     — the worker loop that ties them together.

Two modules break the "everything happens in one process" shape, and break it the same way.
`build_results` and `parity` both answer a question this host is forbidden to answer itself —
does the converted code compile, and does it still behave the same — so both are built as
*contracts* rather than as runners: the platform stores the inputs and adjudicates the
outputs, and the execution happens on the customer's CI, a developer's laptop or an opt-in
sandbox worker, which POST their results back. Neither module contains anything that starts a
process, and neither should.
"""
