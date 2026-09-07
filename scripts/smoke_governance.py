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
    # ── 11. What every assessment owes its reader ────────────────────────
    #
    # Code Review and VAPT go through `chunked_review`, which refuses a truncated response
    # and floors the gate. Every other assessment returned whatever the model said. The
    # worst instance was a regulated one: the 21 CFR Part 11 verifier back-filled
    # `files_scanned` from its own input list when the model did not say what it read, so an
    # FDA report could state that 400 files were verified when nobody knew whether they
    # were.
    #
    # The judgement in `attest.py` is which protections are universal and which are not, so
    # that is what most of this section is about.
    section("11. Attestation — no assessment claims what nobody checked")

    from backend.agents import attest as _at11
    from backend.agents import base_agent as _ba11

    _FILES11 = [{"path": "app/a.py", "content": "x = 1\n"},
                {"path": "app/b.py", "content": "y = 2\n"},
                {"path": "app/c.py", "content": "z = 3\n"}]

    # ── The claim that started this ──────────────────────────────────────
    _silent = _at11.coverage_claim({"files_scanned": []}, _FILES11)
    check("a model that named no files is not credited with all of them",
          _silent["files_claimed"] == 0 and _silent["files_supplied"] == 3,
          str(_silent))
    check("...and the report says so in words an auditor can act on",
          "not the same as having examined all of them" in _silent["note"],
          _silent["note"])
    check("...and does not call the coverage complete",
          _silent["complete"] is False)

    _partial = _at11.coverage_claim({"files_scanned": ["app/a.py"]}, _FILES11)
    check("a partial claim names the gap", _partial["files_unclaimed"] == 2
          and set(_partial["unclaimed_sample"]) == {"app/b.py", "app/c.py"},
          str(_partial))
    check("...and says to treat the rest as unexamined",
          "unexamined" in _partial["note"], _partial["note"])

    _all = _at11.coverage_claim(
        {"files_scanned": ["app/a.py", "app/b.py", "app/c.py"]}, _FILES11)
    check("a complete claim is reported as complete", _all["complete"] is True, str(_all))

    _invented = _at11.coverage_claim(
        {"files_scanned": ["app/a.py", "app/nope.py"]}, _FILES11)
    check("a file named but never supplied is surfaced",
          _invented["files_not_supplied"] == 1
          and _invented["not_supplied_sample"] == ["app/nope.py"], str(_invented))
    check("...with a warning, because it usually means an invented filename",
          "invented" in _invented["note"], _invented["note"])

    _paste = _at11.coverage_claim({"files_scanned": []}, [])
    check("a pasted artifact has no per-file coverage to report, and says that",
          _paste["files_supplied"] == 0 and "pasted artifact" in _paste["note"],
          str(_paste))

    # ── Degeneracy: the failure that is hardest to notice ────────────────
    _big11 = "x = 1\n" * 200
    _raised = ""
    try:
        _at11.attest({"findings": [], "summary": "", "gate_status": "Green"},
                     agent_name="Test Agent", artifact=_big11)
    except RuntimeError as exc:
        _raised = str(exc)
    check("a response with no findings and no summary is refused, not stored",
          "returned an empty result" in _raised, _raised[:200])
    check("...naming the agent, so the error says which one",
          "Test Agent" in _raised, _raised[:120])

    _ok11 = _at11.attest({"findings": [], "summary": "Nothing found.",
                          "gate_status": "Green"},
                         agent_name="Test Agent", artifact=_big11)
    check("a genuinely clean result with a summary is kept",
          _ok11["gate_status"] == "Green")
    _tiny = _at11.attest({"findings": [], "summary": "", "gate_status": "Green"},
                         agent_name="Test Agent", artifact="x = 1")
    check("a trivial artifact may legitimately produce nothing",
          _tiny["gate_status"] == "Green",
          "rejecting these would break every caller that probes an endpoint with a "
          "one-liner")

    # ── The gate floor, and the aliased key two schemas use ─────────────
    _crit = {"findings": [{"severity": 5, "title": "Critical thing", "is_blocking": True}],
             "summary": "One problem.", "gate_status": "Green", "risk_score": 5.0}
    _floored = _at11.attest(_crit, agent_name="Test Agent", artifact=_big11)
    check("a Critical cannot come out Green", _floored["gate_status"] == "Red",
          _floored["gate_status"])
    check("...and the model's own answer is kept, so the disagreement can be measured",
          _floored["model_gate_status"] == "Green", str(_floored.get("model_gate_status")))

    _overall = {"findings": [{"severity": 5, "title": "Critical", "is_blocking": True}],
                "summary": "One problem.", "overall_gate_status": "Green",
                "overall_risk_score": 5.0}
    _fl2 = _at11.attest(_overall, agent_name="Part 11", artifact=_big11,
                        gate_key="overall_gate_status", risk_key="overall_risk_score")
    check("the aliased gate key is floored too", _fl2["overall_gate_status"] == "Red",
          "the Part 11 verifier and the GxP document check name their verdict "
          "`overall_gate_status`, so `reconcile_gate` would not have seen it at all")
    check("...and written back under both names, so no screen can disagree with the record",
          _fl2["gate_status"] == "Red" and _fl2["overall_gate_status"] == "Red",
          f"gate_status={_fl2.get('gate_status')} "
          f"overall={_fl2.get('overall_gate_status')}")

    # ── Evidence verification is opt-in, and this is why ────────────────
    _described = {
        "findings": [{"severity": 4, "title": "No audit trail",
                      "evidence": "app/a.py / save_record() / no audit call",
                      "is_blocking": True}],
        "summary": "A gap.", "gate_status": "Amber",
    }
    _off = _at11.attest(dict(_described), agent_name="Part 11", artifact=_big11)
    check("a described (non-quoted) evidence field survives when verification is off",
          len(_off["findings"]) == 1 and not _off["discarded_findings"],
          "this schema asks for 'File path / function / code pattern', so a substring "
          "check would discard a correct finding for being phrased as instructed")
    check("...and no verification claim is made about it",
          _off["findings"][0].get("evidence_verified") is None,
          "False would mean 'we checked and could not confirm', which is untrue of a "
          "finding nobody checked -- the same class of untruth as the back-filled file list")
    check("...and the result records that quotes were not checked",
          _off["attested"]["evidence_verified"] is False, str(_off["attested"]))

    _on = _at11.attest(dict(_described), agent_name="Code", artifact=_big11,
                       verify_evidence=True)
    check("with verification on, the same finding is discarded",
          not _on["findings"] and len(_on["discarded_findings"]) == 1,
          "which is correct for a schema that demanded a verbatim quote, and wrong for one "
          "that did not -- hence the flag")

    _absent = _at11.attest(
        {"findings": [{"severity": 3, "title": "There is no threat model",
                       "is_blocking": False}],
         "summary": "Design gap.", "gate_status": "Amber"},
        agent_name="Architecture", artifact=_big11, verify_evidence=True)
    check("a finding with no evidence at all is never discarded",
          len(_absent["findings"]) == 1,
          "a design-level finding has no line to quote, and dropping it would delete real "
          "issues the moment a finding was about an absence")

    # ── A generative result is not judged ───────────────────────────────
    _gen = _at11.attest({"scaffold": "...", "files": ["a.py"]},
                        agent_name="Codegen", artifact=_big11)
    check("a result with no findings and no gate is left alone",
          "gate_status" not in _gen and "discarded_findings" not in _gen,
          "inventing a verdict for generated work would be judging something nobody asked "
          "to have judged")

    # ── And it is actually wired in, which is the part that matters ─────
    import inspect as _i11
    for _name11 in ("review", "review_architecture", "review_security"):
        _owner = next(c for c in (_ba11.BaseAgent, _ba11.ArchitectureBaseAgent,
                                  _ba11.SecurityReviewBaseAgent)
                      if _name11 in c.__dict__)
        _src11 = _i11.getsource(_owner.__dict__[_name11])
        check(f"`{_name11}` attests its result", "attest.attest(" in _src11,
              "this is the shared method for the requirement, prompt, compliance, "
              "sensitive-data, provenance, triage, security and architecture products -- "
              "one edit covers all of them")
        check(f"...against the artifact, not the prompt we built around it",
              "artifact=content" in _src11,
              "padding the degeneracy threshold with our own context would let a one-line "
              "artifact clear the 200-character floor and escape the check")
        check(f"...and does not claim to have verified quotes it was never given",
              "verify_evidence" not in _src11,
              "no schema reached by these methods asks for a verbatim quote")

    # Two products that the `base_agent` edit does **not** reach, each for its own reason.
    # Both were found by looking for what the shared method misses rather than by assuming
    # it covered everything.
    from backend.agents.security_intelligence import sensitive_data_agent as _sd11
    _sdsrc11 = _i11.getsource(_sd11.SensitiveDataAgent.scan)
    check("the sensitive-data scan attests, even though it never calls `review`",
          "attest.attest(" in _sdsrc11,
          "it overrides `_call_claude` and `scan` calls it directly, so the protections "
          "added to `BaseAgent.review` pass it by -- and a truncated 'no PHI found' is the "
          "most costly wrong answer this particular agent can give")
    check("...and does not verify evidence, because its sample is deliberately masked",
          "verify_evidence" not in _sdsrc11,
          "the schema asks for an 'Obfuscated sample (mask real data)', so a substring "
          "check against the source is guaranteed to fail for every correctly-masked "
          "finding -- it would discard exactly the findings that followed instructions")

    # The API surface. These two endpoints build their response by hand rather than through
    # `_wrap`'s passthrough, so a new field reaches the browser (which reads the stored
    # result) and not the API -- and the API is what a compliance pipeline reads.
    from backend.routes import v1_agents as _v11
    for _fn11 in (_v11.run_cfr21_verification, _v11.run_gxp_document_check):
        _src11 = _i11.getsource(_fn11)
        check(f"`{_fn11.__name__}` returns the coverage claim to an API caller",
              '"coverage_claim"' in _src11,
              "an integrator seeing an empty `files_scanned` needs this to tell them it "
              "means 'the assessment did not say' rather than 'nothing was scanned' -- "
              "without it the honest answer looks like a bug and invites someone to "
              "re-add the back-fill")
        check(f"...and which checks were applied", '"attested"' in _src11)
        check(f"...and the model's verdict before the floor",
              '"model_gate_status"' in _src11,
              "so a reader can see when the arithmetic and the model disagreed")

    from backend.agents.security_intelligence import cfr21_agent as _c11
    _csrc11 = _i11.getsource(_c11.CFR21VerificationAgent.verify)
    check("the Part 11 verifier no longer back-fills its own coverage",
          'result["files_scanned"] = [f.get("path"' not in _csrc11,
          "this is the line that turned 'the model did not tell us' into '400 files were "
          "verified' inside an FDA report")
    check("...and reconciles the claim instead",
          'claim_key="files_scanned"' in _csrc11, _csrc11[-400:])

    # -- 12. The attestation fires, not just appears in the source -----------
    #
    # Section 11 proves `attest.attest(` is written into each agent by reading its source.
    # That is worth having, but it cannot tell you the call *works*: a wrong `gate_key`
    # would leave a Part 11 verdict unfloored and the source grep would still pass, because
    # the text is there either way. `SensitiveDataAgent` is the reason this matters -- it
    # overrides `_call_claude`, so it was the one agent the shared `base_agent` edit did not
    # reach (D-346), and only a behavioural check distinguishes "wired" from "written".
    #
    # So this drives the three regulated agents with a stubbed model and asserts the result.
    section("12. ...and the attestation fires when the model answers")

    from backend.agents.security_intelligence.cfr21_agent import CFR21VerificationAgent
    from backend.agents.security_intelligence.gxp_document_agent import GxPDocumentAgent
    from backend.agents.security_intelligence.sensitive_data_agent import SensitiveDataAgent

    _CRIT12 = {"control_id": "SEC-003", "severity": 5, "risk_level": "Critical",
               "title": "no audit trail", "description": "", "recommendation": "",
               "is_blocking": True}
    # Long enough to clear the 200-character degeneracy floor, which is about the size of
    # the artifact under review rather than the prompt built around it.
    _FILES12 = [{"path": "app/audit.py", "content": "def log_event(e):\n    pass\n" * 20},
                {"path": "app/sign.py", "content": "def sign(u):\n    return True\n" * 20}]

    _reply12 = {}

    def _stub12(system_prompt, user_content, tool_schema, llm_override=None, max_tokens=None):
        return dict(_reply12)

    _real12, llm_gateway.call_tool = llm_gateway.call_tool, _stub12
    try:
        # A model that grades its own Critical as Green. The aliased-key case: this schema
        # calls the verdict `overall_gate_status`, which `reconcile_gate` does not know.
        _reply12 = {"findings": [dict(_CRIT12)], "overall_gate_status": "Green",
                    "overall_risk_score": 0.0, "summary": "s", "files_scanned": []}
        _out12 = CFR21VerificationAgent().verify(files=_FILES12, system_description="LIMS")
        check("the Part 11 verifier really floors its own gate, not just imports the floor",
              _out12.get("overall_gate_status") == "Red", str(_out12.get("overall_gate_status")))
        check("...and under the plain key too, so screen and stored record agree",
              _out12.get("gate_status") == "Red", str(_out12.get("gate_status")))
        check("the Part 11 verifier leaves an unstated file list empty",
              _out12.get("files_scanned") == [], str(_out12.get("files_scanned")))
        check("...and reports the gap it did not fill",
              (_out12.get("coverage_claim") or {}).get("files_supplied") == 2
              and (_out12.get("coverage_claim") or {}).get("files_unclaimed") == 2,
              str(_out12.get("coverage_claim")))

        _reply12 = {"findings": [], "overall_gate_status": "Green", "overall_risk_score": 0.0,
                    "summary": "", "files_scanned": []}
        try:
            CFR21VerificationAgent().verify(files=_FILES12, system_description="LIMS")
            check("a truncated Part 11 response does not become a compliance report", False)
        except RuntimeError:
            check("a truncated Part 11 response does not become a compliance report", True)

        _reply12 = {"findings": [dict(_CRIT12, title="unsigned SOP")],
                    "overall_gate_status": "Green", "overall_risk_score": 0.0, "summary": "s"}
        _out12 = GxPDocumentAgent().check(documents=[{"path": "SOP-1.md",
                                                      "content": "text\n" * 60}])
        check("the GxP document check really floors its own gate",
              _out12.get("overall_gate_status") == "Red", str(_out12.get("overall_gate_status")))

        # The agent D-346 is about. Its schema uses the plain keys, so nothing is aliased --
        # what is being checked is that `scan` reaches `attest` at all.
        _reply12 = {"findings": [dict(_CRIT12, title="hardcoded database password")],
                    "gate_status": "Green", "risk_score": 0.0, "summary": "s"}
        _out12 = SensitiveDataAgent().scan("PASSWORD = 'hunter2'\n" * 30)
        check("the sensitive-data scan really floors its own gate",
              _out12.get("gate_status") == "Red", str(_out12.get("gate_status")))

        _reply12 = {"findings": [], "gate_status": "Green", "risk_score": 0.0, "summary": ""}
        try:
            SensitiveDataAgent().scan("PASSWORD = 'hunter2'\n" * 30)
            check("a truncated sensitive-data scan is not stored as 'nothing found'", False)
        except RuntimeError:
            check("a truncated sensitive-data scan is not stored as 'nothing found'", True)
    finally:
        llm_gateway.call_tool = _real12

    # Nothing here re-checks that the two regulated `/api/v1` responses carry
    # `coverage_claim`/`attested`/`model_gate_status` (D-347): section 11 already does it
    # with `inspect.getsource` on `run_cfr21_verification` and `run_gxp_document_check`,
    # which is stricter than searching the file text.


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
