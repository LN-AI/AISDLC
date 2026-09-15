"""What an application-testing session has to check its assertions against.

A suite written from a running application can only ever be as good as the answer to one
question: **is there anything to compare the application to?** With nothing, the best it can
do is describe what it saw — useful for catching a change, useless for catching a mistake
that was already there. That is not a theoretical concern. A live session against this
platform's own interface, run with nothing supplied, produced eleven tests of which nine
were some form of "the button is visible": green, confident and worthless, because a test
that checks a control exists passes just as happily when the feature behind it is broken.

So this module collects the answer from the three places it can come from, and — this is the
part that matters — records **who said it**, because that decides what a finished test may
claim about itself.

    stated    someone wrote it down.  A typed requirement, or a document uploaded to the
              project's Requirements or QA shelves.
    derived   this platform generated it, from something a person wrote.  Scenarios and test
              cases produced by the two upstream QA agents.
    observed  nothing said it.  The test records what the application did on the day it was
              explored and claims no more than that.

**Why `derived` exists at all.** Test cases are a near-perfect brief for this product — they
arrive as numbered steps with an expected result per step, which is exactly the shape of an
automated test. The temptation is to treat them as a specification and let a test that
matches one claim it verified the requirement. That would launder a model's guess into a
verified fact in two hops, which is precisely what D-406 warns about: a model's claim about
its own output is not evidence. A person's requirement sits at the root of the chain, so the
material is genuinely worth using — but the chain has to be visible at the far end, and one
word on each assertion is the cheapest way to make it visible.

Stdlib and `backend` service imports only; no agent imports, because this is consulted both
when a session opens (can it start at all?) and when the suite is written (what may it
claim?), and those are different call stacks.
"""
import logging
from typing import Any, Dict, List, Optional

from backend import database
from backend.services.knowledge_base import knowledge_base

logger = logging.getLogger(__name__)

# The document shelves this product reads, and deliberately not all of them.
#
# `qa` is the obvious one. `requirement` is the one that actually matters: a specification is
# filed under Requirements because that is where it belongs, and it is simultaneously the
# single most useful document a test writer could be handed. Without this, a customer would
# upload their spec, watch nothing improve, and conclude the feature does not work.
#
# Everything else is left out on purpose. The cost of reading a deployment runbook is not the
# money; it is that twenty pages of irrelevant material make the three relevant paragraphs
# harder to find, and the model has a fixed amount of attention to spend.
DOC_CATEGORIES = ("requirement", "qa")

# Review types whose output can be used as a brief. Both are produced by the QA agents that
# sit upstream of this one; both are optional and most sessions will have neither.
SOURCE_REVIEW_TYPES = ("test_scenarios", "test_cases")

# Caps on what is folded into the generation prompt. A project with forty test cases must not
# turn one call into an unaffordable one, and a brief nobody read is worse than a short one.
MAX_SOURCE_REVIEWS = 5
MAX_CASES_RENDERED = 40
MAX_SCENARIOS_RENDERED = 40
MAX_BRIEF_CHARS = 40_000


def project_name_for(project_id: Optional[str]) -> str:
    """The name the knowledge base files documents under, from the id a session carries."""
    if not project_id:
        return ""
    try:
        project = database.get_project(project_id)
    except Exception:
        logger.exception("Could not resolve project %s for its documents", project_id)
        return ""
    return (project or {}).get("name") or ""


def documents_for(project_name: str) -> str:
    """Requirements and QA documents for this project, as one block of context.

    Both shelves, concatenated, and capped by the knowledge base itself. Returns an empty
    string when the project has neither, which is what makes it usable as a yes/no test of
    whether a session has anything to check against.
    """
    parts = []
    for category in DOC_CATEGORIES:
        try:
            text = knowledge_base.get_context(category, project_name=project_name)
        except Exception:
            logger.exception("Knowledge base lookup failed for category %s", category)
            text = ""
        if text and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def _rows(value: Any) -> List[Dict[str, Any]]:
    """Objects only. The same filter every model-written array gets here (D-392)."""
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _render_cases(cases: List[Dict[str, Any]]) -> List[str]:
    """Test cases as a brief. Steps and expected results are the whole point of using them."""
    out = []
    for case in cases[:MAX_CASES_RENDERED]:
        ident = str(case.get("test_case_id") or "").strip() or "(no id)"
        title = str(case.get("title") or "").strip()
        out.append(f"- **{ident}** {title}".rstrip())
        pre = [str(p) for p in (case.get("preconditions") or []) if p]
        if pre:
            out.append(f"  - given: {'; '.join(pre[:4])}")
        for step in _rows(case.get("steps"))[:12]:
            action = str(step.get("action") or "").strip()
            expected = str(step.get("expected_result") or "").strip()
            if action and expected:
                out.append(f"  - when {action} -> expect {expected}")
            elif action:
                out.append(f"  - when {action}")
    return out


