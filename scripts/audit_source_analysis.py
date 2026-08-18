"""What the deterministic layer concludes about a codebase, before anything is spent.

    python scripts/audit_source_analysis.py path/to/repo.zip
    python scripts/audit_source_analysis.py path/to/repo.zip --stack ".NET 8 / ASP.NET Core MVC"
    python scripts/audit_source_analysis.py path/to/repo.zip --quiet     # counts only

Offline: no server, no API key, no network, no model call. Everything printed here is a fact
the pipeline computes for free before the discovery gate — which is exactly why getting it
wrong is expensive. These facts decide what the converter is told, which files are allowed to
convert at all, and what a customer approves when they approve a spend.

Why this script exists
----------------------
Every PHP fixture in `scripts/smoke_modernization.py` was procedural: `mysqli_*` at file
scope, one page per URL, includes for dependencies. The analyzers were measured against that
shape and no other. The first layered PSR-4 application to arrive reported `db_apis: []` for a
codebase that is nothing but database access, one route for a codebase with fifteen, and thirty
third-party dependencies that do not exist — and the run said nothing was wrong, because
**nothing compared any of these numbers against anything else.**

So run this against a new codebase *before* running a conversion on it. It takes a second and
it costs nothing. The `SUSPICIOUS` lines at the end are not this script's own opinion any more:
they come from `backend/services/modernization/sanity.py`, which runs inside the product on the
path to the discovery gate of every run. What prints here is what the gate will say. This
script's remaining value is that it says it *before* a run exists, and shows the working —
which playbook sections get sent, which files will refuse to convert — that the gate summarises.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.modernization import (  # noqa: E402
    analyzers, evidence, graph, playbooks, repo_ingest, sanity, validation,
)
from backend.services.modernization import advice  # noqa: E402


def load(path):
    """Zip or directory -> {relative path: text}. Common root stripped, as ingest does."""
    if os.path.isdir(path):
        members = []
        for root, _dirs, files in os.walk(path):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, path).replace("\\", "/")
                try:
                    members.append({"path": rel,
                                    "content": open(full, encoding="utf-8",
                                                    errors="replace").read()})
                except OSError:
                    continue
    else:
        with open(path, "rb") as handle:
            members = repo_ingest.from_zip(handle.read())
        members = repo_ingest.strip_common_root(members)
    # The same filter and redaction the real ingest applies. Without it this script reads
    # `README.md` and `.gitignore` as source, and then reports them as entry points because
    # nothing in the codebase depends on them — an artefact of the script, not of the product.
    normalized = repo_ingest.normalize_files(members)
    return ({m["path"]: m.get("content") or "" for m in normalized["files"]},
            normalized["skipped"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source", help="Zip file or directory of the source repository.")
    parser.add_argument("--stack", default=".NET 8 / ASP.NET Core MVC with Razor views",
                        help="Target stack, which selects the playbook and the advice pack.")
    parser.add_argument("--quiet", action="store_true", help="Counts and warnings only.")
    args = parser.parse_args()

    files, skipped = load(args.source)
    if not files:
        print("Nothing readable in that source.")
        return 1

    facts = {}
    for path, text in sorted(files.items()):
        try:
            result = analyzers.analyze_file(path, text)
        except Exception as exc:                       # a bad file must not end the audit
            print(f"  ! analyzer raised on {path}: {type(exc).__name__}: {exc}")
            continue
        if result:
            facts[path] = result

    rows = []
    for path, text in sorted(files.items()):
        try:
            rows.extend(evidence.from_file(path, text))
        except Exception as exc:
            print(f"  ! evidence raised on {path}: {type(exc).__name__}: {exc}")

    built = graph.build(list(facts.values())) if facts else {"units": [], "stats": {}}
    rows.extend(evidence.from_graph(built["units"]))
    stats = built.get("stats") or {}
    units = [{"path": p, "language": f.language, "meta": f.meta} for p, f in facts.items()]
    aggregate = playbooks.aggregate_facts(units)

    def values(kind):
        return sorted({r["value"] for r in rows if r["kind"] == kind})

    languages = Counter(f.language for f in facts.values())
    kinds = Counter(r["kind"] for r in rows)

    print(f"\n{'=' * 74}\n{args.source}\n{'=' * 74}")
    print(f"  files ingested          {len(files)}  ({len(skipped)} skipped by extension)")
    print(f"  files analysed          {len(facts)}   {dict(languages)}")
    print(f"  dependency edges        {stats.get('edges', 0)}")
    print(f"  levels / cycles         {stats.get('levels', 0)} / {stats.get('cycles', 0)}")
    print(f"  external dependencies   {len(stats.get('external_dependencies') or [])}")

    print("\n  -- evidence ------------------------------------------------------")
    for kind in sorted(kinds):
        print(f"  {kind:<16} {kinds[kind]:>4}")

    print("\n  -- what the charter will be built from ---------------------------")
    print(f"  data APIs        {aggregate.get('db_apis')}")
    print(f"  engine(s)        {aggregate.get('db_engines') or values('datastore')}")
    print(f"  tables           {aggregate.get('tables') or values('table')}")
    print(f"  hazards          {aggregate.get('hazard_kinds')}")
    print(f"  entry points     {values('entrypoint')}")

    # The dominant *real* language, not whichever key landed first: a Counter over a mixed
    # repository has an empty-string entry for every file no analyzer claimed.
    dominant = next((lang for lang, _n in languages.most_common() if lang), "")
    book = playbooks.find(dominant, args.stack)
    selected = playbooks.select(book, aggregate) if book else []
    omitted = [s.key for s in (book.sections if book else []) if s.key not in selected]
    print(f"\n  playbook sections sent     {selected}")
    print(f"  playbook sections omitted  {omitted}")

    blocked = sorted(
        path for path, f in facts.items()
        if set(((f.meta or {}).get("php") or {}).get("hazard_kinds") or [])
        & validation.BLOCKING_HAZARDS)
    print(f"\n  files that will refuse to convert  {len(blocked)}")
    for path in blocked:
        kinds_here = sorted(set(((facts[path].meta or {}).get("php") or {})
                                .get("hazard_kinds") or []) & validation.BLOCKING_HAZARDS)
        print(f"    {path}  ({', '.join(kinds_here)})")

    if not args.quiet:
        print("\n  -- routes --------------------------------------------------------")
        for value in values("route") or ["(none found)"]:
            print(f"    {value}")

    observed = advice.observe(
        [{"path": p, "language": f.language, "meta": f.meta, "module_type": f.module_type}
         for p, f in facts.items()],
        rows, [])
    described = advice.describe(args.stack, observed)
    for field in described:
        if field["key"] == "data_access" and field.get("options"):
            print("\n  -- the data-access sentence the charter will offer ---------------")
            print(f"    {field['options'][0]['value'][:200]}")

    # ── Cross-signal checks ───────────────────────────────────────────────
    # These used to be a prototype block written here, in this script. They now live in
    # `backend/services/modernization/sanity.py` and run inside the product, on the path to
    # the discovery gate of every run — so this script *calls* them rather than keeping a
    # second copy. Two implementations of "what looks wrong about this analysis" that were
    # meant to agree and drifted would be a contradiction of exactly the kind they exist to
    # find. What is printed here is byte-for-byte what a run will show.
    signals = sanity.collect(
        [{"path": p, "content": t} for p, t in sorted(files.items())],
        built["units"], rows, stats, skipped=skipped)

    print("\n  -- what was read, and how well -----------------------------------")
    for line in sanity.render_counts(signals).splitlines():
        print(f"  {line}")

    print("\n  -- cross-signal checks ------------------------------------------")
    contradictions = sanity.evaluate(signals, args.stack)
    if not contradictions:
        print("    nothing contradicts anything else")
    for warning in contradictions:
        print(f"    SUSPICIOUS  {warning['title']}")
        print(f"                {warning['left']}")
        print(f"                vs {warning['right']}")
        if not args.quiet:
            print(f"                {warning['detail']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
