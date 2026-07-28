"""LM AI-SDLC — Issue Triage analytics dashboard.

Aggregates stats from stored `issue_triage` reviews — new issues by severity/priority, unassigned
tickets, duplicate rate, confidence distribution, team workload, escalations, and recent reports.
Pure read/aggregation layer over the existing reviews table; no new scanning logic.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import backend.database as database
from backend.services.auth_service import decode_token

router = APIRouter(prefix="/api/v1/issue-triage", tags=["Issue Triage Dashboard"])
_bearer = HTTPBearer(auto_error=False)


def _require_auth(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(status_code=401, detail="Bearer token required")
    user = decode_token(creds.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


@router.get("/dashboard", summary="Issue Triage analytics for a project (or all projects)")
def get_issue_triage_dashboard(project_name: Optional[str] = None, limit: int = 500, user: dict = Depends(_require_auth)):
    rows = database.list_reviews_by_type("issue_triage", project_name, limit=limit)

    severity_counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    priority_counts = {"P0 - Immediate": 0, "P1 - Urgent": 0, "P2 - Normal": 0, "P3 - Low priority": 0}
    gate_counts = {"Green": 0, "Amber": 0, "Red": 0}
    category_counts: dict = {}
    team_workload: dict = {}
    confidence_buckets = {"90-100 (auto)": 0, "70-89 (confirm)": 0, "0-69 (manual)": 0}
    unassigned = 0
    duplicates = 0
    escalations = 0
    confidence_sum = 0
    recent = []

    for row in rows:
        result = row.get("result", {})
        severity = result.get("severity", "Low")
        priority = result.get("priority", "P3 - Low priority")
        confidence = result.get("confidence_score", 0) or 0
        category = result.get("issue_category", "Unknown")
        assignment = result.get("recommended_assignment", {}) or {}
        team = assignment.get("recommended_team", "Unknown")
        dup_status = (result.get("duplicate_analysis", {}) or {}).get("status", "None found")

        if severity in severity_counts:
            severity_counts[severity] += 1
        if priority in priority_counts:
            priority_counts[priority] += 1
        gate_counts[row.get("gate_status", "Green")] = gate_counts.get(row.get("gate_status", "Green"), 0) + 1
        category_counts[category] = category_counts.get(category, 0) + 1
        team_workload[team] = team_workload.get(team, 0) + 1
        confidence_sum += confidence

        if confidence >= 90:
            confidence_buckets["90-100 (auto)"] += 1
        elif confidence >= 70:
            confidence_buckets["70-89 (confirm)"] += 1
        else:
            confidence_buckets["0-69 (manual)"] += 1

        if team == "Insufficient ownership data" or assignment.get("assignment_confidence", 100) < 50:
            unassigned += 1
        if dup_status != "None found":
            duplicates += 1
        if (result.get("escalation", {}) or {}).get("required"):
            escalations += 1

        recent.append({
            "review_id": row["id"],
            "created_at": row["created_at"],
            "summary": result.get("issue_summary", row.get("input_summary", ""))[:150],
            "category": category,
            "severity": severity,
            "priority": priority,
            "gate_status": row.get("gate_status"),
            "confidence_score": confidence,
            "recommended_team": team,
        })

    total = len(rows)
    return {
        "project_name": project_name or "",
        "total_issues": total,
        "severity_breakdown": severity_counts,
        "priority_breakdown": priority_counts,
        "gate_breakdown": gate_counts,
        "category_breakdown": category_counts,
        "team_workload": team_workload,
        "confidence_distribution": confidence_buckets,
        "unassigned_count": unassigned,
        "duplicate_count": duplicates,
        "duplicate_rate_pct": round(100 * duplicates / total, 1) if total else 0.0,
        "escalations_required": escalations,
        "average_confidence": round(confidence_sum / total, 1) if total else 0.0,
        "recent_issues": recent[:20],
    }


@router.get("/reviews/{review_id}", summary="Get one issue triage report by review ID")
def get_issue_triage_review(review_id: str, user: dict = Depends(_require_auth)):
    review = database.get_review(review_id)
    if not review or review.get("review_type") != "issue_triage":
        raise HTTPException(status_code=404, detail="Issue triage report not found")
    return review