def _render_scenarios(scenarios: List[Dict[str, Any]]) -> List[str]:
    """Scenarios as a brief. Business-level flows, so title and intent are what carry."""
    out = []
    for row in scenarios[:MAX_SCENARIOS_RENDERED]:
        ident = str(row.get("scenario_id") or row.get("id") or "").strip()
        title = str(row.get("title") or row.get("scenario") or row.get("name") or "").strip()
        detail = str(row.get("description") or row.get("expected_outcome") or "").strip()
        label = f"- **{ident}** {title}".rstrip() if ident else f"- {title}"
        out.append(label)
        if detail:
            out.append(f"  - {detail[:300]}")
    return out


def brief_from_reviews(review_ids: List[str], visible_project_ids) -> Dict[str, Any]:
    """Turn finished scenario/case runs into a brief this product can test against.

    Refuses quietly rather than loudly for anything it cannot use: a review id that does not
    exist, belongs to a project the caller cannot see, or is the wrong kind of run is
    *skipped and reported*, not raised. A session should not fail to open because one of four
    references was stale, but the caller must be able to see which ones were dropped —
    silently ignoring a reference is how somebody believes their cases are being covered
    when they are not (D-316's shape).
    """
    used: List[Dict[str, str]] = []
    skipped: List[str] = []
    lines: List[str] = []
    case_ids: List[str] = []

    for review_id in list(review_ids or [])[:MAX_SOURCE_REVIEWS]:
        review = None
        try:
            review = database.get_review(review_id)
        except Exception:
            logger.exception("Could not read review %s as a session source", review_id)
        if not review:
            skipped.append(f"{review_id}: no such run")
            continue
        review_type = str(review.get("review_type") or "")
        if review_type not in SOURCE_REVIEW_TYPES:
            skipped.append(f"{review_id}: a {review_type or 'unknown'} run is not a brief")
            continue
        if visible_project_ids is not None and review.get("project_id") not in visible_project_ids:
            # 404-shaped, for the reason `_visible_session` gives: confirming that an id is
            # real is itself a disclosure.
            skipped.append(f"{review_id}: no such run")
            continue

        result = review.get("result") or {}
        if not isinstance(result, dict):
            skipped.append(f"{review_id}: its result could not be read")
            continue

        if review_type == "test_cases":
            cases = _rows(result.get("test_cases"))
            if not cases:
                skipped.append(f"{review_id}: that run produced no test cases")
                continue
            lines.append(f"### Test cases from an earlier run ({len(cases)})")
            lines.extend(_render_cases(cases))
            case_ids.extend(
                str(c.get("test_case_id") or "").strip()
                for c in cases[:MAX_CASES_RENDERED] if c.get("test_case_id")
            )
        else:
            scenarios = _rows(result.get("scenarios") or result.get("test_scenarios"))
            if not scenarios:
                skipped.append(f"{review_id}: that run produced no scenarios")
                continue
            lines.append(f"### Scenarios from an earlier run ({len(scenarios)})")
            lines.extend(_render_scenarios(scenarios))

        used.append({"review_id": review_id, "review_type": review_type})
        lines.append("")

    return {
        "markdown": "\n".join(lines)[:MAX_BRIEF_CHARS].strip(),
        "used": used,
        "skipped": skipped,
        "case_ids": [c for c in case_ids if c],
    }


def collect(*, requirement: str, project_id: Optional[str], source_review_ids: List[str],
            visible_project_ids=None) -> Dict[str, Any]:
    """Everything this session can check itself against, and where each part came from.

    The single place that answers both questions this product turns on: *may this session
    start at all* (is there any basis), and *what may its assertions claim* (is the basis
    something a person wrote, or something the platform generated).
    """
    project_name = project_name_for(project_id)
    documents = documents_for(project_name) if project_name else ""
    brief = brief_from_reviews(source_review_ids, visible_project_ids)

    typed = (requirement or "").strip()
    has_stated = bool(typed or documents)
    has_derived = bool(brief["used"])

    return {
        "project_name": project_name,
        "requirement": typed,
        "documents": documents,
        "has_documents": bool(documents),
        "brief_markdown": brief["markdown"],
        "source_reviews": brief["used"],
        "skipped_sources": brief["skipped"],
        "case_ids": brief["case_ids"],
        "has_stated": has_stated,
        "has_derived": has_derived,
        "has_any": has_stated or has_derived,
        # The strongest label any assertion in this session may honestly carry. The honesty
        # pass in `orchestrator.generate_app_suite` flattens anything above it.
        "strongest_basis": "stated" if has_stated else ("derived" if has_derived else "observed"),
    }


NOTHING_TO_CHECK_AGAINST = (
    "This session has nothing to check the application against, so its tests could only "
    "record what the application happens to do today — which catches a later change and "
    "cannot catch a mistake that is already there. Supply one of: a requirement in your own "
    "words; a requirements or QA document uploaded against this project; or a finished Test "
    "Scenario or Test Case run to cover."
)


__all__ = [
    "DOC_CATEGORIES",
    "NOTHING_TO_CHECK_AGAINST",
    "SOURCE_REVIEW_TYPES",
    "brief_from_reviews",
    "collect",
    "documents_for",
    "project_name_for",
]
