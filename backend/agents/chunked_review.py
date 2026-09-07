"""One implementation of "review a large artifact and return a result you can trust".

This is the eleven-stage pipeline that lived inside `CodeReviewAgent.review_code` and
nowhere else. `code_chunker`, `finding_pipeline` and `finding_gate` had exactly one caller
each, so VAPT — the agent whose findings carry CVSS scores and block releases — had none of
it (C-73): no splitting, no evidence verification, no de-duplication, no fingerprints, no
waivers, and a gate that was whatever the model happened to say.

Lifting it here rather than copying it into VAPT is the whole point. Two copies of this
would drift the first time either was fixed, and the copy guarding the security agent would
be the stale one.

What varies per agent is passed in, and it is a short list: the tool schema and output
budget (both hidden inside `call_chunk`), the prompt, the `review_type`, and any extra
top-level fields that agent's schema carries. Everything else — the fan-out, the
cache-warming first call, the halve-and-retry on truncation, the failure isolation, the
gate arithmetic and the coverage clamp — is identical, because the reasons for it are.

Order of the assembled result is load-bearing and matches what Code Review did before:

  chunk -> fan out -> merge raw -> refuse a degenerate response -> verify evidence
  -> suppress noisy paths -> merge duplicates -> fingerprint -> compare to baseline
  -> record coverage -> reconcile the gate

`apply_baseline` has to follow `process` because fingerprints only exist after merging, and
the gate has to be last because the coverage clamp can add a finding of its own.
"""
import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from backend.services import code_chunker, finding_gate, finding_pipeline
from backend.services.llm_gateway import LLMTruncatedResponseError

logger = logging.getLogger(__name__)

# Concurrency for the fan-out. Kept modest on purpose: the point is to finish a large
# artifact in reasonable wall-clock time, not to burn a per-key rate limit and turn every
# chunk into a retry.
MAX_CHUNK_WORKERS = 4

# How many times a chunk may be halved after a truncated response. Truncation means "more
# findings than the output budget", so halving is the right answer — but a chunk that
# truncates after two halvings is pathological and should surface as an error.
MAX_TRUNCATION_SPLITS = 2

# Flat string lists every chunk tends to repeat; de-duplicated rather than concatenated.
_STRING_LISTS = ("recommendations", "compliance_flags", "guardrail_flags")


