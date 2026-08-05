"""Offline smoke test for the product registry, entitlements, quotas and metering.

Run it:

    python scripts/smoke_products.py

There is no test framework in this repo, so this is a plain script: it exits 0 on success
and 1 on the first failure, printing a PASS/FAIL line per check.

It needs no server, no API key and no network. The app is driven through FastAPI's
TestClient against a throwaway SQLite file, and `llm_gateway.call_tool` is replaced with a
stub that returns a canned tool result and records a fixed token spend. That keeps the run
free and deterministic while still exercising the real routing, auth, entitlement, quota
and metering paths — everything except the model itself.
"""
import contextlib
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point at a throwaway DB *before* anything imports the app.
_TMP_DB = os.path.join(tempfile.gettempdir(), f"smoke_products_{uuid.uuid4().hex}.db")
os.environ["DATABASE_PATH"] = _TMP_DB
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-smoke-test-placeholder")
# Section 8 asserts the shipped default (legacy routes open). Pin it rather than inherit
# whatever the developer's own .env happens to say, then flip it deliberately in-test.
os.environ["LEGACY_ROUTES_REQUIRE_AUTH"] = "false"

from backend import config  # noqa: E402

config.DATABASE_PATH = _TMP_DB

from backend.services import llm_gateway  # noqa: E402

# ── LLM stub ───────────────────────────────────────────────────────────────
STUB_INPUT_TOKENS = 1000
STUB_OUTPUT_TOKENS = 100
_llm_calls = []


def _stub_call_tool(system_prompt, user_content, tool_schema, llm_override=None, max_tokens=None):
    """Return something every agent's tool schema will accept, and record token spend
    through the same accumulator the real gateway uses."""
    _llm_calls.append(tool_schema.get("name", "?"))
    llm_gateway._record_usage("anthropic", config.MODEL, STUB_INPUT_TOKENS, STUB_OUTPUT_TOKENS)
    return {
        "findings": [{
            "control_id": "SEC-001", "severity": 3, "risk_level": "Moderate",
            "title": "Stubbed finding", "description": "from the smoke stub",
            "recommendation": "none", "is_blocking": False,
        }],
        "risk_score": 30.0,
        "gate_status": "Amber",
        "summary": "stubbed summary",
        "recommendations": ["stubbed recommendation"],
        "compliance_flags": ["NIST-AI-RMF"],
    }


llm_gateway.call_tool = _stub_call_tool

from fastapi.testclient import TestClient  # noqa: E402

import backend.database as database  # noqa: E402
from backend.products import registry  # noqa: E402
import backend.main as main  # noqa: E402

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


