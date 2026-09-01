"""Does the converted application start, and does it answer its own URLs?

Every other check in this package reads code. Tier-0 asks whether a file is whole,
reconciliation whether the files agree with each other, and the build loop whether a compiler
accepts them. A conversion can pass all three and be unusable, and this product has shipped
that twice:

* a controller returning `View(model)` whose emitted view was named differently — builds with
  zero errors, throws `The view 'Index' was not found` on the first request (C-60);
* a container missing a registration — builds with zero errors, 500s on *every* route (C-59).

Both were found by running the export by hand. Neither could have been found any other way,
which is why "it compiles" is not the acceptance bar.

Nothing here executes anything
------------------------------
Same shape as `build_results` and `parity`, for the same reason (D-05, D-62): **the platform
stores the inputs and adjudicates the outputs; something else executes.**
`POST .../runtime-results` takes what a runner observed, `GET .../startup-plan` tells it what
to request, and a developer's machine or the customer's CI is what starts the process. No
function below spawns anything.

Why this is not the build-results contract
------------------------------------------
Reusing it would be wrong twice, and both would be silent:

* a runtime 500 has no file and no line, so every row would be `unmapped` — and `ingest`
  raises a **blocking** `build_unmapped` for that, halting the run for entirely the wrong
  reason;
* it would consume a `build_round`, which is what the round cap and the non-convergence
  fingerprint are counted in. A startup check would look like a failed build round and the
  loop would start reporting `build_stuck` about something it never built.

What counts as a failure, and what deliberately does not
--------------------------------------------------------
A 5xx, a process that exits while being probed, or a port that never answers. Not a 302 — an
unauthenticated request to a guarded route *should* redirect, and a converted app that did not
would be the defect. Not a 4xx either: a route that answers 404 is a route that is missing, so
it is a warning worth reading and not proof the application is broken. A run where **every**
probed URL 404s is a different claim, and that one fails: nothing is serving.

`skipped` is a state, not a pass. A run whose database is MySQL and whose runner had no MySQL
could not have started the app, and recording that as success is the absence-looks-like-
evidence failure this subsystem keeps closing.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from backend.services.modernization import build_results, stages, store

logger = logging.getLogger(__name__)

# A corpus of URLs is not what this is. It probes an application's own declared routes, and a
# run with more than this many is a run whose analysis found a route table, not a defect.
MAX_CHECKS = 500
MAX_PATH_CHARS = 2_000
MAX_LOG_CHARS = 20_000

# How many failing URLs a checkpoint spells out. A display budget, not a storage limit.
CHECKPOINT_CHECKS = 20

STATUSES = ("passed", "failed", "skipped", "never_run")


class RuntimeReportError(ValueError):
    """The report could not be read. The route turns this into a 400."""


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _check(raw: Any) -> Dict[str, Any]:
    """One probed URL in contract shape. Coerced here and nowhere else."""
    if not isinstance(raw, dict):
        raise RuntimeReportError("every check must be an object with a path and a status.")
    return {
        "method": (_text(raw.get("method"), 10) or "GET").upper(),
        "path": _text(raw.get("path"), MAX_PATH_CHARS),
        "status": _int(raw.get("status")),
        "error": _text(raw.get("error"), 400),
        "ms": _int(raw.get("ms")),
    }


def verdict(checks: List[Dict[str, Any]], started: bool, skipped_reason: str = "") -> str:
    """The one word this run's runtime state reduces to.

    Kept separate from `ingest` and pure, because it is the part that is easy to get subtly
    generous — and a check that is generous about "did it run" is worse than no check.
    """
    if skipped_reason:
        return "skipped"
    if not started:
        return "failed"
    if not checks:
        # It started and nothing was asked of it. That is not a pass; it is a check that did
        # not run, and it has to read as one.
        return "skipped"
    if any(c["status"] >= 500 or (c["error"] and not c["status"]) for c in checks):
        return "failed"
    if all(c["status"] == 404 for c in checks):
        # Every URL the analysis found is missing. The host is up and the application is not
        # wired to it — which is exactly the shape a missing `MapControllers` or a wrong route
        # prefix produces, and it is a failure however healthy the process looks.
        return "failed"
    return "passed"


def ingest(run_id: str, payload: Dict[str, Any], actor: str = "") -> Dict[str, Any]:
    """Record one startup attempt and adjudicate it. The single entry point for any producer."""
    if not isinstance(payload, dict):
        raise RuntimeReportError("the report must be an object.")
    raw_checks = payload.get("checks")
    if raw_checks is None:
        raise RuntimeReportError(
            "a report must carry `checks` — an empty list is how a runner says it started the "
            "application and asked it nothing, and omitting the key is indistinguishable from "
            "a report that was never built.")
    if not isinstance(raw_checks, list):
        raise RuntimeReportError("`checks` must be a list.")
    if len(raw_checks) > MAX_CHECKS:
        raise RuntimeReportError(f"at most {MAX_CHECKS} checks per report.")

    checks = [_check(c) for c in raw_checks]
    started = bool(payload.get("started"))
    skipped_reason = _text(payload.get("skipped_reason"), 400)
    status = verdict(checks, started, skipped_reason)

    report_id = store.record_runtime(
        run_id, checks,
        status=status,
        started=started,
        skipped_reason=skipped_reason,
        base_url=_text(payload.get("base_url"), 300),
        command=_text(payload.get("command"), 300),
        source=_text(payload.get("source"), 40) or "external",
        log_tail=_text(payload.get("log_tail"), MAX_LOG_CHARS),
        actor=_text(actor, 120),
    )

    failing = [c for c in checks if c["status"] >= 500 or (c["error"] and not c["status"])]
    if status == "passed":
        # Withdrawn on the report rather than at the next `gate` call, for the reason
        # `build_results.ingest` documents: `_stop_reason` pauses at the top of every stage for
        # any open blocking checkpoint, and `gate` runs at the end of reconcile — so a run
        # holding an open `runtime_required` cannot reach the only code that would take it
        # down. php-crud-v2 was in that state after a clean build and a passed startup check.
        cleared = store.withdraw_checkpoints(
            run_id, ("runtime_required",),
            f"Withdrawn automatically: the application started and answered {len(checks)} "
            f"probe(s) with {len(failing)} failing, which is the evidence this gate was "
            f"asking for.")
        if cleared:
            store.append_event(
                run_id, "gate_withdrawn",
                f"{cleared} startup gate(s) withdrawn — this report answered them", level="info")
    if status == "failed":
        # Read the output and send what it names back through the loop, rather than storing a
        # log for a human to decode. Only a host that never started has output worth parsing;
        # a 500 on one URL is a running process, and its stack trace is in the response rather
        # than in this tail.
        repair = _repair(run_id, payload.get("log_tail") or "") if not started else {}
        detail = _describe(checks, started, payload.get("log_tail") or "") + (
            repair.get("note") or "")
        store.create_checkpoint(
            run_id, "runtime_failed",
            ("The converted application did not start"
             if not started else
             f"The application started and {len(failing) or len(checks)} URL(s) did not answer"),
            detail=detail[:4000],
            # Non-blocking once work has gone back into the loop: the run needs resuming, not
            # deciding, and a blocking gate there would stop the very repair it just queued.
            blocking=bool(repair.get("blocking", True)),
            payload={"report_id": report_id, "checks": checks[:CHECKPOINT_CHECKS],
                     "started": started, "reopened": repair.get("reopened", 0),
                     "diagnostics": diagnostics(payload.get("log_tail") or "")[:MAX_DIAGNOSTICS]},
        )
    logger.info("Modernization runtime check for run %s: %s (%s URL(s), started=%s)",
                run_id, status, len(checks), started)
    return {"report_id": report_id, "status": status, "checks": len(checks),
            "started": started, "failing": len(failing)}


# ── Reading a start-up failure ─────────────────────────────────────────────
#
# The log was already captured, stored and shown. Nothing read it, so a host that refused to
# start was a dead end for the loop: a human opened a checkpoint and decoded twenty lines of
# .NET. That is the same position build errors were in before `build_results`, and the answer
# is the same one — parse what is parseable, route it to the unit that can fix it, and hand
# what is left to the model with the log in front of it.
#
# **The parser does not have to be complete, and this is what makes that acceptable.** A
# pattern it recognises produces an exact, deterministic fix. A pattern it does not produces a
# type name harvested from the message, routed to whichever unit declared that type, and the
# converter is shown the raw output. Only a message naming nothing this run produced is
# genuinely unroutable, and that stays a checkpoint for a human — which is where every one of
# them was before.

# `Unable to resolve service for type 'X' while attempting to activate 'Y'.` The single most
# common way a converted ASP.NET Core application fails to start, and with `ValidateOnBuild`
# on it now names *every* one of them at once instead of the first request's worth.
_UNRESOLVED = re.compile(
    r"Unable to resolve service for type '(?P<missing>[\w.+<>,\[\] ]+)'"
    r"(?:\s*while attempting to activate '(?P<wanted_by>[\w.+<>,\[\] ]+)')?")

# A singleton holding a scoped service. Not a crash on request one — it is a stale object
# served to every later request, which is why `ValidateScopes` exists and why this is fatal.
_SCOPE = re.compile(
    r"Cannot consume (?P<lifetime>scoped|transient) service '(?P<missing>[\w.+<>,\[\] ]+)' "
    r"from singleton '(?P<wanted_by>[\w.+<>,\[\] ]+)'")

# A controller returning `View(model)` whose view was emitted under another name. Builds with
# zero errors; throws on the first request to that action (C-60).
_VIEW = re.compile(r"The view '(?P<view>[^']+)' was not found")

# Two actions claiming one URL. Also invisible to the compiler.
_AMBIGUOUS = re.compile(r"AmbiguousMatchException:?\s*(?P<detail>.*)")

# The last resort: any dotted type name in the message. A `.`-separated identifier is not
# proof of anything on its own, so the caller only keeps the ones this run actually declared.
_TYPE_TOKEN = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)+)\b")

_UNHANDLED = re.compile(r"Unhandled exception\.\s*(?P<type>[\w.`]+)\s*:\s*(?P<message>.*)")

# How many start-up repair rounds a run gets before it stops and asks. The same argument as
# `build_results.MAX_BUILD_ROUNDS` and a smaller number, because each round here costs a whole
# build and a launch rather than a compile.
MAX_STARTUP_ROUNDS = 3

# Findings reported from one log. A failure cascade repeats itself; the cap keeps a checkpoint
# and a prompt readable without hiding that there were more.
MAX_DIAGNOSTICS = 25


def _short(name: str) -> str:
    return str(name or "").rsplit(".", 1)[-1].split("<", 1)[0].split("`", 1)[0].strip()


def diagnostics(log_tail: str) -> List[Dict[str, Any]]:
    """What a .NET start-up failure says, as rows. `[]` when nothing is recognised.

    -> `[{kind, missing, wanted_by, detail, fixable}]`, where `fixable` says whether a
    deterministic stage could act on it or whether it needs the converter.

    Deduplicated on (kind, missing, wanted_by): the container reports the same unresolvable
    service once per consumer that wanted it, and a prompt listing the same line eleven times
    reads as eleven problems.
    """
    text = str(log_tail or "")
    if not text.strip():
        return []
    found: List[Dict[str, Any]] = []
    seen = set()

    def add(kind, missing, wanted_by, detail, fixable):
        key = (kind, _short(missing), _short(wanted_by))
        if key in seen:
            return
        seen.add(key)
        found.append({"kind": kind, "missing": str(missing or ""),
                      "wanted_by": str(wanted_by or ""), "detail": detail,
                      "fixable": bool(fixable)})

    for match in _UNRESOLVED.finditer(text):
        missing, wanted = match.group("missing"), match.group("wanted_by") or ""
        add("missing_service", missing, wanted,
            f"The container has no registration for `{_short(missing)}`"
            + (f", and `{_short(wanted)}` asks for one." if wanted else "."),
            True)
    for match in _SCOPE.finditer(text):
        add("scope_mismatch", match.group("missing"), match.group("wanted_by"),
            f"`{_short(match.group('wanted_by'))}` is registered as a singleton and holds "
            f"`{_short(match.group('missing'))}`, which is {match.group('lifetime')}. It would "
            f"be created once and reused for every later request.", True)
    for match in _VIEW.finditer(text):
        add("missing_view", match.group("view"), "",
            f"A controller renders the view `{match.group('view')}`, and no view file was "
            f"emitted under a name the view engine looks for.", False)
    for match in _AMBIGUOUS.finditer(text):
        add("ambiguous_route", "", "",
            "Two actions claim the same URL, so routing cannot choose between them: "
            + (match.group("detail") or "").strip()[:300], False)

    if not found:
        # Nothing recognised. Harvest the type names so the caller can still route it, and keep
        # the exception line so the converter is told what actually happened rather than that
        # something did.
        unhandled = _UNHANDLED.search(text)
        headline = (f"{unhandled.group('type')}: {unhandled.group('message')}".strip()[:300]
                    if unhandled else "")
        for name in dict.fromkeys(_TYPE_TOKEN.findall(text)):
            add("unclassified", name, "",
                headline or "The application did not start, and the reason is not a shape this "
                            "parser recognises. The output is below.", False)
    return found[:MAX_DIAGNOSTICS]


def summarize_for_prompt(issues: List[Dict[str, Any]], log_tail: str = "") -> str:
    """The rework instruction, in the shape every other checker here uses.

    The log goes in as well as the findings. A parser that recognised the message can say what
    to do; one that did not has nothing better to offer than what the process actually printed,
    and withholding it to keep the prompt tidy is how a rework gets asked to guess.
    """
    if not issues and not log_tail:
        return ""
    lines = [
        "## The application built, and would not start",
        "",
        "No compiler can see any of this. The solution compiles with zero errors and the "
        "process fails while starting, so it is found by launching it and not before.",
        "",
    ]
    for issue in issues[:MAX_DIAGNOSTICS]:
        lines.append(f"- **{issue['kind'].replace('_', ' ')}** — {issue['detail']}")
    if log_tail:
        lines += ["", "What the process printed:", "",
                  "```",
                  "\n".join(str(log_tail).strip().splitlines()[-25:]),
                  "```"]
    return "\n".join(lines)


def _owner_of(symbols: List[Dict[str, Any]], name: str) -> str:
    """The unit that declared this type, by short name. "" when this run declared no such type."""
    wanted = _short(name)
    if not wanted:
        return ""
    for symbol in symbols:
        if _short(symbol.get("name") or "") == wanted and symbol.get("unit_id"):
            return str(symbol["unit_id"])
    return ""


def route(run_id: str, issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Which unit can act on each finding. -> `{by_unit, unrouted, services}`.

    Routed by **who declared the type**, which is the only anchor a start-up message offers —
    it has no file and no line, so the `build_results` file-path route is unavailable and a
    reader who tried to reuse it would get `unmapped` for every row (the trap that module's
    own docstring names).

    `services` is the deterministic half: types the container could not resolve that this run
    *did* declare. Those are a wiring gap rather than a conversion defect, and the wire-up pass
    can register them exactly, given the one thing it was missing — the knowledge that they are
    needed at all.
    """
    symbols = store.list_symbols(run_id, limit=100000)
    by_unit: Dict[str, List[Dict[str, Any]]] = {}
    unrouted: List[Dict[str, Any]] = []
    services: List[str] = []
    for issue in issues or ():
        if issue.get("kind") in ("missing_service", "scope_mismatch") \
                and _owner_of(symbols, issue.get("missing") or ""):
            services.append(_short(issue.get("missing") or ""))
        # The consumer first: it is the unit whose constructor asks for the thing that could
        # not be supplied, so it is the one with something to change.
        owner = (_owner_of(symbols, issue.get("wanted_by") or "")
                 or _owner_of(symbols, issue.get("missing") or ""))
        if owner:
            by_unit.setdefault(owner, []).append(issue)
        else:
            unrouted.append(issue)
    return {"by_unit": by_unit, "unrouted": unrouted,
            "services": sorted(dict.fromkeys(services))}


