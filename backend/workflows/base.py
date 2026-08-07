"""Shared plumbing for composite workflows."""
from typing import Any, Callable, List, Optional, Sequence, Tuple

# (label, will_run) — a workflow's plan for this particular request.
Step = Tuple[str, bool]


class ProgressReporter:
    """Reports "step N of M: <label>" as a workflow advances.

    `on_progress(step_index, total_steps, step_label)` is the contract the job-status
    endpoint relies on (see `job_service.start_job`), so a client polling a long-running
    workflow can see which LLM call is in flight rather than just "still running".

    `total` counts only steps that will actually run for this request — conditional
    sub-reviews the caller supplied no content for are not counted.

    Step counting lives here because it used to be duplicated in three places
    (`orchestrator.full_review`, `v1_agents.run_full`, `main.py`'s legacy `/api/review/full`),
    each free to drift from the others.
    """

    def __init__(self, steps: Sequence[Step], on_progress: Optional[Callable[[int, int, str], Any]] = None):
        self.total = count_steps(steps)
        self._index = 0
        self._on_progress = on_progress

    def step(self, label: str) -> None:
        self._index += 1
        if self._on_progress:
            self._on_progress(self._index, self.total, label)


def count_steps(steps: Sequence[Step]) -> int:
    return sum(1 for _, will_run in steps if will_run)


def combine_files(files: List[dict]) -> str:
    """Flatten generated/migrated files into one reviewable blob.

    Used by the scaffold and migration workflows before handing the output to the
    governance agents — they take a single string, not a file list.
    """
    return "\n\n".join(
        f"# ── {f.get('path', '')} ──\n{f.get('content', '')}" for f in files
    )
