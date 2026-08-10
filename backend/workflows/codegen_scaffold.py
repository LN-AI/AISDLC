"""Generate a runnable starter project, then optionally govern it before returning it.

Two stages: the CodeGen agent produces the files, then — if `auto_review` — the Code
Review and Sensitive Data agents check the generated output as a pre-release gate.
"""
from typing import Any, Dict

from backend import database
from backend.workflows.shared import governance_review

WORKFLOW_ID = "codegen-scaffold"


def run(
    orch,
    project_name: str,
    language: str,
    backend_framework: str,
    frontend_framework: str = "",
    database_choice: str = "",
    architecture_pattern: str = "Modular Monolith",
    requirement: str = "",
    include_auth: bool = True,
    include_docker: bool = True,
    include_tests: bool = True,
    auto_review: bool = False,
    context: str = "",
) -> Dict[str, Any]:
    result = orch.codegen_agent.generate_scaffold(
        project_name=project_name,
        language=language,
        backend_framework=backend_framework,
        frontend_framework=frontend_framework,
        database=database_choice,
        architecture_pattern=architecture_pattern,
        requirement=requirement,
        include_auth=include_auth,
        include_docker=include_docker,
        include_tests=include_tests,
        context=context,
    )

    files = result.get("files", [])
    if not files:
        raise RuntimeError(
            "Scaffold generation returned no files — this usually means the LLM response "
            "was truncated or malformed rather than a deliberate empty scaffold. Try again "
            "with a narrower scope (fewer options, e.g. skip docker/tests) or retry."
        )

    if auto_review:
        gov = governance_review(orch, files, language, project_name, "Generated scaffold")
        result["governance_review"] = gov
        # Lifted to the top level, not just nested: `load_findings_from_reviews` and
        # `database.save_review` both read `result["findings"]`, so a scaffold whose
        # auto-review found Criticals otherwise reached the release gate as zero findings
        # and showed Green on the dashboard.
        result["findings"] = gov["findings"]
        result["gate_status"] = gov["gate_status"]
        result["risk_score"] = gov["risk_score"]

    review_id = database.save_review(
        review_type="codegen_scaffold",
        project_name=project_name,
        input_summary=f"Codegen scaffold — {language}/{backend_framework}"
                      f"{'/' + frontend_framework if frontend_framework else ''} ({len(files)} files)",
        input_content=requirement[:5000],
        result=result,
    )
    result["review_id"] = review_id
    return result