def _repair(run_id: str, log_tail: str) -> Dict[str, Any]:
    """Act on a start-up failure the way a failed build is acted on. Never raises.

    -> `{"reopened": n, "note": str, "blocking": bool}`. `blocking` is True when a human has to
    look — nothing was routable, or the run has used its rounds — and False when work went back
    into the loop and the run only needs resuming.

    The findings go onto the units as `startup_issues`, which is how they reach the converter:
    `_convert_one` already assembles its rework guidance from the unit's own meta, so this is
    the same road every other checker uses rather than a second one.
    """
    try:
        issues = diagnostics(log_tail)
        attempts = store.runtime_attempts(run_id)
        routed = route(run_id, issues)
        if attempts > MAX_STARTUP_ROUNDS:
            return {"reopened": 0, "blocking": True, "note": (
                f"\n\nThe run has been launched {attempts} times and has used all "
                f"{MAX_STARTUP_ROUNDS} of its automatic start-up repair rounds, so nothing was "
                f"reopened. Continuing would be spending against a loop that has not finished "
                f"the job.")}
        if not routed["by_unit"]:
            return {"reopened": 0, "blocking": True, "note": (
                f"\n\n{len(issues)} finding(s) were read out of that output, and none of them "
                f"names a type this run produced — so there is nothing to send back through "
                f"conversion. That points at the project skeleton or the host rather than at "
                f"the converted code." if issues else "")}

        for unit_id, unit_issues in routed["by_unit"].items():
            store.set_unit_meta(unit_id, {"startup_issues": unit_issues,
                                          "startup_log": (log_tail or "")[-4000:]})
        # The wire-up unit goes back too, and carries what it was missing. Reopened and told
        # nothing it would re-run `host_plan` over unchanged inputs, emit byte-identical output
        # and spend the round proving it — the D-65 trap, one stage over. The list of types the
        # container could not resolve is exactly the fact it did not have.
        wireup_units = store.list_units(run_id, kind="wireup") if routed["services"] else []
        for wireup_unit in wireup_units[:1]:
            store.set_unit_meta(wireup_unit["id"], {"required_services": routed["services"]})
        outcome = store.reopen_units(
            run_id, sorted(routed["by_unit"]) + [u["id"] for u in wireup_units[:1]],
            reason=f"the application did not start: {len(issues)} finding(s) from launch "
                   f"attempt {attempts}")
        if not outcome.get("reopened"):
            return {"reopened": 0, "blocking": True, "note": (
                f"\n\nEvery unit these findings point at has already been reworked as many "
                f"times as it is allowed, so nothing was reopened.")}
        # `units` is a list of `{unit_id, path}`, not a list of ids. Passing the dicts
        # straight to `get_unit` bound a dict as a SQL parameter, the whole repair fell
        # into the fail-open branch, and a run that could have been fixed was reported
        # as unrepairable — a shape worth naming, because the failure was invisible.
        kinds = {(store.get_unit(row["unit_id"]) or {}).get("kind") or "component"
                 for row in outcome.get("units") or []} or {"component"}
        store.update_run(run_id, phase=build_results._rework_phase(kinds))
        store.append_event(
            run_id, "startup_reopened",
            f"{outcome['reopened']} unit(s) reopened from {len(issues)} start-up finding(s)"
            + (f"; {len(routed['services'])} service(s) recorded for the wire-up pass"
               if routed["services"] else ""), level="warning")
        return {"reopened": outcome["reopened"], "blocking": False, "note": (
            f"\n\n**{outcome['reopened']} unit(s) have been sent back to be converted again**, "
            f"with what the process printed in front of them. Resume the run to apply the fixes "
            f"and build again."
            + (f" {len(routed['services'])} service(s) the container could not resolve are "
               f"declared by this run, so the wire-up pass will register them."
               if routed["services"] else "")
            + (f" {len(routed['unrouted'])} finding(s) name nothing this run produced and were "
               f"left for a human." if routed["unrouted"] else ""))}
    except Exception:  # noqa: BLE001
        # A repair loop that can fail the report it was reading is worse than no repair loop
        # (D-101). The checkpoint below still carries the log, which is where this was before.
        logger.exception("Could not act on the start-up failure for run %s (continuing)", run_id)
        return {"reopened": 0, "blocking": True, "note": ""}


