"""Transform an artifact from one stack to another, then optionally govern the output.

Mirrors `codegen_scaffold`: generate, then optionally run the Code Review and Sensitive
Data agents over the result as a pre-release check.
"""
from typing import Any, Dict

from backend import database
from backend.workflows.shared import governance_review

WORKFLOW_ID = "migration-execute"


def run(
    orch,
    source: str,
    migration_target: str,
    file_path: str = "",
    requirement: str = "",
    auto_review: bool = False,
    context: str = "",
    project_name: str = "Unnamed Project",
) -> Dict[str, Any]:
    # Not compressed: this generates replacement code 1:1 from `source` — exact
    # source fidelity matters more here than for a review-only agent.
    result = orch.migration_agent.execute(
        source, migration_target, file_path, requirement, context, project_name
    )

    files = result.get("files", [])
    if not files:
        # Matches the guard the scaffold workflow has always had. Without it a truncated
        # LLM response was persisted as a successful-but-empty migration.
        raise RuntimeError(
            "Migration returned no files — this usually means the LLM response was "
            "truncated or malformed rather than a deliberate empty migration. Try again "
            "with a smaller source artifact, or migrate one file at a time."
        )

    if auto_review:
        result["governance_review"] = governance_review(
            orch, files, "", project_name, "Migrated code"
        )

    review_id = database.save_review(
        review_type="migration_execution",
        project_name=project_name,
        input_summary=f"Migration execution — {migration_target} ({len(files)} files)",
        input_content=source[:5000],
        result=result,
    )
    result["review_id"] = review_id
    return result
