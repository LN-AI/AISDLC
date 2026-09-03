"""Findings as SARIF, so they can leave this platform.

SARIF (Static Analysis Results Interchange Format, OASIS, v2.1.0) is the format every
security tool speaks. Without it, findings produced here can only be read here — the tool is
a silo a customer has to visit separately, alongside the dashboard where their security
findings already live.

One serializer over the finding shape the pipeline already produces buys:

* **GitHub's Security tab.** Upload a SARIF file from a workflow and findings appear as code
  scanning alerts, annotated on the diff, with GitHub's own triage and dismissal on top of
  them. That is a findings UI this project does not have to build.
* **Azure DevOps, GitLab, Defect Dojo, and most SIEMs**, all of which ingest SARIF.
* **A durable artifact for an audit.** A JSON file with rules, locations and severities in a
  standard schema outlives whatever this application's database looks like later.

Two decisions worth stating, because SARIF permits both ways:

**Only findings with a real location get a `physicalLocation`.** A consumer draws a diff
annotation from that, and an annotation on the wrong line reads as the tool being wrong about
the code. A finding with no verified file and line becomes a result with no location rather
than a result pointing at line 1 of something.

**`level` is derived from severity, and `rank` carries the number.** SARIF's `level`
vocabulary is only error/warning/note/none, which cannot express five severities; `rank`
(0-100) can, so both are emitted and a consumer can use whichever it understands.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

TOOL_NAME = "LM AI-SDLC ReviewOps Agent"
TOOL_URI = "https://learnedmachine.ai"

# severity 1-5 -> (SARIF level, rank). Critical and High are `error` because they are the two
# that block a release here; Moderate is `warning`; the rest are `note`. Rank is what carries
# the distinction a consumer that understands it should see.
_LEVEL_BY_SEVERITY = {
    5: ("error", 100.0),
    4: ("error", 80.0),
    3: ("warning", 55.0),
    2: ("note", 30.0),
    1: ("note", 10.0),
}
_DEFAULT_LEVEL = ("warning", 50.0)

# Findings the platform decided not to report are not exported as results. A discarded
# finding failed verification and a suppressed one is in a fixture; exporting either would
# push noise into the customer's own dashboard, which is the fastest way to have the
# integration switched off. The counts still travel, in `properties`, so the export is not
# silently lossy.
_KIND_BY_STATE = {
    "reported": "fail",
    "suppressed": "informational",
}


def _rule_id(finding: Dict[str, Any]) -> str:
    """A stable rule id. `control_id` is the platform's own control vocabulary (SEC-003,
    VAPT-011, AI-001), which is exactly what a rule id is for — and it means a customer's
    dashboard groups findings the same way this platform's reports do."""
    return str(finding.get("control_id") or "LM-UNSPECIFIED").upper()


