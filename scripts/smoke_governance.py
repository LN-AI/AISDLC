"""Offline smoke test for the governance-reporting and Core-SDLC-Review changes.

Run it:

    python scripts/smoke_governance.py

Covers, in order:

  1. `full`'s step plan — the default is byte-for-byte what it ran before, the three opt-in
     stages only fire when both the flag and their artifact are present, and the run reports
     what it skipped.
  2. `reports` — attribution to a project, the new listing endpoint, and the scoping bug
     that made every report a 404 for anyone but a super admin.
  3. `governance_review` — findings from a scaffold's auto-review reaching the release gate,
     which they never did while they lived only under `result["governance_review"]`.
  4. The swarm surface the new Agent Hub panel drives.

Same conventions as the three suites beside it (D-09): plain script, no framework, exits 0
or 1, driven through `TestClient` against a throwaway SQLite file with
`llm_gateway.call_tool` stubbed — so it needs no server, no API key and no network.
"""
import contextlib
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP_DB = os.path.join(tempfile.gettempdir(), f"smoke_governance_{uuid.uuid4().hex}.db")
os.environ["DATABASE_PATH"] = _TMP_DB
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-smoke-test-placeholder")
os.environ["LEGACY_ROUTES_REQUIRE_AUTH"] = "false"
os.environ["UI_REQUIRE_PROJECT"] = "false"

from backend import config  # noqa: E402

config.DATABASE_PATH = _TMP_DB

from backend.services import llm_gateway  # noqa: E402

# ── LLM stub ───────────────────────────────────────────────────────────────
# One Critical finding, so anything that reaches a gate is visibly Red. That is the point of
# several checks below: a finding that never arrives leaves a *Green* gate, which is exactly
# how the governance_review hole stayed invisible.
_llm_calls = []
# The prompts themselves, so section 10 can assert that a later stage of `full` actually
# received what earlier stages found rather than the caller's context alone.
_llm_inputs = []


def _stub_call_tool(system_prompt, user_content, tool_schema, llm_override=None, max_tokens=None):
    name = tool_schema.get("name", "?")
    _llm_calls.append(name)
    _llm_inputs.append(user_content if isinstance(user_content, str) else str(user_content))
    llm_gateway._record_usage("anthropic", config.MODEL, 1000, 100)
    result = {
        "findings": [{
            "control_id": "SEC-003", "severity": 5, "risk_level": "Critical",
            "title": "Hardcoded credential", "description": "from the smoke stub",
            "recommendation": "move it to a secret store", "is_blocking": True,
        }],
        "risk_score": 80.0,
        "gate_status": "Red",
        "summary": "stubbed summary",
        "recommendations": ["stubbed recommendation"],
        "compliance_flags": ["NIST-AI-RMF"],
    }
    # The generative agents are validated against their own schemas, which have no
    # `findings` — they must return files or the workflow raises on an empty result.
    if "scaffold" in name or "migration" in name or "codegen" in name:
        result["files"] = [{"path": "app.py", "content": "KEY = 'sk-live-123'\n", "purpose": "entrypoint"}]
    return result


llm_gateway.call_tool = _stub_call_tool

from fastapi.testclient import TestClient  # noqa: E402

import backend.database as database  # noqa: E402
import backend.main as main  # noqa: E402
from backend.workflows import full_review as full_workflow  # noqa: E402
from backend.workflows.base import count_steps  # noqa: E402
from backend.workflows.shared import strictest_gate  # noqa: E402

# ── Harness ────────────────────────────────────────────────────────────────
_failures = []
_checks = 0


def check(label, condition, detail=""):
    global _checks
    _checks += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))
        _failures.append(label)


def section(title):
    print(f"\n{title}")


def _labels(steps):
    return [label for label, runs in steps if runs]