def main_():
    database.init_db()
    main._init_default_admin()
    # TestClient must be entered as a context manager: otherwise it spins up (and tears
    # down) a fresh event loop per request, which orphans the background task that
    # `/api/v1/agents/full` schedules via job_service. Entered via ExitStack purely to
    # keep the rest of this function at one indent level.
    stack = contextlib.ExitStack()
    client = stack.enter_context(TestClient(main.app))

    # ── auth + fixtures ────────────────────────────────────────────────────
    section("Setup")
    login = client.post("/api/auth/login",
                        json={"username": "admin", "password": config.DEFAULT_ADMIN_PASSWORD})
    check("admin can log in", login.status_code == 200, login.text[:300])
    admin = {"Authorization": f"Bearer {login.json()['token']}"}

    project = client.post("/api/v1/projects", json={"name": "Smoke Project"}, headers=admin)
    check("project created", project.status_code in (200, 201), project.text[:300])
    project_id = project.json()["project"]["id"]

    def new_key(label):
        resp = client.post("/api/v1/keys",
                           json={"project_id": project_id, "team_name": label}, headers=admin)
        assert resp.status_code in (200, 201), resp.text
        record = resp.json()["key"]
        return record["raw_key"], record["id"]

    open_key, open_key_id = new_key("no-plan-key")
    scoped_key, scoped_key_id = new_key("code-only-key")
    wf_key, wf_key_id = new_key("workflow-only-key")
    quota_key, quota_key_id = new_key("quota-key")

    def hdr(key):
        return {"X-API-Key": key}

    # ── 1. registry / catalogue ────────────────────────────────────────────
    section("1. Product catalogue")
    products = client.get("/api/v1/products", headers=admin)
    check("GET /api/v1/products responds", products.status_code == 200, products.text[:300])
    body = products.json()
    ids = [p["id"] for p in body["products"]]
    check("lists every registry product exactly once",
          sorted(ids) == sorted(registry.all_ids()) and len(ids) == len(set(ids)),
          f"{len(ids)} returned vs {len(registry.all_ids())} in registry")
    check("agents and workflows both present",
          body["counts"]["agents"] > 0 and body["counts"]["workflows"] > 0, str(body["counts"]))

    legacy = client.get("/api/v1/agents", headers=admin)
    legacy_names = {a["name"] for a in legacy.json()["agents"]}
    check("legacy /api/v1/agents names all resolve in the registry",
          all(registry.exists(n) for n in legacy_names),
          str(sorted(n for n in legacy_names if not registry.exists(n))))

    from backend.services.api_key_service import VALID_AGENT_NAMES
    from backend.routes.agents import VALID_AGENTS
    check("no agent-name list disagrees with the registry",
          all(registry.exists(n) for n in VALID_AGENT_NAMES)
          and all(registry.exists(n) for n in VALID_AGENTS))

    # Each product declares the reviews.review_type its code actually writes. Catching a
    # drift here is the difference between a product's history being findable and not.
    import re
    written = set()
    for path in [ROOT / "backend" / "services" / "orchestrator.py",
                 ROOT / "backend" / "services" / "swarm_service.py",
                 *(ROOT / "backend" / "workflows").glob("*.py")]:
        written |= set(re.findall(r'review_type="([a-z0-9_]+)"', path.read_text(encoding="utf-8")))
    declared = {p.review_type for p in registry.PRODUCTS if p.review_type}
    check("every declared review_type is one the code really writes",
          declared <= written, f"orphaned: {sorted(declared - written)}")

    every_invocable = all(p.invoke is not None for p in registry.PRODUCTS if p.id != "swarm")
    check("every product except swarm has an invoke adapter", every_invocable)

    # ── 2. back-compat: an existing key keeps working ──────────────────────
    section("2. Back-compat (key with no plan, scopes='all')")
    r = client.post("/api/v1/agents/code", json={"code": "x = 1"}, headers=hdr(open_key))
    check("unplanned key can call an agent", r.status_code == 200, r.text[:300])
    r = client.post("/api/v1/agents/requirement", json={"text": "As a user..."}, headers=hdr(open_key))
    check("unplanned key can call a second agent", r.status_code == 200, r.text[:300])

    # ── 3. entitlement enforcement ─────────────────────────────────────────
    section("3. Entitlements")
    plan = client.post("/api/v1/plans", json={
        "name": "Code Only", "products": [{"product_id": "code"}]}, headers=admin)
    check("plan created", plan.status_code in (200, 201), plan.text[:300])
    code_plan_id = plan.json()["id"]

    bad = client.post("/api/v1/plans", json={
        "name": "Bogus", "products": [{"product_id": "not-a-real-product"}]}, headers=admin)
    check("plan with an unknown product id is rejected", bad.status_code == 400, bad.text[:200])

    client.patch(f"/api/v1/keys/{scoped_key_id}/plan", json={"plan_id": code_plan_id}, headers=admin)
    r = client.post("/api/v1/agents/code", json={"code": "x = 1"}, headers=hdr(scoped_key))
    check("entitled agent allowed (200)", r.status_code == 200, r.text[:200])
    r = client.post("/api/v1/agents/requirement", json={"text": "hi"}, headers=hdr(scoped_key))
    check("non-entitled agent denied (403)", r.status_code == 403, f"{r.status_code} {r.text[:200]}")

    # ── 4. workflow entitlement doesn't leak into agent access ─────────────
    section("4. Workflow entitlement is its own product")
    wf_plan = client.post("/api/v1/plans", json={
        "name": "Full Review Only", "products": [{"product_id": "full"}]}, headers=admin)
    client.patch(f"/api/v1/keys/{wf_key_id}/plan",
                 json={"plan_id": wf_plan.json()["id"]}, headers=admin)
    r = client.post("/api/v1/agents/full", json={"text": "review this"}, headers=hdr(wf_key))
    check("entitled workflow accepted (202)", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    job_id = r.json().get("job_id")
    r = client.post("/api/v1/agents/code", json={"code": "x = 1"}, headers=hdr(wf_key))
    check("workflow entitlement does not grant its sub-agents (403)",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")

    # ── 5. metering: one event per workflow, not per sub-agent ─────────────
    section("5. Metering")
    import time
    for _ in range(50):                       # the /full job runs in a background thread
        job = client.get(f"/api/v1/agents/jobs/{job_id}", headers=hdr(wf_key)).json()
        if job.get("status") in ("completed", "failed"):
            break
        time.sleep(0.2)
    check("full_review job completed", job.get("status") == "completed", str(job.get("error"))[:300])

    events = _usage_events()
    full_events = [e for e in events if e["product_id"] == "full"]
    check("exactly one usage_event for the whole workflow",
          len(full_events) == 1, f"got {len(full_events)}")
    if full_events:
        ev = full_events[0]
        check("workflow event aggregates all its nested LLM calls",
              ev["calls"] > 1 and ev["input_tokens"] == ev["calls"] * STUB_INPUT_TOKENS,
              f"calls={ev['calls']} input_tokens={ev['input_tokens']}")
        check("workflow event is tagged kind=workflow", ev["product_kind"] == "workflow",
              ev["product_kind"])
        check("workflow event records a non-zero cost", ev["cost_usd"] > 0, str(ev["cost_usd"]))

    agent_events = [e for e in events if e["product_id"] == "code"]
    check("agent calls are metered too", len(agent_events) >= 2, f"got {len(agent_events)}")
    check("agent events tagged kind=agent",
          all(e["product_kind"] == "agent" for e in agent_events))

    # ── 6. quota ───────────────────────────────────────────────────────────
    section("6. Quotas")
    q_plan = client.post("/api/v1/plans", json={
        "name": "One Call", "products": [{"product_id": "code", "quota_calls_per_month": 1}],
    }, headers=admin)
    client.patch(f"/api/v1/keys/{quota_key_id}/plan",
                 json={"plan_id": q_plan.json()["id"]}, headers=admin)
    first = client.post("/api/v1/agents/code", json={"code": "x = 1"}, headers=hdr(quota_key))
    second = client.post("/api/v1/agents/code", json={"code": "y = 2"}, headers=hdr(quota_key))
    check("first call within quota (200)", first.status_code == 200, first.text[:200])
    check("second call over quota (429)", second.status_code == 429,
          f"{second.status_code} {second.text[:200]}")
    if second.status_code == 429:
        check("429 body identifies the product and limit",
              second.json().get("error_type") == "quota_exceeded"
              and second.json().get("product_id") == "code", second.text[:200])

    # ── 7. budget pre-check happens before the 202 ─────────────────────────
    section("7. Budget timing on the async /full endpoint")
    client.patch(f"/api/v1/keys/{open_key_id}/budget",
                 json={"monthly_budget_usd": 0.000001}, headers=admin)
    r = client.post("/api/v1/agents/full", json={"text": "review"}, headers=hdr(open_key))
    check("over-budget /full returns 402 immediately, not 202-then-failed-job",
          r.status_code == 402, f"{r.status_code} {r.text[:200]}")
    client.patch(f"/api/v1/keys/{open_key_id}/budget",
                 json={"monthly_budget_usd": None}, headers=admin)

    # ── 8. legacy surface is metered but still open ────────────────────────
    section("8. Legacy /api/review/* surface")
    before = len(_usage_events())
    r = client.post("/api/review/code", json={"code": "x = 1", "language": "python"})
    check("unauthenticated legacy route still works (no regression)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    after = _usage_events()
    new_rows = [e for e in after[before:] if e["product_id"] == "code"]
    check("legacy call now writes a usage_event", len(new_rows) == 1, f"got {len(new_rows)}")
    if new_rows:
        check("legacy caller recorded as anonymous with no key",
              new_rows[0]["caller_type"] == "anonymous" and new_rows[0]["key_id"] is None,
              f"{new_rows[0]['caller_type']} / {new_rows[0]['key_id']}")
        check("legacy event has real token counts",
              new_rows[0]["input_tokens"] == STUB_INPUT_TOKENS, str(new_rows[0]["input_tokens"]))

    r = client.post("/api/review/code", json={"code": "x = 1"}, headers=hdr(scoped_key))
    check("legacy route honours entitlements for API-key callers (200 for entitled)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    r = client.post("/api/review/requirement", json={"content": "hi"}, headers=hdr(scoped_key))
    check("legacy route denies a non-entitled API key (403)",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")

    # The other half of the switch: with the flag on, an unauthenticated call is rejected.
    # legacy_guard reads config at call time, so setting the attribute is enough.
    config.LEGACY_ROUTES_REQUIRE_AUTH = True
    try:
        r = client.post("/api/review/code", json={"code": "x = 1"})
        check("LEGACY_ROUTES_REQUIRE_AUTH=true rejects an unauthenticated call (401)",
              r.status_code == 401, f"{r.status_code} {r.text[:200]}")
        r = client.post("/api/review/code", json={"code": "x = 1"}, headers=hdr(scoped_key))
        check("LEGACY_ROUTES_REQUIRE_AUTH=true still admits a valid key (200)",
              r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    finally:
        config.LEGACY_ROUTES_REQUIRE_AUTH = False

    # ── 9. swarm is metered ────────────────────────────────────────────────
    section("9. Swarm metering (the ThreadPoolExecutor contextvar fix)")
    r = client.post("/api/v1/swarm",
                    json={"text": "review this artifact", "project_name": "Smoke"},
                    headers=hdr(open_key))
    check("swarm runs", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    swarm_events = [e for e in _usage_events() if e["product_id"] == "swarm"]
    check("swarm writes exactly one usage_event", len(swarm_events) == 1, f"got {len(swarm_events)}")
    if swarm_events:
        check("swarm event has NON-ZERO tokens (fails without copy_context)",
              swarm_events[0]["input_tokens"] > 0,
              f"input_tokens={swarm_events[0]['input_tokens']} — worker-thread context not propagated")

    stack.close()

    print(f"\n{'-' * 60}")
    if _failures:
        print(f"FAILED: {len(_failures)}/{_checks} checks failed")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"OK: all {_checks} checks passed")
    return 0


def _usage_events():
    conn = database.get_connection()
    rows = conn.execute("SELECT * FROM usage_events ORDER BY created_at, rowid").fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    try:
        code = main_()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_TMP_DB + suffix)
            except OSError:
                pass
    sys.exit(code)
