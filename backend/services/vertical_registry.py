"""Vertical dashboard registry — curated stat tiles + quick-launch agent shortcuts per industry.

A "vertical" bundles: which past-review stats matter most for that industry, and which agents
(and default framing) a team in that industry reaches for most often. This is a pure config
layer over agents that already exist — no new orchestration, no changes to the Swarm system.

Only "Hospital" is fully configured for now; the others are placeholders so the registry (and
the Projects UI built on top of it) doesn't need to change shape when they're filled in later.
"""
from typing import Any, Dict, List, Optional

VERTICALS: Dict[str, Dict[str, Any]] = {
    "Hospital": {
        "name": "Hospital",
        "icon": "🏥",
        "available": True,
        "description": "Healthcare providers handling PHI, clinical systems, and FDA-regulated lab/device software.",
        "frameworks": ["HIPAA", "GDPR", "21CFR11", "GxP"],
        "stat_tiles": [
            {"key": "compliance_gate", "label": "Latest Compliance Gate", "review_type": "compliance", "field": "gate_status", "kind": "gate"},
            {"key": "phi_findings", "label": "PHI/PII Findings (latest scan)", "review_type": "sensitive_data", "field": "findings_count", "kind": "count"},
            {"key": "cfr21_gate", "label": "21 CFR Part 11 Gate", "review_type": "cfr21_verification", "field": "gate_status", "kind": "gate"},
            {"key": "gxp_completeness", "label": "GxP Document Completeness", "review_type": "gxp_document_check", "field": "completeness_score", "kind": "percent"},
        ],
        "quick_launches": [
            {"page": "sensitive", "label": "Scan for PHI/PII", "icon": "🔍", "project_field": "sd-project"},
            {"page": "cfr21", "label": "21 CFR Part 11 Verify", "icon": "💊", "project_field": "cfr-project"},
            {"page": "gxp-docs", "label": "Check GxP Documents", "icon": "📑", "project_field": "gxp-project"},
            {"page": "gxp-generate", "label": "Generate Validation Package", "icon": "🏭", "project_field": "gxg-project"},
            {"page": "architecture", "label": "Architecture Review", "icon": "🏗️", "project_field": "arch-project"},
            {"page": "security", "label": "Security Review (STRIDE)", "icon": "🔐", "project_field": "sec-project"},
            {"page": "full", "label": "Full SDLC Review", "icon": "🔍", "project_field": None},
        ],
    },
    "Pharma": {
        "name": "Pharma", "icon": "💊", "available": False,
        "description": "Pharmaceutical manufacturing, clinical trials, and lab systems under GxP/21 CFR Part 11.",
        "frameworks": ["GxP", "21CFR11", "GDPR"],
        "stat_tiles": [], "quick_launches": [],
    },
    "BFSI": {
        "name": "BFSI", "icon": "🏦", "available": False,
        "description": "Banking, financial services, and insurance — PCI DSS, SOC 2, data residency.",
        "frameworks": ["PCI-DSS", "SOC2", "GDPR"],
        "stat_tiles": [], "quick_launches": [],
    },
    "IT Services": {
        "name": "IT Services", "icon": "💻", "available": False,
        "description": "General software delivery across client engagements.",
        "frameworks": ["ISO27001", "SOC2"],
        "stat_tiles": [], "quick_launches": [],
    },
    "Generic": {
        "name": "Generic", "icon": "🗂️", "available": False,
        "description": "No vertical-specific defaults — use any agent directly from the sidebar.",
        "frameworks": [],
        "stat_tiles": [], "quick_launches": [],
    },
}

VALID_VERTICALS = tuple(VERTICALS.keys())


def get_vertical(name: Optional[str]) -> Dict[str, Any]:
    return VERTICALS.get(name or "Generic", VERTICALS["Generic"])


def list_verticals() -> List[Dict[str, Any]]:
    return list(VERTICALS.values())


def resolve_stat_value(review: Optional[Dict[str, Any]], field: str) -> Any:
    """Stat fields may be a top-level reviews-table column (gate_status, findings_count, risk_score)
    or only present inside the stored result JSON (e.g. completeness_score) — check both."""
    if not review:
        return None
    if field in review and review[field] is not None:
        return review[field]
    return (review.get("result") or {}).get(field)