def main_():
    database.init_db()
    main._init_default_admin()
    stack = contextlib.ExitStack()
    client = stack.enter_context(TestClient(main.app))

    section("Setup")
    login = client.post("/api/auth/login",
                        json={"username": "admin", "password": config.DEFAULT_ADMIN_PASSWORD})
    check("admin can log in", login.status_code == 200, login.text[:300])
    admin = {"Authorization": f"Bearer {login.json()['token']}"}

    proj = client.post("/api/v1/projects", json={"name": "Governance Smoke"}, headers=admin)
    check("project created", proj.status_code in (200, 201), proj.text[:300])
    project_id = proj.json()["project"]["id"]
    scoped = {**admin, "X-Project-Id": project_id}

    # A second project, to prove scoping actually excludes rather than just filtering.
    other = client.post("/api/v1/projects", json={"name": "Other Project"}, headers=admin)
    other_id = other.json()["project"]["id"]

    # ── 1. The step plan ───────────────────────────────────────────────────
    section("1. Core SDLC Review — the step plan")

    base = full_workflow.plan_steps(requirement="r", code="c", architecture="a")
    check("the default plan is unchanged — five stages plus gate and report",
          _labels(base) == ["Requirement Review", "Architecture Review", "Code Review",
                            "Release Gate Evaluation", "Report Generation"],
          str(_labels(base)))
    check("...and none of the opt-in stages are in it",
          not any(s in _labels(base) for s in
                  ("Security Review (STRIDE)", "VAPT Assessment", "Sensitive Data Scan")))

    everything = full_workflow.plan_steps(
        requirement="r", compliance_artifact="c", architecture="a", code="c",
        prompt_config={"system_prompt": "p"},
        include_security=True, include_vapt=True, include_sensitive_data=True,
    )
    check("every stage on is 10 steps", count_steps(everything) == 10, str(count_steps(everything)))
    check("the opt-in stages run in SDLC order, each after the artifact it reads",
          _labels(everything).index("Security Review (STRIDE)") > _labels(everything).index("Architecture Review")
          and _labels(everything).index("VAPT Assessment") > _labels(everything).index("Code Review"),
          str(_labels(everything)))

    orphan = full_workflow.plan_steps(requirement="r", include_vapt=True, include_security=True,
                                      include_sensitive_data=True)
    check("a flag without its artifact is a no-op, not an error",
          _labels(orphan) == ["Requirement Review", "Release Gate Evaluation", "Report Generation"],
          str(_labels(orphan)))

    check("ALL_STEPS covers every label the planner can emit",
          {label for label, _ in everything} <= set(full_workflow.ALL_STEPS))

    # ── 2. Running it ──────────────────────────────────────────────────────
    section("2. Core SDLC Review — running it")

    def run_full(payload):
        resp = client.post("/api/review/full", json=payload, headers=scoped)
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        for _ in range(400):
            job = client.get(f"/api/v1/agents/jobs/{job_id}", headers=scoped).json()
            if job.get("status") in ("completed", "failed"):
                return job
        raise AssertionError("job never finished")

    default_job = run_full({"project_name": "Governance Smoke", "code": "KEY='sk-live'", "requirement": "story"})
    check("a default run completes", default_job["status"] == "completed", str(default_job.get("error"))[:300])
    default_result = default_job["result"]

    not_run = default_result.get("steps_not_run", [])
    check("the result names the stages it skipped", bool(not_run), str(default_result.keys()))
    check("...including the three security stages nobody asked for",
          {"Security Review (STRIDE)", "VAPT Assessment", "Sensitive Data Scan"} <= set(not_run),
          str(not_run))
    check("...and never claims a stage it actually ran",
          "Code Review" not in not_run and "Requirement Review" not in not_run, str(not_run))
    check("the gate is not silently Green over a Critical finding",
          default_result.get("gate_status") == "Red", str(default_result.get("gate_status")))

    opted_job = run_full({
        "project_name": "Governance Smoke", "code": "KEY='sk-live'", "architecture": "one API instance",
        "include_security": True, "include_vapt": True, "include_sensitive_data": True,
    })
    check("a run with every opt-in stage completes", opted_job["status"] == "completed",
          str(opted_job.get("error"))[:300])
    opted = opted_job["result"]
    sources = {s["source"] for s in opted.get("sub_reviews", [])}
    check("the opted-in agents really ran", {"security", "vapt", "sensitive_data"} <= sources, str(sources))
    check("...and nothing is reported as skipped",
          not [s for s in opted.get("steps_not_run", [])
               if s in ("Security Review (STRIDE)", "VAPT Assessment", "Sensitive Data Scan")],
          str(opted.get("steps_not_run")))
    check("the job's step count matches the longer plan",
          opted_job["total_steps"] > default_job["total_steps"],
          f"{opted_job['total_steps']} vs {default_job['total_steps']}")

    check("the product kept its id, so existing keys and cost history still resolve",
          client.get("/api/v1/products", headers=admin).status_code == 200
          and any(p["id"] == "full" for p in client.get("/api/v1/products", headers=admin).json()["products"]))
    titles = {p["id"]: p["title"] for p in client.get("/api/v1/products", headers=admin).json()["products"]}
    check("...while the title now says what it actually does", titles.get("full") == "Core SDLC Review",
          str(titles.get("full")))

    # ── 3. Reports ─────────────────────────────────────────────────────────
    section("3. Governance reports")

    check("the full review wrote a report", bool(default_result.get("report_id")))
    report_id = default_result["report_id"]

    listing = client.get("/api/reports", headers=scoped)
    check("GET /api/reports exists", listing.status_code == 200, listing.text[:300])
    reports = listing.json()["reports"]
    check("it lists the report this project produced", any(r["id"] == report_id for r in reports),
          str([r["id"] for r in reports]))
    check("the summary row carries the gate without shipping the whole document",
          bool(reports) and "gate_status" in reports[0] and "report_json" not in reports[0],
          str(list(reports[0].keys()) if reports else []))

    other_listing = client.get("/api/reports", headers={**admin, "X-Project-Id": other_id}).json()["reports"]
    check("another project's reports are not in the list",
          not any(r["id"] == report_id for r in other_listing), str([r["id"] for r in other_listing]))

    # The bug: reports are written with review_id=None, so scoping through the review meant
    # a project member could never read their own report.
    member = client.post("/api/auth/users", headers=admin, json={
        "username": "reportreader", "email": "rr@example.com",
        "password": "Reader@1234", "role": "developer"})
    check("a non-admin user was created", member.status_code in (200, 201), member.text[:200])
    client.post(f"/api/v1/projects/{project_id}/members",
                json={"username": "reportreader"}, headers=admin)
    member_login = client.post("/api/auth/login",
                               json={"username": "reportreader", "password": "Reader@1234"})
    member_hdr = {"Authorization": f"Bearer {member_login.json()['token']}",
                  "X-Project-Id": project_id}

    fetched = client.get(f"/api/reports/{report_id}", headers=member_hdr)
    check("a project member can read their own project's report",
          fetched.status_code == 200, f"{fetched.status_code} {fetched.text[:200]}")
    check("...and it comes back as a document, not just a row",
          fetched.status_code == 200 and "executive_summary" in (fetched.json().get("report") or {}),
          str(list((fetched.json().get('report') or {}).keys()))[:200] if fetched.status_code == 200 else "")

    outsider = client.post("/api/auth/users", headers=admin, json={
        "username": "outsider", "email": "out@example.com",
        "password": "Outsid@1234", "role": "developer"})
    check("an unrelated user was created", outsider.status_code in (200, 201), outsider.text[:200])
    out_login = client.post("/api/auth/login",
                            json={"username": "outsider", "password": "Outsid@1234"})
    out_hdr = {"Authorization": f"Bearer {out_login.json()['token']}"}
    denied = client.get(f"/api/reports/{report_id}", headers=out_hdr)
    check("someone outside the project still gets a 404, not the report",
          denied.status_code == 404, f"{denied.status_code} {denied.text[:200]}")

    generated = client.post("/api/v1/agents/report-generate", headers=scoped, json={
        "project_name": "Governance Smoke",
        "review_ids": [default_result["review_id"]],
    })
    check("report-generate runs over prior reviews", generated.status_code == 200, generated.text[:300])
    check("...and its response carries what the report screen renders",
          generated.status_code == 200
          and {"executive_summary", "findings_by_category", "controls_failed", "gate_status"}
          <= set(generated.json().keys()),
          str(list(generated.json().keys()))[:300] if generated.status_code == 200 else "")

    # ── 4. governance_review reaching the gate ─────────────────────────────
    section("4. Auto-review findings reach the release gate")

    check("strictest_gate takes the worst, not the first",
          strictest_gate("Green", "Red", "Amber") == "Red" and strictest_gate("Green", "") == "Green")
    check("...and fails closed on a gate value it doesn't recognise",
          strictest_gate("Green", "banana") == "Red")

    scaffold = client.post("/api/codegen/scaffold", headers=scoped, json={
        "project_name": "Governance Smoke", "language": "Python",
        "backend_framework": "FastAPI", "requirement": "an API", "auto_review": True,
    })
    check("a scaffold with auto-review completes", scaffold.status_code == 200, scaffold.text[:300])
    scaffold_review_id = scaffold.json().get("review_id")
    check("it produced a review row", bool(scaffold_review_id))

    stored = client.get(f"/api/reviews/{scaffold_review_id}", headers=scoped).json()
    check("its findings are on the top level, where the gate reads them",
          bool(stored["result"].get("findings")),
          str(list(stored["result"].keys()))[:300])
    check("...and the saved row counts them instead of showing zero",
          stored.get("findings_count", 0) > 0, str(stored.get("findings_count")))
    check("...and the row's gate reflects the auto-review, not a default Green",
          stored.get("gate_status") == "Red", str(stored.get("gate_status")))
    check("the nested detail both agents produced is still there",
          "code_review" in (stored["result"].get("governance_review") or {}))

    gate = client.post("/api/release-gate", headers=scoped, json={
        "project_name": "Governance Smoke", "review_ids": [scaffold_review_id],
    })
    check("a release gate over that scaffold runs", gate.status_code == 200, gate.text[:300])
    check("...and actually sees its findings rather than gating over nothing",
          gate.status_code == 200 and gate.json().get("total_findings", 0) > 0,
          gate.text[:300])

    # ── 5. The swarm surface the Agent Hub panel drives ────────────────────
    section("5. Swarm surface")

    policies = client.get("/api/v1/swarm/policies", headers=scoped)
    check("GET /api/v1/swarm/policies responds", policies.status_code == 200, policies.text[:200])
    body = policies.json() if policies.status_code == 200 else {}
    check("it returns the names the policy dropdown lists",
          {"standard", "hipaa", "pci", "ai-product"} <= set(body.get("policies", {})),
          str(list(body.get("policies", {}))))
    check("...each with the required/optional split the panel shows",
          all({"required_agents", "optional_agents", "min_passing_score"} <= set(p)
              for p in body.get("policies", {}).values()))

    swarm = client.post("/api/v1/swarm", headers=scoped, json={
        "project_name": "Governance Smoke", "policy_name": "hipaa",
        "text": "def f(): KEY='sk-live-123'",
    })
    check("a swarm run over pasted text succeeds", swarm.status_code == 200, swarm.text[:300])
    if swarm.status_code == 200:
        sw = swarm.json()
        check("...and returns the fields the results grid renders",
              {"swarm_gate", "swarm_risk_score", "results", "policy_applied", "agents_run"} <= set(sw),
              str(list(sw))[:300])
        check("...with a per-agent row for everything that ran",
              len(sw["results"]) == len(sw["agents_run"]) and len(sw["results"]) > 1,
              f"{len(sw['results'])} results / {len(sw['agents_run'])} run")

    # ── 6. The approval loop ───────────────────────────────────────────────
    section("6. Approvals — a blocked gate reaches a human")

    # `save_review` has always enqueued an approval for a Red gate or a Critical finding.
    # What it did not do was stamp `project_id`, so the queue could not be scoped and
    # showed every customer's blocked releases to anyone holding a token.
    queue = client.get("/api/approvals?status=pending", headers=scoped)
    check("GET /api/approvals responds", queue.status_code == 200, queue.text[:200])
    pending = queue.json().get("approvals", []) if queue.status_code == 200 else []
    check("a blocked review is waiting for sign-off", len(pending) > 0, f"{len(pending)} pending")
    check("...attributed to the project that ran it, so the queue can be scoped at all",
          pending and all(a.get("project_id") == project_id for a in pending),
          str({a.get("project_id") for a in pending}))
    check("...at urgent priority for a Red gate",
          any(a.get("priority") == "urgent" for a in pending),
          str([a.get("priority") for a in pending]))
    check("...naming the review it is gating",
          all(a.get("review_id") for a in pending))

    stats = client.get("/api/approvals/stats", headers=scoped).json()
    check("the sidebar badge counts them", stats.get("pending", 0) == len(pending), str(stats))

    check("the queue is scoped — another project sees none of them",
          client.get("/api/approvals?status=pending",
                     headers={**admin, "X-Project-Id": other_id}).json().get("approvals") == [])
    check("...and it refuses an unauthenticated caller outright",
          client.get("/api/approvals").status_code == 401)

    # The Release Gate page's own endpoint. It used to evaluate inline and persist nothing.
    before_reviews = client.get("/api/reviews?limit=100", headers=scoped).json()["total"]
    legacy = client.post("/api/release-gate", headers=scoped, json={
        "project_name": "Governance Smoke", "review_ids": [scaffold_review_id],
    })
    check("the legacy /api/release-gate route still answers", legacy.status_code == 200,
          legacy.text[:300])
    lg = legacy.json() if legacy.status_code == 200 else {}
    check("...and now persists its decision instead of discarding it",
          bool(lg.get("review_id")), str(list(lg))[:200])
    check("...so the release decision reaches History like every other result",
          client.get("/api/reviews?limit=100", headers=scoped).json()["total"] > before_reviews)
    check("...and the response names the approval it raised",
          lg.get("approval_required") is True and bool(lg.get("approval_id")),
          str({k: lg.get(k) for k in ("gate_status", "approval_required", "approval_id")}))
    check("...with the gate decision itself now pickable for a governance report",
          any(r["id"] == lg.get("review_id") for r in
              client.get("/api/reviews?limit=100", headers=scoped).json()["reviews"]))

    pasted = client.post("/api/release-gate", headers=scoped, json={
        "project_name": "Governance Smoke",
        "findings": [{"control_id": "X-1", "severity": 5, "risk_level": "Critical",
                      "title": "pasted", "description": "d", "recommendation": "r",
                      "is_blocking": True}],
    })
    check("pasting findings straight into the gate still works", pasted.status_code == 200,
          pasted.text[:300])

    target = pending[0]["id"]
    decided = client.post(f"/api/approvals/{target}/approve", headers=scoped,
                          json={"comment": "accepted for the smoke run"})
    check("an approval can be decided", decided.status_code == 200, decided.text[:200])
    check("...and a second decision on it is refused",
          client.post(f"/api/approvals/{target}/approve", headers=scoped,
                      json={"comment": "again"}).status_code == 400)
    check("...and it is invisible to a caller scoped elsewhere",
          client.get(f"/api/approvals/{target}",
                     headers={**admin, "X-Project-Id": other_id}).status_code == 404)

    # ── 7. The findings read side ──────────────────────────────────────────
    section("7. Findings — the list the lifecycle never had")

    listing = client.get("/api/v1/findings?status=open", headers=scoped)
    check("GET /api/v1/findings responds", listing.status_code == 200, listing.text[:200])
    payload = listing.json() if listing.status_code == 200 else {}
    rows = payload.get("findings", [])
    check("it returns this project's open findings", len(rows) > 0, str(payload)[:200])
    check("...with the review context a row is meaningless without",
          all({"review_id", "review_type", "review_created_at"} <= set(r) for r in rows))
    check("...and a severity breakdown for the filter chips",
          {"critical", "high", "total"} <= set(payload.get("counts", {})),
          str(payload.get("counts")))
    check("the Critical from the stub is in it",
          any(r.get("severity") == 5 for r in rows))
    check("filtering by severity narrows it",
          all(r.get("severity", 0) >= 4 for r in
              client.get("/api/v1/findings?min_severity=4", headers=scoped).json()["findings"]))
    check("findings are scoped — another project sees none of them",
          client.get("/api/v1/findings",
                     headers={**admin, "X-Project-Id": other_id}).json()["findings"] == [])

    # Waiving through the list, then proving it leaves the open set.
    waive_id = rows[0]["id"]
    open_before = client.get("/api/v1/findings?status=open", headers=scoped).json()["total"]
    waived = client.patch(f"/api/v1/findings/{waive_id}", headers=scoped,
                          json={"status": "accepted_risk", "comment": "smoke"})
    check("a finding reached from the list can be waived", waived.status_code == 200, waived.text[:200])
    open_after = client.get("/api/v1/findings?status=open", headers=scoped).json()["total"]
    check("...and it drops out of the open set", open_after == open_before - 1,
          f"{open_before} -> {open_after}")
    check("...and is still reachable under its new status",
          any(r["id"] == waive_id for r in
              client.get("/api/v1/findings?status=accepted_risk", headers=scoped).json()["findings"]))
    check("a finding in another project cannot be waived by id",
          client.patch(f"/api/v1/findings/{waive_id}",
                       headers={**admin, "X-Project-Id": other_id},
                       json={"status": "open", "comment": "x"}).status_code == 404)

    # ── 8. Audit events that were only ever filter options ─────────────────
    section("8. Audit trail — the event types nothing emitted")

    events = client.get("/api/audit?limit=200", headers=admin).json()
    kinds = {e.get("event_type") for e in events.get("events", events.get("audit", []))}
    check("`user_login` is now written", "user_login" in kinds, str(sorted(kinds)))
    check("`approval_created` is now written", "approval_created" in kinds, str(sorted(kinds)))
    check("...alongside the two that always were",
          {"review_created", "approval_decided"} <= kinds, str(sorted(kinds)))

    # ── 9. The swarm's error gate ──────────────────────────────────────────
    section("9. Swarm — a crashed required agent cannot pass")

    from backend.models.swarm_models import AgentSwarmResult, SwarmRequest
    from backend.services.swarm_service import SwarmService, BUILTIN_POLICIES

    policy = BUILTIN_POLICIES["standard"]
    required = policy.required_agents[0]
    req = SwarmRequest(project_name="Governance Smoke", text="x")

    crashed = SwarmService()._aggregate(
        req=req, policy=policy,
        results={required: AgentSwarmResult(
            agent=required, gate_status="Green", risk_score=0.0, summary="",
            error="boom — the agent never ran")},
        agents_run=[required], source_type="text", source_ref="",
    )
    check("a required agent that errored is reported as errored",
          crashed.agents_errored == [required], str(crashed.agents_errored))
    check("...and the swarm does NOT come back GREEN over a check that never ran",
          crashed.swarm_gate != "GREEN", crashed.swarm_gate)
    check("...it comes back AMBER — incomplete, but still not blocking (the documented promise)",
          crashed.swarm_gate == "AMBER", crashed.swarm_gate)

    clean = SwarmService()._aggregate(
        req=req, policy=policy,
        results={required: AgentSwarmResult(
            agent=required, gate_status="Green", risk_score=1.0, summary="clean")},
        agents_run=[required], source_type="text", source_ref="",
    )
    check("a required agent that actually ran clean still gates GREEN",
          clean.swarm_gate == "GREEN", clean.swarm_gate)

    # ── 10. `full` as a pipeline, not a batch ──────────────────────────────
    section("10. Core SDLC Review — later stages see earlier findings")

    _llm_inputs.clear()
    run_full({"project_name": "Governance Smoke", "requirement": "story", "code": "KEY='sk-live'"})
    carried = [text for text in _llm_inputs if "EARLIER STAGES OF THIS SAME REVIEW" in text]
    check("a later stage's prompt carries what earlier stages found",
          len(carried) > 0, f"{len(_llm_inputs)} prompts, {len(carried)} carrying a digest")
    check("...naming the actual finding rather than just a count",
          any("Hardcoded credential" in text for text in carried))
    check("...attributed to the stage that raised it",
          any("[requirement]" in text for text in carried))
    check("the first stage is unaffected — there is nothing to carry yet",
          "EARLIER STAGES OF THIS SAME REVIEW" not in (_llm_inputs[0] if _llm_inputs else ""))

    # ── done ───────────────────────────────────────────────────────────────
    stack.close()
    print("\n" + "-" * 60)
    if _failures:
        print(f"FAILED: {len(_failures)} of {_checks} checks")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"OK: all {_checks} checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main_()
    finally:
        with contextlib.suppress(OSError):
            os.remove(_TMP_DB)
    sys.exit(code)