def _describe(checks: List[Dict[str, Any]], started: bool, log_tail: str) -> str:
    if not started:
        return (
            "The process never answered on its port, so nothing could be requested of it. "
            "This is the failure a compiler cannot see: the solution builds and the "
            "application cannot start.\n\n"
            + ("Its last output was:\n\n    "
               + "\n    ".join((log_tail or "").strip().splitlines()[-20:])
               if log_tail else "The runner captured no output."))
    bad = [c for c in checks if c["status"] >= 500 or (c["error"] and not c["status"])]
    lines = [f"  - {c['method']} {c['path']} -> "
             + (f"{c['status']}" if c["status"] else f"no response ({c['error']})")
             for c in (bad or checks)[:CHECKPOINT_CHECKS]]
    if not bad:
        head = ("Every URL this run's analysis found answered 404. The host is running and the "
                "application is not reachable on any of its own routes.")
    else:
        head = (f"{len(bad)} of {len(checks)} URL(s) failed. A 5xx here is a defect no compiler "
                f"and no cross-file check can see — a missing container registration or a view "
                f"whose file was emitted under a different name both build with zero errors.")
    return head + "\n\n" + "\n".join(lines) + (
        "\n\nThe runner's last output:\n\n    "
        + "\n    ".join((log_tail or "").strip().splitlines()[-20:]) if log_tail else "")