def _rules(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One rule per distinct control seen, in first-seen order.

    SARIF wants the rule catalogue up front and results referencing it by index. Emitting
    only the rules actually used, rather than the whole control library, keeps the file
    proportional to the findings in it.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for f in findings:
        rid = _rule_id(f)
        if rid in seen:
            continue
        help_parts = []
        if f.get("recommendation"):
            help_parts.append(str(f["recommendation"]))
        rule: Dict[str, Any] = {
            "id": rid,
            "name": rid.replace("-", ""),
            "shortDescription": {"text": str(f.get("title") or rid)[:200]},
            "fullDescription": {"text": str(f.get("description") or f.get("title") or rid)[:1000]},
            "defaultConfiguration": {
                "level": _LEVEL_BY_SEVERITY.get(_severity(f), _DEFAULT_LEVEL)[0],
            },
            "properties": {
                "security-severity": str(_LEVEL_BY_SEVERITY.get(_severity(f), _DEFAULT_LEVEL)[1] / 10),
                "tags": _tags(f),
            },
        }
        if help_parts:
            rule["help"] = {"text": "\n\n".join(help_parts)[:2000]}
        seen[rid] = rule
    return list(seen.values())


def _tags(finding: Dict[str, Any]) -> List[str]:
    """Tags a consumer filters on. GitHub's Security tab shows these."""
    tags = ["security"]
    for key in ("owasp_category", "owasp_ref", "cwe_id", "nist_ref"):
        value = finding.get(key)
        if value:
            tags.append(str(value))
    if finding.get("detector") == "pattern":
        # Worth carrying across: a consumer triaging a hundred alerts wants to know which
        # came from a deterministic scanner and which from a model.
        tags.append("deterministic-scanner")
    return tags


def _severity(finding: Dict[str, Any]) -> int:
    try:
        return int(finding.get("severity") or 0)
    except (TypeError, ValueError):
        return 0


def _location(finding: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A physical location, or None when we cannot honestly give one.

    Requires a path *and* a positive line. A consumer turns this into an annotation on a
    specific line of a specific file, and an annotation in the wrong place is worse than an
    unplaced result — it reads as the tool being wrong about the code rather than merely
    vague about where.
    """
    path = str(finding.get("file_path") or "").strip().lstrip("./")
    line = finding.get("line_start")
    if not path or not isinstance(line, int) or line <= 0:
        return None
    region: Dict[str, Any] = {"startLine": line}
    end = finding.get("line_end")
    if isinstance(end, int) and end > line:
        region["endLine"] = end
    if finding.get("evidence"):
        # `snippet` is what makes an alert readable without opening the file. Safe to include:
        # the evidence quote is at most three lines and, for a secret finding, the value was
        # already redacted before it got here.
        region["snippet"] = {"text": str(finding["evidence"])[:1000]}
    return {
        "physicalLocation": {
            "artifactLocation": {"uri": path},
            "region": region,
        }
    }


def _result(finding: Dict[str, Any], rule_index: int, state: str) -> Dict[str, Any]:
    level, rank = _LEVEL_BY_SEVERITY.get(_severity(finding), _DEFAULT_LEVEL)
    message = str(finding.get("title") or "Finding")
    if finding.get("description"):
        message = f"{message}: {finding['description']}"

    result: Dict[str, Any] = {
        "ruleId": _rule_id(finding),
        "ruleIndex": rule_index,
        "level": level,
        "rank": rank,
        "kind": _KIND_BY_STATE.get(state, "fail"),
        "message": {"text": message[:2000]},
        "properties": {
            "severity": _severity(finding),
            "riskLevel": finding.get("risk_level"),
            "isBlocking": bool(finding.get("is_blocking")),
            "isNew": finding.get("is_new"),
            # Provenance a triager needs and no other field carries: was this proven against
            # the source, how sure was the reviewer, and did a scanner or a model find it.
            "evidenceVerified": finding.get("evidence_verified"),
            "confidence": finding.get("confidence"),
            "detector": finding.get("detector") or "model",
            "fingerprint": finding.get("fingerprint"),
        },
    }
    if finding.get("cvss_score") is not None:
        result["properties"]["cvssScore"] = finding["cvss_score"]
    if finding.get("cvss_vector"):
        result["properties"]["cvssVector"] = finding["cvss_vector"]
    if finding.get("recommendation"):
        result["properties"]["remediation"] = str(finding["recommendation"])[:2000]
    if state == "suppressed":
        # SARIF's own mechanism for "we know, and it does not count" — so a consumer files it
        # as suppressed rather than as an alert the customer has to dismiss again.
        result["suppressions"] = [{
            "kind": "external",
            "justification": str(finding.get("suppressed_reason") or "filtered by path")[:500],
        }]

    location = _location(finding)
    if location:
        result["locations"] = [location]
    # `partialFingerprints` is how a consumer recognises the same finding across runs. Ours
    # deliberately excludes the line number (see `finding_pipeline.fingerprint`), which is
    # exactly the property SARIF wants here: an alert should survive code moving down a file.
    if finding.get("fingerprint"):
        result["partialFingerprints"] = {"lmFindingFingerprint/v1": finding["fingerprint"]}
    return result


def from_review(
    review: Dict[str, Any],
    *,
    include_suppressed: bool = True,
    repo_root: str = "",
) -> Dict[str, Any]:
    """One review result -> a SARIF 2.1.0 log.

    `include_suppressed` defaults on, marked with SARIF `suppressions`, because a consumer
    that wants them can then show them and one that does not will hide them — whereas
    dropping them here makes that choice for everyone. Discarded findings are never included:
    they failed verification, and pushing unfounded alerts into a customer's dashboard is how
    the integration gets switched off.
    """
    reported = list(review.get("findings") or [])
    suppressed = list(review.get("suppressed_findings") or []) if include_suppressed else []
    all_findings = reported + suppressed

    rules = _rules(all_findings)
    rule_index = {r["id"]: i for i, r in enumerate(rules)}

    results = [
        _result(f, rule_index.get(_rule_id(f), 0), "reported") for f in reported
    ] + [
        _result(f, rule_index.get(_rule_id(f), 0), "suppressed") for f in suppressed
    ]

    coverage = review.get("coverage") or {}
    source = review.get("source") or {}
    invocation: Dict[str, Any] = {
        # False when any chunk failed. A consumer reading this knows the scan is partial and
        # should not treat an absence of alerts in the unscanned part as a clean result — the
        # same claim the gate makes internally, in the field SARIF provides for it.
        "executionSuccessful": not bool(coverage.get("chunks_failed")),
    }
    if coverage.get("chunks_failed"):
        invocation["toolExecutionNotifications"] = [{
            "level": "warning",
            "message": {"text": (
                f"{coverage['chunks_failed']} of {coverage.get('chunks_total', '?')} piece(s) "
                f"of this source could not be reviewed, so part of it was never examined."
            )},
        }]

    log: Dict[str, Any] = {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {
                "driver": {
                    "name": TOOL_NAME,
                    "informationUri": TOOL_URI,
                    "semanticVersion": "1.0.0",
                    "rules": rules,
                }
            },
            "invocations": [invocation],
            "results": results,
            "properties": {
                "gateStatus": review.get("gate_status"),
                "riskScore": review.get("risk_score"),
                "modelGateStatus": review.get("model_gate_status"),
                "reviewId": review.get("review_id"),
                "reviewType": review.get("review_type"),
                "agent": review.get("agent_name"),
                # The export is not silently lossy: what was left out, and why, travels with
                # it even though those items are not results.
                "discardedFindingCount": len(review.get("discarded_findings") or []),
                "suppressedFindingCount": len(review.get("suppressed_findings") or []),
                "newFindingCount": review.get("new_findings_count"),
                "preExistingFindingCount": review.get("pre_existing_count"),
                "waivedFindingCount": review.get("waived_count"),
                "filesReviewed": source.get("files_reviewed") or coverage.get("files_reviewed"),
                "filesSkipped": source.get("files_skipped"),
                "coverageComplete": review.get("coverage_complete"),
            },
        }],
    }
    if repo_root:
        log["runs"][0]["originalUriBaseIds"] = {"SRCROOT": {"uri": repo_root}}

    logger.info(
        "SARIF export: %d result(s) over %d rule(s) from review %s",
        len(results), len(rules), review.get("review_id"),
    )
    return log