def _merge_raw(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold N per-chunk responses into one, taking the stricter gate and the higher risk."""
    findings: List[Dict[str, Any]] = []
    strings: Dict[str, List[str]] = {k: [] for k in _STRING_LISTS}
    summaries: List[str] = []
    gate, risk = "Green", 0.0
    extra: Dict[str, List[str]] = {}

    for res in results:
        findings.extend(res.get("findings") or [])
        for key in _STRING_LISTS:
            strings[key].extend(res.get(key) or [])
        if res.get("summary"):
            summaries.append(res["summary"])
        gate = finding_gate.stricter(gate, res.get("gate_status") or "Green")
        try:
            risk = max(risk, float(res.get("risk_score") or 0.0))
        except (TypeError, ValueError):
            pass
        # Any prose field an agent's own schema adds (VAPT's attack_surface_summary,
        # for instance) is collected per chunk and joined, rather than the last chunk
        # silently winning.
        for key, value in res.items():
            if key in ("findings", "summary", "gate_status", "risk_score", "agent_name") \
                    or key in _STRING_LISTS:
                continue
            if isinstance(value, str) and value.strip():
                extra.setdefault(key, []).append(value.strip())

    merged: Dict[str, Any] = {
        "findings": findings,
        "gate_status": gate,
        "risk_score": risk,
        "summary": " ".join(summaries),
    }
    for key in _STRING_LISTS:
        merged[key] = list(dict.fromkeys(strings[key]))
    for key, values in extra.items():
        merged[key] = " ".join(dict.fromkeys(values))
    return merged


def call_with_halving(
    call_chunk: Callable[[str], Dict[str, Any]],
    body: str,
    depth: int = 0,
) -> Dict[str, Any]:
    """One call over one chunk, halving and retrying if the response was cut off.

    A truncated tool response means the findings did not fit the output budget, not that the
    call was transiently unlucky — so the useful retry is a smaller input, not the same one
    again. Raising `max_tokens` only moves the ceiling; this is what handles the chunk that
    still does not fit under it.
    """
    try:
        return call_chunk(body)
    except LLMTruncatedResponseError:
        if depth >= MAX_TRUNCATION_SPLITS:
            raise
        lines = body.splitlines()
        if len(lines) < 4:
            raise
        mid = len(lines) // 2
        logger.warning(
            "Chunk truncated at depth %d — splitting %d lines in half and retrying",
            depth, len(lines),
        )
        halves = ["\n".join(lines[:mid]), "\n".join(lines[mid:])]
        return _merge_raw([
            call_with_halving(call_chunk, half, depth + 1) for half in halves
        ])


def _fan_out(
    chunks: List[Any],
    call_chunk: Callable[[str], Dict[str, Any]],
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> tuple:
    """Run every chunk, isolating failures so one bad chunk does not lose the rest.

    Chunk 1 runs alone first. An Anthropic prompt-cache entry only becomes readable once the
    first response using that prefix has started, so firing all N at once means all N pay
    the full uncached input price; sequencing the first turns the remaining N-1 into cache
    reads.

    `contextvars.copy_context()` is load-bearing, not decoration: the BYOL override and the
    usage accumulator are both contextvars, and without copying them into the worker every
    chunk would bill to nobody. Same reason `swarm_service` does it.
    """
    done = [0]

    def guarded(chunk):
        try:
            result = call_with_halving(call_chunk, chunk.body)
            return chunk, result, None
        except Exception as exc:
            logger.exception("Review chunk %d/%d failed", chunk.index, chunk.total)
            return chunk, None, exc
        finally:
            done[0] += 1
            if on_progress:
                try:
                    on_progress(done[0], len(chunks))
                except Exception:
                    logger.exception("Progress callback failed — continuing the review")

    collected = [guarded(chunks[0])]
    if len(chunks) > 1:
        # .copy() per task, not one shared Context: a Context object cannot be entered by
        # two threads at once, and reusing one raises "is already entered". The copy has to
        # be taken here, on the calling thread — taking it inside the worker would snapshot
        # the worker's own empty context and record no usage at all.
        parent_context = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=MAX_CHUNK_WORKERS) as pool:
            futures = [
                pool.submit(parent_context.copy().run, guarded, c) for c in chunks[1:]
            ]
            collected += [f.result() for f in futures]

    results, failures = [], []
    for chunk, res, err in collected:
        if err is not None:
            failures.append({
                "chunk": chunk.index,
                "files": chunk.paths,
                "error": f"{type(err).__name__}: {err}",
            })
            continue
        results.append(res)
    return results, failures


def run(
    *,
    agent_name: str,
    review_type: str,
    call_chunk: Callable[[str], Dict[str, Any]],
    artifact: Optional[str] = None,
    files: Optional[List[Dict[str, Any]]] = None,
    is_diff: Optional[bool] = None,
    coverage_incomplete: bool = False,
    coverage_reason: str = "",
    extra_findings: Optional[List[Dict[str, Any]]] = None,
    baseline_lookup: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
    resolved_lookup: Optional[Callable[[], Dict[str, Any]]] = None,
    suppression: Any = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """Review one artifact — a string or a file list — and return a gated result.

    Exactly one of `artifact` / `files` is expected. `files` is the repository and zip path
    and gets per-file verification, which is a stronger check than the whole-haystack one:
    the evidence must appear in *the file the finding names*, not merely somewhere in the
    input. See `finding_pipeline.verify_evidence_per_file`.

    `extra_findings` are findings produced deterministically, before any model ran — today
    that means `source_bundle.secret_findings`, the hardcoded credentials the ingest scanner
    matched. They are merged **before** the pipeline rather than appended after, so they get
    everything the model's findings get: verification against the file they name,
    de-duplication against the model's version of the same finding, path suppression when
    they are in a fixture, a fingerprint so they can be waived, and a vote in the gate.
    Appending them afterwards would have made them second-class in all five ways.

    `suppression` is what the repository itself says not to gate on — its `.lmignore` and
    its inline `lm-ignore` comments, parsed by `backend/services/suppression.py` and
    carried on the Bundle. It is applied at the same stage as the built-in test/vendor path
    rules, which means a suppressed finding is still reported and still exported; it just
    stops voting in the gate. Passed in rather than looked up here for the same reason
    `baseline_lookup` is: this module does not know what a source is.
    """
    multi_file = bool(files) and not (len(files) == 1 and not files[0].get("path"))
    if multi_file:
        chunks = code_chunker.chunk_files(files)
        haystack = "\n\n".join(f.get("content") or "" for f in files)
        diff_mode = False
    else:
        text = artifact if artifact is not None else ((files or [{}])[0].get("content") or "")
        chunks = code_chunker.chunk_artifact(text)
        haystack = text
        diff_mode = code_chunker.is_unified_diff(text) if is_diff is None else is_diff

    if not chunks:
        raise RuntimeError(f"{agent_name} was given an empty artifact — nothing to review.")

    if len(chunks) > 1:
        logger.info("%s fanning out over %d chunks", agent_name, len(chunks))
    results, failures = _fan_out(chunks, call_chunk, on_progress)

    # Every chunk failing is indistinguishable from the agent not running at all — that must
    # be an error, not a clean report over nothing.
    if failures and len(failures) == len(chunks):
        raise RuntimeError(
            f"{agent_name} failed on all {len(chunks)} chunk(s) — no part of the artifact "
            f"was reviewed. First error: {failures[0]['error']}"
        )

    result = _merge_raw(results)
    result["agent_name"] = agent_name
    result["review_type"] = review_type

    # Judged before the deterministic findings are folded in. Otherwise a repository that
    # happens to contain one hardcoded password would supply a finding on the agent's behalf
    # and mask a completely failed model response as a real result.

    # Fail loud rather than persisting a truncated response as a clean Green. Judged on the
    # artifact, not the assembled prompt, so the size threshold reflects the real input.
    degenerate = finding_gate.looks_degenerate(result, haystack)
    if degenerate:
        raise RuntimeError(
            f"{agent_name} returned an empty result — {degenerate}. "
            "Try again with a smaller artifact."
        )

    if extra_findings:
        result["findings"] = list(extra_findings) + list(result["findings"])
        result["deterministic_findings_count"] = len(extra_findings)

    # Zeroed first, so the report below names the rules that fired on *this* run rather
    # than accumulating across a retry of the same Bundle.
    if suppression is not None:
        suppression.reset()

    if multi_file:
        processed = finding_pipeline.process_files(
            result["findings"], files, correct_lines=True, suppression=suppression)
    else:
        # Line correction is off for diffs — see verify_evidence's docstring.
        processed = finding_pipeline.process(
            result["findings"], haystack, correct_lines=not diff_mode,
            suppression=suppression,
        )

    # Baseline comparison has to follow `process`, because fingerprints only exist after
    # merging. Waived findings drop out here; everything else is labelled new or pre-existing
    # so a repeat run reads "3 new, 41 pre-existing" instead of "44 findings" for the tenth
    # time.
    baseline = baseline_lookup(processed["findings"]) if baseline_lookup else {}
    processed.update(finding_pipeline.apply_baseline(processed["findings"], baseline))
    result.update(processed)

    # And the other direction: what the last review of this same code reported that this
    # one does not. Computed here rather than in the pipeline because this is the only
    # place that knows which files were actually read -- a file in a failed chunk was never
    # opened, so nothing in it may be credited as fixed.
    if resolved_lookup:
        files_read = None
        if multi_file:
            files_read = {f.get("path") for f in files if f.get("path")}
            for failure in failures:
                for path in (failure.get("files") or []):
                    files_read.discard(path)
        try:
            closed = finding_pipeline.resolved_since(
                result["findings"], resolved_lookup(), files_read)
            result.update(closed)
        except Exception:
            logger.exception("Resolved-since comparison failed — reporting none as fixed")

    # What the repository asked not to be gated on, and — the part that matters — which of
    # its rules matched nothing. A misspelled pattern is otherwise invisible: the developer
    # believes they suppressed something, the gate still fails, and the tool takes the
    # blame.
    if suppression is not None:
        result["suppression"] = suppression.report()

    result["coverage"] = {
        "chunks_total": len(chunks),
        "chunks_succeeded": len(chunks) - len(failures),
        "chunks_failed": len(failures),
        "failures": failures,
        "files_reviewed": len(files) if multi_file else 1,
        # Which files each call actually saw. Computed here because this is the only place
        # that knows the real split -- and it answers the question a reader asks when a
        # finding is missing: "was that file even in front of it, and with what?" A file in a
        # failed chunk was never read at all, which the counts above cannot tell you.
        "chunk_files": [
            {"index": c.index, "files": c.paths, "chars": len(c.body),
             "failed": any(f.get("chunk") == c.index for f in failures)}
            for c in chunks
        ] if multi_file else [],
    }

    incomplete = coverage_incomplete or bool(failures)
    reason = coverage_reason
    if failures:
        failed_note = (
            f"{len(failures)} of {len(chunks)} chunk(s) of this artifact could not be "
            f"reviewed, so part of the code was never examined."
        )
        reason = f"{reason} {failed_note}".strip() if reason else failed_note

    finding_gate.reconcile_gate(result, coverage_incomplete=incomplete, coverage_reason=reason)
    return result


def default_baseline(findings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """What this project already decided about these findings.

    Imported lazily: the agent layer does not otherwise depend on the data layer, and a
    module-level import would make that dependency permanent for every agent that imports
    this one. A lookup failure degrades to "no baseline" — every finding then reads as new,
    which is the pre-baseline behaviour and never hides anything.
    """
    fingerprints = [f.get("fingerprint") for f in findings if f.get("fingerprint")]
    if not fingerprints:
        return {}
    try:
        from backend import context, database
        # A risk accepted for 90 days has to be back in front of somebody by the next
        # review, and there is no scheduler here to do it on a timer. This is the moment it
        # matters: the lapse happens before the baseline is read, so a run cannot suppress a
        # finding whose acceptance ran out yesterday.
        database.lapse_expired_waivers()
        return database.get_finding_baseline(context.current_project_id(), fingerprints)
    except Exception:
        logger.exception("Finding baseline lookup failed — treating every finding as new")
        return {}


def default_resolved() -> Dict[str, Dict[str, Any]]:
    """What the last review of this same repository left open.

    Lazily imported and failure-tolerant for the same reasons as `default_baseline`: a
    lookup that cannot run degrades to "nothing was fixed", which understates progress and
    never invents it. Reads the repository from the request context — the same binding
    `save_review` uses to store it, so the comparison is against the same repository the
    row will be filed under.
    """
    try:
        from backend import context, database
        ref = context.current_source_ref()
        return database.previously_open_findings(
            context.current_project_id(),
            repo_owner=ref.get("repo_owner") or "",
            repo_name=ref.get("repo_name") or "",
            product_id=context.current_product_id() or "",
        )
    except Exception:
        logger.exception("Previously-open lookup failed — reporting nothing as fixed")
        return {}