def summary_for_run(run_id: str) -> Dict[str, Any]:
    """The one-line runtime state a run manifest carries.

    Always a dict, and `never_run` is one of its states — the same argument
    `build_results.summary_for_run` makes and the opposite of `parity`, where no recorded case
    means the customer never opted in. Every run produces something that either starts or does
    not, so "nobody looked" has to be sayable out loud rather than rendering as nothing.
    """
    latest = store.latest_runtime(run_id)
    if not latest:
        return {"status": "never_run", "checks": 0, "failing": 0, "started": False}
    checks = store.runtime_checks(run_id, latest["id"])
    return {
        "status": latest.get("status") or "never_run",
        "round": latest.get("round") or 0,
        "checks": len(checks),
        "failing": sum(1 for c in checks
                       if (c.get("status") or 0) >= 500
                       or (c.get("error") and not c.get("status"))),
        "started": bool(latest.get("started")),
        "skipped_reason": latest.get("skipped_reason") or "",
        "report_id": latest.get("id") or "",
    }


def gate(run_id: str) -> Dict[str, Any]:
    """At the end of the run, ask whether anybody ever started this thing.

    The same shape as `build_results.gate` and `parity.gate`, and blocking for the same reason
    the build gate now is: an export nobody ran is not one this product can claim starts. Every
    check up to here reads code or asks a compiler, and both of the defects in the module
    docstring passed all of them.

    Waivable. The platform cannot start a process (D-05), so it can only make sure the question
    is asked of something that can — and a gate that made a run unfinishable would be worse
    than no gate. The waiver stays on the record: the badge keeps saying `never_run`.
    """
    state = summary_for_run(run_id)
    if state["status"] in ("passed", "failed"):
        # `failed` already raised its own blocking checkpoint at ingest, with the diagnosis.
        if state["status"] == "passed":
            # Somebody started it and it answered, which is exactly what this gate asked for.
            # Left open it parks the run in `paused` forever — the state php-crud-v2 sat in
            # after a clean build and a passed startup check, with nothing left to do and no
            # way to finish. Same half-a-ratchet as the build gate above.
            store.withdraw_checkpoints(
                run_id, ("runtime_required",),
                f"Withdrawn automatically: the application started and answered "
                f"{state.get('checks') or 0} probe(s) with "
                f"{state.get('failing') or 0} failing, which is the evidence this gate was "
                f"asking for.")
        return state
    if not any(c["kind"] == "runtime_required" for c in store.list_checkpoints(run_id)):
        store.create_checkpoint(
            run_id, "runtime_required",
            ("Nobody has started this application"
             if state["status"] == "never_run" else
             "The startup check was skipped"),
            detail=(
                (f"The startup check was skipped: {state['skipped_reason']}\n\n"
                 if state.get("skipped_reason") else
                 "Every check this run performed reads the converted code, or asks a compiler "
                 "to read it. None of them starts the application — and the two defects this "
                 "product has shipped that a customer noticed both compiled with zero errors: "
                 "a view emitted under a name its controller does not ask for, and a container "
                 "missing a registration. The first throws on one route, the second on all of "
                 "them.\n\n")
                + "The shortest path is a machine with the .NET SDK on it:\n\n"
                f"    python scripts/build_run.py --loop --serve --run {run_id} \\\n"
                "        --api <this deployment> --user <you> --password <yours>\n\n"
                "It builds the export, starts the entry project against a seeded database, "
                "requests every URL this run's own analysis found, and posts what it saw "
                "back here.\n\n"
                "Answer this to finish the run without that evidence."
            )[:4000],
            blocking=True,
            payload={"status": state["status"],
                     "command": f"python scripts/build_run.py --loop --serve --run {run_id}"},
        )
    store.append_event(
        run_id, "runtime_not_checked",
        "Nothing has started this export. Every check that ran reads code or compiles it, and "
        "an application that builds can still fail on its first request.",
        level="warning")
    return state


def startup_plan(run_id: str) -> Dict[str, Any]:
    """What a runner needs to know to start this export and probe it.

    Assembled here rather than left to the runner, so the URLs it requests are the ones the
    run's own analysis found — not a list somebody maintains alongside.
    """
    from backend.services.modernization import evidence as evidence_module

    run = store.get_run(run_id) or {}
    plan = run.get("plan") or {}
    projects = [p.get("name") for p in (plan.get("projects") or []) if p.get("name")]
    urls = evidence_module.route_urls(store.list_evidence(run_id, kind="route"))
    return {
        "run_id": run_id,
        # Entry project first, which `architecture.normalize_projects` guarantees (D-93) and
        # `_scaffold_one` relies on for the same reason.
        "entry_project": projects[0] if projects else "",
        "projects": projects,
        "urls": urls,
        "probe_count": sum(1 for u in urls if u["probe"]),
        # Denominators, in the style the discovery gate settled on (D-97): a coverage figure
        # means nothing without the population it was drawn from.
        "unprobed": [{"path": u["path"], "reason": u["reason"]} for u in urls if not u["probe"]],
        "config_keys": sorted({e["value"] for e in store.list_evidence(run_id, kind="config_key")
                               if e.get("value")}),
    }
