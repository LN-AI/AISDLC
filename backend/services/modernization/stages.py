"""The order of a run, as data.

A run used to have one stage that cost anything — convert this file — and one gate. It now
has several on either side of that spine, and the thing that makes them tractable is that
they are *not* a new execution model. Each stage drains rows of one `kind` from
`modernization_units`, through the same claim-and-lease loop, with the same lease recovery,
the same per-unit metering and the same one-transaction checkpoint. A stage is a filter over
a queue, not a second queue.

Two invariants hold the whole thing up:

  1. **One unit is at most one LLM call.** That is what keeps D-20 literally true and
     `agent_guard._EXPECTED_CALLS_BY_PRODUCT["modernization"] == 1` correct without touching
     the guard. A stage that needed two calls per row would have to become two stages.
  2. **`plan()` writes a stage's rows in one transaction.** `store.insert_units` is one
     `executemany` plus one commit, so a stage's rows are either all present or all absent —
     which is what makes `stage_units_exist` a safe resume cursor. A `plan()` that wrote in
     two transactions would reintroduce exactly the hole this avoids.

This module is deliberately data-only: no imports from the stage implementations, and none
from `runner`. The order lives in exactly one place, and the driver, the progress bar and
the cost estimate all read it here rather than each deriving it again — which is the same
argument `full_review.plan_steps` settles for the `full` product (D-27).
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Stage:
    id: str
    label: str
    kind: str                      # the `modernization_units.kind` this stage drains
    run_status: str                # what `modernization_runs.status` reads while it runs
    llm: bool                      # does draining a row here call a model?
    gate: str = ""                 # blocking checkpoint kind raised when the stage finishes

    # There is deliberately no `optional` flag. A stage IS skipped when its `plan()` writes
    # no rows — comprehension with no source, synthesis with fewer than two probes — but
    # that is decided in `runner._plan_stage`, not here. A field declaring it that nothing
    # read described the behaviour without causing it, and its comment went stale claiming
    # probes did not ship at all.


# Order matters and is the point. `run.phase` holds the id of the stage in progress, which
# is the coarse resume cursor; the unit rows within it are the fine one.
#
# On run_status: only `comprehending` and `reconciling` got their own status, and the test
# was "does the existing status lie?". `analyzing` means free and deterministic — showing it
# while a run spends forty metered calls is a lie on the product's main screen. Design,
# planning, scaffolding and conversion do not lie under `analyzing`/`converting`.
STAGES: Tuple[Stage, ...] = (
    Stage(
        id="comprehension",
        label="Understanding the codebase",
        kind="comprehension",
        run_status="comprehending",
        llm=True,
    ),
    Stage(
        id="synthesis",
        label="Assembling the system portrait",
        kind="synthesis",
        run_status="comprehending",
        llm=True,
    ),
    Stage(
        id="design",
        label="Designing the target architecture",
        kind="design",
        run_status="analyzing",
        llm=True,
    ),
    Stage(
        id="taskplan",
        label="Costing the plan",
        kind="",                # deterministic; owns no rows
        run_status="analyzing",
        llm=False,
        gate="plan_approval",   # GATE B — the conversion spend, against a real number
    ),
    Stage(
        id="scaffold",
        label="Generating the project skeleton",
        kind="scaffold",
        run_status="converting",
        llm=False,
    ),
    Stage(
        id="convert",
        label="Converting",
        kind="component",
        run_status="converting",
        llm=True,
    ),
    Stage(
        id="wireup",
        label="Correcting the project files",
        kind="wireup",
        run_status="converting",
        llm=False,              # deterministic, like the scaffold — no model call at all
    ),
    Stage(
        id="reconcile",
        label="Reconciling the converted codebase",
        kind="",                # deterministic; owns no rows
        run_status="reconciling",
        llm=False,
    ),
)

BY_ID: Dict[str, Stage] = {s.id: s for s in STAGES}
ORDER: List[str] = [s.id for s in STAGES]

FIRST = ORDER[0]


def get(stage_id: str) -> Optional[Stage]:
    return BY_ID.get(stage_id or "")


def current(phase: str) -> Optional[Stage]:
    """The stage a run's `phase` names, or the first one if it names nothing we know.

    A phase set by an older build — or one of the pre-conversion phases like
    `charter_required` — resolves to the first stage rather than to `None`, because `None`
    means "the run is finished" to the driver and finishing a run that never started would
    be the worst possible reading of an unrecognised string.
    """
    if phase in BY_ID:
        return BY_ID[phase]
    return BY_ID[FIRST]


def next_id(stage: Stage) -> str:
    """The id after this one, or "" when this was the last."""
    i = ORDER.index(stage.id)
    return ORDER[i + 1] if i + 1 < len(ORDER) else ""


def remaining(stage_id: str) -> List[Stage]:
    """This stage and everything after it. Used to size a progress bar honestly."""
    if stage_id not in BY_ID:
        return list(STAGES)
    return [BY_ID[s] for s in ORDER[ORDER.index(stage_id):]]


def llm_stage_ids() -> List[str]:
    return [s.id for s in STAGES if s.llm]
