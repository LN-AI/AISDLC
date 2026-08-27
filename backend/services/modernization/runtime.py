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
from typing import Any, Dict, List, Optional

from backend.services.modernization import store

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
    if status == "failed":
        detail = _describe(checks, started, payload.get("log_tail") or "")
        store.create_checkpoint(
            run_id, "runtime_failed",
            ("The converted application did not start"
             if not started else
             f"The application started and {len(failing) or len(checks)} URL(s) did not answer"),
            detail=detail[:4000], blocking=True,
            payload={"report_id": report_id, "checks": checks[:CHECKPOINT_CHECKS],
                     "started": started},
        )
    logger.info("Modernization runtime check for run %s: %s (%s URL(s), started=%s)",
                run_id, status, len(checks), started)
    return {"report_id": report_id, "status": status, "checks": len(checks),
            "started": started, "failing": len(failing)}


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
