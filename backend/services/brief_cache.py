"""Deterministic briefs, computed once per codebase instead of once per request.

Why this exists
---------------
`code_brief.build` and `test_context_brief.build` are pure functions of a bundle's file
paths and contents: no model, no clock, no randomness, no I/O. They are also slow on a real
repository, because they run the language analyzers and the dependency graph over every
file. Measured on this repository's own backend — 191 files, 5.7 MB:

    test_context_brief.build     2.6 s
    code_brief.build             9.3 s
    for_automation (the pair)   12.5 s

And every code-reading product computes them **twice** for one generation: once for the free
preflight, which is how a user decides whether to spend, and again for the run itself, on
byte-identical input. So a user waited 12.5 seconds for a quote and another 12.5 before the
model call even started, for an answer that could not have changed.

That is the whole of it. This does not reduce what a run costs — the model never sees these
timings — it removes a wait that was never doing any work.

Why a content hash rather than a commit sha
-------------------------------------------
A sha is not available for half the intake. A paste has none, an uploaded zip has none, and
a `files[]` list has none; only a GitHub source carries one. Hashing what was actually read
covers every door with one rule, and it cannot go stale: different content is a different
key, so there is no invalidation to get wrong and no way to serve a brief for a repository
that has since changed.

It is also the reason this is safe to share across callers. The key is derived from the
content, so a brief cannot be retrieved without already holding the bytes that produced it —
possession of the key *is* possession of the input. Nothing is stored that its holder could
not compute.

What this is not
----------------
**Not durable.** It lives in this process and dies with it. A restart recomputes, which is
correct: the alternative is a disk cache holding customer source-derived data with its own
retention question, and `review_source_store` already exists for the case where source has
to outlive a request.

**Not a correctness layer.** Entries are treated as immutable — the top level is copied on
the way out, but a caller that mutates a nested list inside a cached brief corrupts it for
everyone after. Every current caller reads.
"""
import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable

logger = logging.getLogger(__name__)

# Six distinct codebases in flight at once is already generous for a single process, and the
# value held is a brief (a few hundred KB) rather than the source it describes. Small enough
# that the cache cannot become the reason a deployment runs out of memory, which is the only
# failure mode worth designing against here — a miss costs seconds, a leak costs the process.
MAX_ENTRIES = 6

_lock = threading.Lock()
_entries: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_stats = {"hits": 0, "misses": 0}


def key_for(files: Iterable[Dict[str, Any]], *parts: Any) -> str:
    """A stable digest of exactly what the briefs read: each file's path and content.

    Sorted, so two bundles that hold the same files in a different order share an entry —
    `repo_ingest` does not promise an order, and a brief that ignores order must not be
    keyed on it. `parts` carries anything else the computation depends on, which today is
    the size limit each builder takes: the same repository at a different `max_chars` is a
    different brief and must not collide with it.
    """
    digest = hashlib.sha256()
    rows = sorted(((f.get("path") or ""), (f.get("content") or "")) for f in (files or ()))
    for path, content in rows:
        digest.update(path.encode("utf-8", "replace"))
        digest.update(b"\x00")
        digest.update(content.encode("utf-8", "replace"))
        digest.update(b"\x00")
    for part in parts:
        digest.update(repr(part).encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()


def memoize(name: str, files: Iterable[Dict[str, Any]], parts: tuple,
            compute: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Return the cached brief for this content, or compute and store it.

    `compute` runs **outside** the lock. Holding it across a 9-second analyzer pass would
    serialize every unrelated review in the process behind one big repository — the cache
    would have removed a wait by adding a worse one. The cost of not holding it is that two
    threads racing on the same new codebase both compute; they then agree on the answer,
    because the function is pure, so the loser overwrites with an identical value.
    """
    cache_key = name + ":" + key_for(files, *parts)
    with _lock:
        hit = _entries.get(cache_key)
        if hit is not None:
            _entries.move_to_end(cache_key)
            _stats["hits"] += 1
            return dict(hit)
        _stats["misses"] += 1

    value = compute()
    if not isinstance(value, dict):
        return value

    with _lock:
        _entries[cache_key] = value
        _entries.move_to_end(cache_key)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)
    return dict(value)


def stats() -> Dict[str, int]:
    """Hits, misses and size. For a smoke test to assert the cache is actually being read —
    a cache that silently never hits looks exactly like one that works."""
    with _lock:
        return {"hits": _stats["hits"], "misses": _stats["misses"], "entries": len(_entries)}


def clear() -> None:
    """Drop everything. For tests, which must not inherit another test's entries."""
    with _lock:
        _entries.clear()
        _stats["hits"] = 0
        _stats["misses"] = 0
