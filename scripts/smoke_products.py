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
# Pin the governance flags rather than inherit whatever the developer's own .env says,
# then flip each deliberately in-test. Section 8 needs legacy routes open to prove the
# no-regression path still works; section 14 reloads config with the environment cleared
# to assert what a fresh install actually ships with.
os.environ["LEGACY_ROUTES_REQUIRE_AUTH"] = "false"
os.environ["UI_REQUIRE_PROJECT"] = "false"

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
from backend.services import agent_guard, subject_service  # noqa: E402
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

    # `swarm` and `modernization` are the two products whose own service owns dispatch:
    # neither is a single orchestrator call a flat params dict could drive. Everything else
    # must have an adapter, or it is entitleable and meterable but not runnable.
    SERVICE_DISPATCHED = {"swarm", "modernization"}
    every_invocable = all(p.invoke is not None
                          for p in registry.PRODUCTS if p.id not in SERVICE_DISPATCHED)
    check("every product except the service-dispatched ones has an invoke adapter",
          every_invocable,
          f"missing: {sorted(p.id for p in registry.PRODUCTS if p.invoke is None) }")
    check("only swarm and modernization are service-dispatched",
          {p.id for p in registry.PRODUCTS if p.invoke is None} == SERVICE_DISPATCHED)

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

    # ── 10. BYOL spend is recorded but not charged to the platform budget ──
    section("10. BYOL billing separation")
    budget_before = database.get_api_key_budget(open_key_id)["budget_used_usd"]
    r = client.post("/api/v1/agents/code", headers=hdr(open_key), json={
        "code": "x = 1",
        # A caller-supplied credential: their own account pays, not ours.
        "llm_provider": "anthropic", "llm_api_key": "sk-ant-caller-own-key",
    })
    check("BYOL call succeeds", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    byol = [e for e in _usage_events() if e["key_id"] == open_key_id and e["product_id"] == "code"][-1:]
    if byol:
        check("BYOL call is recorded as non-billable", byol[0]["billable"] == 0,
              f"billable={byol[0]['billable']}")
        check("BYOL call still records real cost and tokens for visibility",
              byol[0]["cost_usd"] > 0 and byol[0]["input_tokens"] == STUB_INPUT_TOKENS,
              f"cost={byol[0]['cost_usd']} tokens={byol[0]['input_tokens']}")
        check("BYOL provider is recorded", byol[0]["provider"] == "anthropic",
              str(byol[0]["provider"]))
    budget_after = database.get_api_key_budget(open_key_id)["budget_used_usd"]
    check("BYOL spend does NOT move the platform budget counter",
          abs(budget_after - budget_before) < 1e-9, f"{budget_before} -> {budget_after}")

    # A platform-paid call on the same key must still move it, or the check above
    # would pass simply because budget accrual is broken again.
    client.post("/api/v1/agents/code", json={"code": "y = 2"}, headers=hdr(open_key))
    check("a platform-paid call on the same key DOES move the budget counter",
          database.get_api_key_budget(open_key_id)["budget_used_usd"] > budget_after,
          f"still {budget_after}")

    # Call quotas must still bite for BYOL — otherwise BYOL is unlimited use.
    byol_key, byol_key_id = new_key("byol-quota-key")
    bq_plan = client.post("/api/v1/plans", json={
        "name": "BYOL One Call", "products": [{"product_id": "code", "quota_calls_per_month": 1}],
    }, headers=admin)
    client.patch(f"/api/v1/keys/{byol_key_id}/plan",
                 json={"plan_id": bq_plan.json()["id"]}, headers=admin)
    byol_body = {"code": "x = 1", "llm_provider": "anthropic", "llm_api_key": "sk-ant-caller-own-key"}
    first = client.post("/api/v1/agents/code", json=byol_body, headers=hdr(byol_key))
    second = client.post("/api/v1/agents/code", json=byol_body, headers=hdr(byol_key))
    check("first BYOL call within call quota (200)", first.status_code == 200, first.text[:200])
    check("BYOL call still counts toward a CALL quota (429)", second.status_code == 429,
          f"{second.status_code} {second.text[:200]}")

    # ── 11. usage / metering API ───────────────────────────────────────────
    section("11. Usage & metering API")
    r = client.get("/api/v1/usage/summary", headers=admin)
    check("GET /api/v1/usage/summary responds", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    summary = r.json() if r.status_code == 200 else {"totals": {}, "by_product": [], "by_key": []}
    ledger = _usage_events()
    check("summary totals match the ledger row count",
          summary["totals"].get("events") == len(ledger),
          f"{summary['totals'].get('events')} vs {len(ledger)}")
    check("by_product costs sum to the total",
          abs(sum(p["cost_usd"] for p in summary["by_product"]) - summary["totals"]["cost_usd"]) < 1e-6,
          f"{sum(p['cost_usd'] for p in summary['by_product'])} vs {summary['totals']['cost_usd']}")
    check("by_project costs sum to the total",
          abs(sum(p["cost_usd"] for p in summary["by_project"]) - summary["totals"]["cost_usd"]) < 1e-6,
          f"{sum(p['cost_usd'] for p in summary['by_project'])} vs {summary['totals']['cost_usd']}")
    check("by_project resolves real project names",
          any(p["label"] not in ("Unattributed", "") for p in summary["by_project"]),
          str(summary["by_project"])[:200])
    check("by_key costs sum to the total",
          abs(sum(k["cost_usd"] for k in summary["by_key"]) - summary["totals"]["cost_usd"]) < 1e-6,
          f"{sum(k['cost_usd'] for k in summary['by_key'])} vs {summary['totals']['cost_usd']}")
    check("billable total is below the gross total (BYOL excluded)",
          summary["totals"]["billable_cost_usd"] < summary["totals"]["cost_usd"],
          f"billable={summary['totals']['billable_cost_usd']} gross={summary['totals']['cost_usd']}")
    check("by_key rows carry a readable label, not just a UUID",
          all(k.get("label") for k in summary["by_key"]), str(summary["by_key"])[:200])
    keyed = [e for e in ledger if e["key_id"]]
    check("API-key calls are attributed to the key's project",
          keyed and all(e["project_id"] for e in keyed),
          f"{sum(1 for e in keyed if not e['project_id'])} of {len(keyed)} key-authenticated rows have no project")

    r = client.get("/api/v1/usage/summary?product_id=code", headers=admin)
    check("summary honours the product_id filter",
          r.status_code == 200 and all(p["product_id"] == "code" for p in r.json()["by_product"]),
          r.text[:200])

    r = client.get("/api/v1/usage/events?limit=5", headers=admin)
    check("GET /api/v1/usage/events paginates",
          r.status_code == 200 and len(r.json()["events"]) == 5 and r.json()["total"] == len(ledger),
          f"{r.status_code} {r.text[:200]}")
    check("events are newest first",
          r.status_code == 200 and r.json()["events"][0]["created_at"] >= r.json()["events"][-1]["created_at"],
          "ordering")

    r = client.get("/api/v1/usage/events.csv", headers=admin)
    check("CSV export downloads", r.status_code == 200 and "attachment" in r.headers.get("content-disposition", ""),
          f"{r.status_code} {r.headers.get('content-disposition')}")
    check("CSV has a header row and one line per event",
          r.status_code == 200 and len(r.text.strip().splitlines()) == len(ledger) + 1,
          f"{len(r.text.strip().splitlines())} lines for {len(ledger)} events")

    check("usage API rejects an unauthenticated caller (401)",
          client.get("/api/v1/usage/summary").status_code == 401)
    check("usage API rejects an API key (bearer JWT only)",
          client.get("/api/v1/usage/summary", headers=hdr(open_key)).status_code == 401)

    # ── 12. plan editing, as the admin UI drives it ────────────────────────
    section("12. Plan editing (the paths the Plans screen uses)")
    edit_key, edit_key_id = new_key("plan-edit-key")
    created = client.post("/api/v1/plans", json={
        "name": "Editable", "description": "before",
        "products": [{"product_id": "code"}],
    }, headers=admin)
    plan_id = created.json()["id"]
    client.patch(f"/api/v1/keys/{edit_key_id}/plan", json={"plan_id": plan_id}, headers=admin)

    r = client.patch(f"/api/v1/plans/{plan_id}",
                     json={"name": "Editable", "description": "after"}, headers=admin)
    check("PATCH /plans/{id} updates name and description",
          r.status_code == 200 and r.json()["description"] == "after", r.text[:200])

    r = client.put(f"/api/v1/plans/{plan_id}/products", headers=admin, json={
        "products": [{"product_id": "requirement", "quota_calls_per_month": 5}],
    })
    check("PUT /plans/{id}/products replaces the product list",
          r.status_code == 200 and [p["product_id"] for p in r.json()["products"]] == ["requirement"],
          r.text[:200])
    newly_added = client.post("/api/v1/agents/requirement",
                              json={"text": "As a user I want to log in"}, headers=hdr(edit_key))
    newly_removed = client.post("/api/v1/agents/code", json={"code": "x=1"}, headers=hdr(edit_key))
    check("a product added by the edit becomes callable (200)",
          newly_added.status_code == 200, f"{newly_added.status_code} {newly_added.text[:200]}")
    check("a product removed by the edit becomes denied (403)",
          newly_removed.status_code == 403, f"{newly_removed.status_code} {newly_removed.text[:200]}")

    r = client.patch(f"/api/v1/plans/{plan_id}", json={"is_active": False}, headers=admin)
    check("deactivating a plan drops its entitlements to the scopes fallback",
          r.status_code == 200
          and client.post("/api/v1/agents/code", json={"code": "x=1"}, headers=hdr(edit_key)).status_code == 200,
          "an inactive plan should not keep restricting the key")

    r = client.get(f"/api/v1/keys/{edit_key_id}/entitlements", headers=admin)
    check("GET /keys/{id}/entitlements responds", r.status_code == 200, r.text[:200])

    client.patch(f"/api/v1/plans/{plan_id}", json={"is_active": True}, headers=admin)
    # A key is still on this plan, so the delete is refused until it is confirmed —
    # removing a plan makes its subjects *less* restricted, and that shouldn't happen by
    # accident. Section 15 covers the refusal itself; here we just mean it.
    r = client.delete(f"/api/v1/plans/{plan_id}?force=true", headers=admin)
    check("DELETE /plans/{id} removes the plan", r.status_code == 200, r.text[:200])
    check("a key whose plan was deleted falls back to scopes, not lockout",
          client.post("/api/v1/agents/code", json={"code": "x=1"}, headers=hdr(edit_key)).status_code == 200,
          "deleting a plan locked the key out")

    check("plans API rejects a non-super-admin",
          client.get("/api/v1/plans").status_code == 401)

    # ── 13. one allowance, two doors ───────────────────────────────────────
    # The point of the whole thing: a browser session and an API key working in the same
    # project must hit the same plan, the same budget and the same quota. Everything above
    # this line tested the API-key door only — which is exactly how the UI came to be
    # ungoverned in the first place.
    section("13. Project governance — browser and API key share one allowance")

    gov = client.post("/api/v1/projects", json={"name": "Governed Project"}, headers=admin)
    gov_id = gov.json()["project"]["id"]
    gov_plan = client.post("/api/v1/plans", json={
        "name": "Project Code Only", "products": [{"product_id": "code"}]}, headers=admin)
    gov_plan_id = gov_plan.json()["id"]

    r = client.patch(f"/api/v1/projects/{gov_id}/plan",
                     json={"plan_id": gov_plan_id}, headers=admin)
    check("PATCH /projects/{id}/plan assigns a plan", r.status_code == 200, r.text[:200])
    r = client.patch(f"/api/v1/projects/{gov_id}/plan",
                     json={"plan_id": "not-a-real-plan"}, headers=admin)
    check("assigning an unknown plan is rejected (404)", r.status_code == 404, r.text[:200])
    client.patch(f"/api/v1/projects/{gov_id}/plan", json={"plan_id": gov_plan_id}, headers=admin)

    # A browser call is admin's JWT plus the project header — exactly what apiFetch sends.
    def ui(project_id=None):
        h = dict(admin)
        if project_id:
            h["X-Project-Id"] = project_id
        return h

    # A key belongs to a project from birth — there is no endpoint to move one, by design:
    # a key's project decides whose budget it spends, so changing it would silently
    # re-bill a customer.
    def key_in(project, label):
        resp = client.post("/api/v1/keys",
                           json={"project_id": project, "team_name": label}, headers=admin)
        assert resp.status_code in (200, 201), resp.text
        return resp.json()["key"]["raw_key"], resp.json()["key"]["id"]

    # -- entitlement, the thing that was previously skipped entirely for logins
    r = client.post("/api/review/code", json={"code": "x = 1"}, headers=ui(gov_id))
    check("browser call to an entitled product is allowed (200)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    r = client.post("/api/review/requirement", json={"content": "hi"}, headers=ui(gov_id))
    check("browser call to a NON-entitled product is denied (403)",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")
    check("the denial names the project's plan, not just 'API key'",
          "Project Code Only" in r.text, r.text[:300])

    r = client.post("/api/v1/agents/requirement", json={"text": "hi"}, headers=ui(gov_id))
    check("the v1 surface enforces it for a browser caller too (403)",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")

    # -- no regression: an unbound login is exactly as unrestricted as it was before
    r = client.post("/api/review/requirement", json={"content": "hi"}, headers=ui())
    check("a login with no project selected stays unrestricted (no regression)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    # -- attribution: the ledger row must carry the project, or no budget can ever move
    ui_rows = [e for e in _usage_events()
               if e["caller_type"] == "ui" and e["project_id"] == gov_id]
    check("browser calls are attributed to the selected project",
          len(ui_rows) >= 1, "no ui-attributed usage_events rows for the project")

    # -- ONE budget: spend through both doors draws down the same counter
    gov_key, gov_key_id = key_in(gov_id, "governed-project-key")
    before_project = database.get_project_budget(gov_id)["budget_used_usd"]
    client.post("/api/review/code", json={"code": "y = 2"}, headers=ui(gov_id))
    after_ui = database.get_project_budget(gov_id)["budget_used_usd"]
    check("a browser call moves the project budget counter",
          after_ui > before_project, f"{before_project} -> {after_ui}")
    client.post("/api/v1/agents/code", json={"code": "z = 3"}, headers=hdr(gov_key))
    after_key = database.get_project_budget(gov_id)["budget_used_usd"]
    check("an API-key call moves the SAME project budget counter",
          after_key > after_ui, f"{after_ui} -> {after_key}")

    # -- and the cap stops both doors
    r = client.patch(f"/api/v1/projects/{gov_id}/budget",
                     json={"monthly_budget_usd": 0.001}, headers=admin)
    check("PATCH /projects/{id}/budget sets a cap", r.status_code == 200, r.text[:200])
    r = client.post("/api/review/code", json={"code": "x = 1"}, headers=ui(gov_id))
    check("an exhausted project budget stops a browser call (402)",
          r.status_code == 402, f"{r.status_code} {r.text[:200]}")
    r = client.post("/api/v1/agents/code", json={"code": "x = 1"}, headers=hdr(gov_key))
    check("the same exhausted budget stops an API-key call (402)",
          r.status_code == 402, f"{r.status_code} {r.text[:200]}")
    r = client.post("/api/review/code", json={"code": "x = 1"}, headers=hdr(open_key))
    check("a key in a DIFFERENT project is unaffected by that cap (200)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    client.patch(f"/api/v1/projects/{gov_id}/budget",
                 json={"monthly_budget_usd": None}, headers=admin)

    # -- ONE quota, counted across both doors
    quota_project = client.post("/api/v1/projects", json={"name": "Quota Project"}, headers=admin)
    qp_id = quota_project.json()["project"]["id"]
    qp_plan = client.post("/api/v1/plans", json={
        "name": "Project Two Calls",
        "products": [{"product_id": "code", "quota_calls_per_month": 2}]}, headers=admin)
    client.patch(f"/api/v1/projects/{qp_id}/plan",
                 json={"plan_id": qp_plan.json()["id"]}, headers=admin)
    qp_key, _ = key_in(qp_id, "quota-project-key")

    first = client.post("/api/review/code", json={"code": "a = 1"}, headers=ui(qp_id))
    second = client.post("/api/v1/agents/code", json={"code": "b = 2"}, headers=hdr(qp_key))
    third = client.post("/api/review/code", json={"code": "c = 3"}, headers=ui(qp_id))
    check("browser call 1 of 2 allowed", first.status_code == 200, first.text[:200])
    check("API-key call 2 of 2 allowed — the quota is shared, not per-door",
          second.status_code == 200, f"{second.status_code} {second.text[:200]}")
    check("the third call is refused whichever door it came from (429)",
          third.status_code == 429, f"{third.status_code} {third.text[:200]}")

    # -- a user may not bind to a project they don't belong to
    client.post("/api/auth/users", headers=admin, json={
        "username": "smokedev", "email": "smokedev@example.com",
        "password": "smoke-dev-pass-123", "role": "developer"})
    dev_login = client.post("/api/auth/login",
                            json={"username": "smokedev", "password": "smoke-dev-pass-123"})
    check("non-admin user can log in", dev_login.status_code == 200, dev_login.text[:200])
    dev = {"Authorization": f"Bearer {dev_login.json()['token']}"}

    r = client.post("/api/review/code", json={"code": "x = 1"},
                    headers={**dev, "X-Project-Id": gov_id})
    check("a user who is not on a project cannot bind to it (403)",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")
    r = client.post("/api/review/code", json={"code": "x = 1"},
                    headers={**dev, "X-Project-Id": "no-such-project"})
    check("binding to a project that doesn't exist is refused (403)",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")

    client.post(f"/api/v1/projects/{gov_id}/admins", json={"username": "smokedev"}, headers=admin)
    r = client.post("/api/review/code", json={"code": "x = 1"},
                    headers={**dev, "X-Project-Id": gov_id})
    check("once assigned to the project, the same user is admitted (200)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("a non-super-admin sees only their own projects in the picker",
          [p["id"] for p in client.get("/api/v1/projects", headers=dev).json()["projects"]] == [gov_id],
          client.get("/api/v1/projects", headers=dev).text[:200])

    # -- GET /products drives the UI's locked-agent markers, so it must agree with the guard
    r = client.get("/api/v1/products", headers=ui(gov_id))
    entitled = {p["id"] for p in r.json()["products"] if p["entitled"]}
    check("/products reflects the project's plan for a browser caller",
          entitled == {"code"}, str(sorted(entitled))[:200])
    check("/products reports the project's budget for the sidebar",
          (r.json().get("project") or {}).get("id") == gov_id, r.text[:200])
    unbound = client.get("/api/v1/products", headers=ui())
    check("/products shows everything when no project is selected",
          unbound.json()["counts"]["entitled"] == unbound.json()["counts"]["total"],
          str(unbound.json()["counts"]))

    # -- the key's own plan still applies as a ceiling inside the project envelope
    key_plan = client.post("/api/v1/plans", json={
        "name": "Key Requirement Only", "products": [{"product_id": "requirement"}]}, headers=admin)
    client.patch(f"/api/v1/keys/{gov_key_id}/plan",
                 json={"plan_id": key_plan.json()["id"]}, headers=admin)
    r = client.post("/api/v1/agents/code", json={"code": "x = 1"}, headers=hdr(gov_key))
    check("a key's plan still restricts inside the project's plan (403)",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")
    r = client.post("/api/v1/agents/requirement", json={"text": "hi"}, headers=hdr(gov_key))
    check("layers INTERSECT — the key's plan cannot widen the project's (403)",
          r.status_code == 403, f"{r.status_code} {r.text[:200]}")
    client.patch(f"/api/v1/keys/{gov_key_id}/plan", json={"plan_id": None}, headers=admin)

    # -- per-project rate limit
    client.patch(f"/api/v1/projects/{gov_id}/budget", headers=admin,
                 json={"monthly_budget_usd": None, "rate_limit_per_minute": 2})
    statuses = [client.post("/api/review/code", json={"code": "x = 1"},
                            headers=ui(gov_id)).status_code for _ in range(4)]
    check("a project rate limit throttles browser calls (429)",
          429 in statuses, str(statuses))
    check("the throttle only bites after the limit, not before",
          statuses[0] == 200, str(statuses))
    client.patch(f"/api/v1/projects/{gov_id}/budget", headers=admin,
                 json={"monthly_budget_usd": None, "rate_limit_per_minute": None})

    # -- UI_REQUIRE_PROJECT closes the "just don't pick one" loophole
    config.UI_REQUIRE_PROJECT = True
    try:
        r = client.post("/api/review/code", json={"code": "x = 1"}, headers=ui())
        check("UI_REQUIRE_PROJECT=true refuses an unbound browser call (403)",
              r.status_code == 403, f"{r.status_code} {r.text[:200]}")
        r = client.post("/api/review/code", json={"code": "x = 1"}, headers=ui(gov_id))
        check("UI_REQUIRE_PROJECT=true still admits a bound one (200)",
              r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        r = client.get("/api/v1/products", headers=ui())
        check("UI_REQUIRE_PROJECT doesn't break the screens that pick a project (200)",
              r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    finally:
        config.UI_REQUIRE_PROJECT = False

    # -- the shipped defaults, which is what a fresh install actually gets
    #
    # Read from source rather than from the loaded module: config.py calls load_dotenv(),
    # so a reload picks up whatever this developer's own .env says and would happily
    # report a bypass as closed while it is open on their machine. Comparing the code
    # default against .env.example also catches the two drifting apart, which is the
    # failure mode where a fresh install silently gets a different posture to the docs.
    section("14. Shipped defaults (source, not this machine's .env)")
    config_src = (ROOT / "backend" / "config.py").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    def code_default(name):
        m = re.search(rf'{name}\s*=\s*os\.getenv\(\s*"{name}"\s*,\s*"([^"]*)"', config_src)
        return m.group(1) if m else None

    def example_value(name):
        m = re.search(rf'^{name}=(\S*)\s*$', env_example, re.MULTILINE)
        return m.group(1) if m else None

    check("LEGACY_ROUTES_REQUIRE_AUTH defaults to true in code — no anonymous bypass",
          code_default("LEGACY_ROUTES_REQUIRE_AUTH") == "true",
          f"config.py default is {code_default('LEGACY_ROUTES_REQUIRE_AUTH')!r}")
    check(".env.example agrees with the code default",
          example_value("LEGACY_ROUTES_REQUIRE_AUTH") == "true",
          f".env.example says {example_value('LEGACY_ROUTES_REQUIRE_AUTH')!r}")
    check("UI_REQUIRE_PROJECT defaults to false — opt in, so no one is locked out on upgrade",
          code_default("UI_REQUIRE_PROJECT") == "false",
          f"config.py default is {code_default('UI_REQUIRE_PROJECT')!r}")
    check("UI_REQUIRE_PROJECT is documented in .env.example",
          example_value("UI_REQUIRE_PROJECT") is not None, "missing from .env.example")

    # A live warning, not a check: an override here beats the shipped default, and this
    # is exactly the situation where someone believes the bypass is shut and it isn't.
    if (ROOT / ".env").exists():
        local_env = (ROOT / ".env").read_text(encoding="utf-8")
        if re.search(r'^LEGACY_ROUTES_REQUIRE_AUTH=false', local_env, re.MULTILINE):
            print("  NOTE  your .env sets LEGACY_ROUTES_REQUIRE_AUTH=false, which overrides "
                  "the shipped default — the anonymous bypass is OPEN on this machine")

    # ── 15. Gaps found reviewing the governance wiring ─────────────────────
    #
    # Each of these was a real hole, verified against the code before it was fixed. They
    # share a theme: governance was applied at the points someone remembered, and the
    # points that were forgotten failed *open*.
    section("15. Governance gaps (each of these was reproducible)")

    # -- deleting a plan must not silently un-govern the projects on it
    doomed = client.post("/api/v1/plans", json={
        "name": "Doomed Plan", "products": [{"product_id": "code"}]}, headers=admin).json()["id"]
    victim = client.post("/api/v1/projects", json={"name": "Plan Delete Victim"},
                         headers=admin).json()["project"]["id"]
    client.patch(f"/api/v1/projects/{victim}/plan", json={"plan_id": doomed}, headers=admin)

    r = client.post("/api/review/requirement", json={"content": "hi"}, headers=ui(victim))
    check("baseline: the plan restricts the project (403)", r.status_code == 403, r.text[:200])

    r = client.delete(f"/api/v1/plans/{doomed}", headers=admin)
    check("deleting a plan that is in use is refused (409)", r.status_code == 409, r.text[:200])
    check("the refusal names what is still on it", "Plan Delete Victim" in r.text, r.text[:300])
    check("the plan survives the refusal",
          client.get(f"/api/v1/plans/{doomed}", headers=admin).status_code == 200)

    r = client.delete(f"/api/v1/plans/{doomed}?force=true", headers=admin)
    check("force=true deletes it", r.status_code == 200, r.text[:200])
    row = database.get_project(victim)
    check("the project's plan_id is cleared, not left dangling",
          row["plan_id"] is None, f"plan_id is still {row['plan_id']!r}")
    check("no plan means no plan — get_plan_for_project agrees",
          database.get_plan_for_project(victim) is None)

    # -- the project rate limit must cover every door, including the async workflow
    rl_id = client.post("/api/v1/projects", json={"name": "Rate Limited"},
                        headers=admin).json()["project"]["id"]
    client.patch(f"/api/v1/projects/{rl_id}/budget",
                 json={"rate_limit_per_minute": 2}, headers=admin)

    r1 = client.post("/api/review/code", json={"code": "a = 1"}, headers=ui(rl_id))
    r2 = client.post("/api/review/code", json={"code": "b = 2"}, headers=ui(rl_id))
    r3 = client.post("/api/review/code", json={"code": "c = 3"}, headers=ui(rl_id))
    check("browser calls 1 and 2 are inside the project's 2/min limit",
          r1.status_code == 200 and r2.status_code == 200,
          f"{r1.status_code} {r2.status_code}")
    check("browser call 3 is rate limited (429)", r3.status_code == 429, r3.text[:200])

    # The heaviest product on the platform, and the one the middleware does not cover.
    r = client.post("/api/review/full", json={"code": "d = 4"}, headers=ui(rl_id))
    check("/api/review/full obeys the project rate limit too (429)",
          r.status_code == 429, f"{r.status_code} {r.text[:200]}")

    # The v1 surface had no project limit at all, and none whatsoever for a login.
    r = client.post("/api/v1/agents/code", json={"code": "e = 5"}, headers=ui(rl_id))
    check("the v1 surface applies the project rate limit to a login (429)",
          r.status_code == 429, f"{r.status_code} {r.text[:200]}")
    r = client.get("/api/v1/products", headers=ui(rl_id))
    check("reads are not rate limited — the catalogue still loads (200)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    # -- an exhausted budget cannot be raced past
    race_id = client.post("/api/v1/projects", json={"name": "Race"},
                          headers=admin).json()["project"]["id"]
    client.patch(f"/api/v1/projects/{race_id}/budget",
                 json={"monthly_budget_usd": 100.0}, headers=admin)
    subject = subject_service.Subject(project_id=race_id, project_name="Race")
    with agent_guard.hold_budget_reservation(subject, config.MODEL):
        held = agent_guard._reserved_for(f"project:{race_id}")
        check("an in-flight call reserves against the cap", held > 0, f"reserved={held}")
    check("the reservation is released when the call ends",
          agent_guard._reserved_for(f"project:{race_id}") == 0.0)

    # A cap smaller than one call's worst case: the first caller's reservation alone must
    # be enough to stop a second caller, which is precisely the overshoot that was possible.
    tiny_id = client.post("/api/v1/projects", json={"name": "Tiny Cap"},
                          headers=admin).json()["project"]["id"]
    client.patch(f"/api/v1/projects/{tiny_id}/budget",
                 json={"monthly_budget_usd": 0.01}, headers=admin)
    tiny_subject = subject_service.Subject(project_id=tiny_id, project_name="Tiny Cap")
    r = client.post("/api/review/code", json={"code": "f = 6"}, headers=ui(tiny_id))
    check("a call fits when nothing else is in flight",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    with agent_guard.hold_budget_reservation(tiny_subject, config.MODEL):
        r = client.post("/api/review/code", json={"code": "g = 7"}, headers=ui(tiny_id))
        check("a concurrent call is refused against the reservation (402)",
              r.status_code == 402, f"{r.status_code} {r.text[:200]}")

    # -- reading stored work needs a credential; spend limits never governed reads
    anon = client.post("/api/review/code", json={"code": "h = 8"}, headers=ui(gov_id))
    review_id = anon.json().get("review_id")
    for path in ("/api/dashboard", "/api/reviews", f"/api/reviews/{review_id or 'x'}",
                 "/api/reports/none", "/api/review/full/jobs/none"):
        r = client.get(path)
        check(f"GET {path} refuses an anonymous reader (401)",
              r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    check("a signed-in user can still read the review list",
          client.get("/api/reviews", headers=admin).status_code == 200)
    check("an API key is a valid reader too",
          client.get("/api/reviews", headers=hdr(gov_key)).status_code == 200)
    check("the scaffold download needs a credential as well",
          client.get("/api/codegen/scaffold/none/download").status_code == 401)

    # ── 16. Membership: working in a project vs administering one ──────────
    #
    # There used to be one relationship, `project_admins`, so the only way to let a
    # developer run agents against a project was to make them an admin of it — which also
    # handed them its API keys. Deployments that declined ended up with developers running
    # against no project, and therefore under no plan, budget or quota.
    section("16. Project membership (may work in ≠ administers)")

    mem_id = client.post("/api/v1/projects", json={"name": "Membership Project"},
                         headers=admin).json()["project"]["id"]
    client.post("/api/v1/projects", json={"name": "Other People's Project"}, headers=admin)

    client.post("/api/auth/users", headers=admin, json={
        "username": "memberdev", "email": "memberdev@example.com",
        "password": "member-dev-pass-123", "role": "developer"})
    mem_login = client.post("/api/auth/login",
                            json={"username": "memberdev", "password": "member-dev-pass-123"})
    member = {"Authorization": f"Bearer {mem_login.json()['token']}"}

    def as_member(project_id=None):
        h = dict(member)
        if project_id:
            h["X-Project-Id"] = project_id
        return h

    r = client.get("/api/v1/projects", headers=member)
    check("a developer with no membership sees an empty picker",
          r.json()["projects"] == [], r.text[:200])
    r = client.post("/api/review/code", json={"code": "m = 1"}, headers=as_member(mem_id))
    check("...and cannot bind to a project (403)", r.status_code == 403, r.text[:200])

    r = client.post(f"/api/v1/projects/{mem_id}/members",
                    json={"username": "memberdev"}, headers=admin)
    check("a member can be added without making them an admin",
          r.status_code == 200, r.text[:200])
    r = client.post(f"/api/v1/projects/{mem_id}/members",
                    json={"username": "nobody-here"}, headers=admin)
    check("adding a user who doesn't exist is refused (404)", r.status_code == 404, r.text[:200])

    names = [p["name"] for p in client.get("/api/v1/projects", headers=member).json()["projects"]]
    check("the project now appears in their picker", names == ["Membership Project"], str(names))
    r = client.post("/api/review/code", json={"code": "m = 2"}, headers=as_member(mem_id))
    check("a member can work in the project (200)", r.status_code == 200, r.text[:200])

    # The whole reason for splitting the two rights.
    keys = client.get("/api/v1/keys", headers=member)
    check("a member is NOT an admin — they see no API keys",
          keys.status_code != 200 or keys.json().get("keys") == [],
          f"{keys.status_code} {keys.text[:200]}")
    r = client.patch(f"/api/v1/projects/{mem_id}/budget",
                     json={"monthly_budget_usd": 999.0}, headers=member)
    check("a member cannot change the project's limits (403)", r.status_code == 403, r.text[:200])
    r = client.post(f"/api/v1/projects/{mem_id}/members",
                    json={"username": "smokedev"}, headers=member)
    check("a member cannot add other members (403)", r.status_code == 403, r.text[:200])

    # A project admin runs their own project's membership — that is the point.
    client.post("/api/auth/users", headers=admin, json={
        "username": "projadmin", "email": "projadmin@example.com",
        "password": "proj-admin-pass-123", "role": "developer"})
    client.post(f"/api/v1/projects/{mem_id}/admins", json={"username": "projadmin"}, headers=admin)
    pa_login = client.post("/api/auth/login",
                           json={"username": "projadmin", "password": "proj-admin-pass-123"})
    proj_admin = {"Authorization": f"Bearer {pa_login.json()['token']}"}

    r = client.post(f"/api/v1/projects/{mem_id}/members",
                    json={"username": "smokedev"}, headers=proj_admin)
    check("a project admin can add a member to their own project",
          r.status_code == 200, r.text[:200])
    other_id = [p["id"] for p in client.get("/api/v1/projects", headers=admin).json()["projects"]
                if p["name"] == "Other People's Project"][0]
    r = client.post(f"/api/v1/projects/{other_id}/members",
                    json={"username": "smokedev"}, headers=proj_admin)
    check("...but not to a project they don't administer (403)",
          r.status_code == 403, r.text[:200])

    members = client.get(f"/api/v1/projects/{mem_id}/members", headers=admin).json()["members"]
    by_name = {m["username"]: m for m in members}
    check("an admin appears in the member list, flagged as one",
          by_name.get("projadmin", {}).get("is_admin") is True, str(list(by_name)))
    check("a plain member is not flagged as an admin",
          by_name.get("memberdev", {}).get("is_admin") is False, str(list(by_name)))

    r = client.delete(f"/api/v1/projects/{mem_id}/members/{by_name['projadmin']['user_id']}",
                      headers=admin)
    check("an admin's implicit membership can't be dropped here (404)",
          r.status_code == 404, r.text[:200])
    check("...and the refusal says what to do instead",
          "admin assignment" in r.text, r.text[:300])

    r = client.delete(f"/api/v1/projects/{mem_id}/members/{by_name['memberdev']['user_id']}",
                      headers=admin)
    check("removing a member succeeds", r.status_code == 200, r.text[:200])
    r = client.post("/api/review/code", json={"code": "m = 3"}, headers=as_member(mem_id))
    check("a removed member can no longer bind to the project (403)",
          r.status_code == 403, r.text[:200])

    # ── 17. Reviews belong to a project ────────────────────────────────────
    #
    # Spend was unified per project while the work product was not: History and the
    # Dashboard showed every customer's reviews to everyone, and "what did Acme produce"
    # could not be answered at all.
    section("17. Review attribution and scoping")

    client.post(f"/api/v1/projects/{mem_id}/members", json={"username": "memberdev"}, headers=admin)
    r = client.post("/api/review/code", json={"code": "attributed = 1"}, headers=as_member(mem_id))
    attributed_id = r.json().get("review_id")
    stored = database.get_review(attributed_id)
    check("a review carries the project it was run under",
          stored["project_id"] == mem_id, f"project_id={stored['project_id']!r}")

    r = client.post("/api/review/code", json={"code": "unattributed = 1"}, headers=admin)
    unattributed_id = r.json().get("review_id")
    check("a call with no project selected records no project",
          database.get_review(unattributed_id)["project_id"] is None)

    # The v1 surface writes through a different path (guarded_call, not the middleware).
    v1_review = client.post("/api/v1/agents/code", json={"code": "v1 = 1"},
                            headers=as_member(mem_id))
    v1_id = v1_review.json().get("review_id")
    check("the v1 surface attributes reviews too",
          v1_id and database.get_review(v1_id)["project_id"] == mem_id,
          f"project_id={database.get_review(v1_id)['project_id'] if v1_id else None!r}")

    seen = client.get("/api/reviews?limit=100", headers=member).json()["reviews"]
    check("a member sees only their own project's reviews",
          seen and all(x["project_id"] == mem_id for x in seen),
          str(sorted({x["project_id"] for x in seen})))
    check("unattributed reviews are hidden from them",
          all(x["id"] != unattributed_id for x in seen))

    all_seen = client.get("/api/reviews?limit=200", headers=admin).json()["reviews"]
    check("a super admin still sees everything, unattributed rows included",
          any(x["id"] == unattributed_id for x in all_seen))

    # Selecting a project narrows the screens to it — "Working in: Acme" should mean
    # History is showing Acme.
    scoped = client.get("/api/reviews?limit=100", headers=ui(gov_id)).json()["reviews"]
    check("a selected project narrows History for a super admin as well",
          scoped and all(x["project_id"] == gov_id for x in scoped),
          str({str(x["project_id"]) for x in scoped}))

    r = client.get(f"/api/reviews/{unattributed_id}", headers=member)
    check("reading another project's review by id is a 404, not a 403",
          r.status_code == 404, f"{r.status_code} {r.text[:150]}")
    r = client.get(f"/api/reviews/{attributed_id}", headers=member)
    check("...while their own review reads fine", r.status_code == 200, r.text[:150])

    stats = client.get("/api/dashboard", headers=member).json()
    check("the dashboard total is scoped to what they may see",
          stats["total_reviews"] == len(seen), f"{stats['total_reviews']} vs {len(seen)}")
    admin_stats = client.get("/api/dashboard", headers=admin).json()
    check("and is larger for a super admin",
          admin_stats["total_reviews"] > stats["total_reviews"],
          f"{admin_stats['total_reviews']} vs {stats['total_reviews']}")

    # A genuinely unattached user — not smokedev, who is an admin of another project by
    # this point and therefore a member of it.
    client.post("/api/auth/users", headers=admin, json={
        "username": "orphandev", "email": "orphandev@example.com",
        "password": "orphan-dev-pass-123", "role": "developer"})
    orphan_login = client.post("/api/auth/login",
                               json={"username": "orphandev", "password": "orphan-dev-pass-123"})
    orphan = {"Authorization": f"Bearer {orphan_login.json()['token']}"}
    check("a user who belongs to no project sees no reviews at all",
          client.get("/api/reviews", headers=orphan).json()["reviews"] == [],
          client.get("/api/reviews", headers=orphan).text[:200])
    check("...and no dashboard figures either",
          client.get("/api/dashboard", headers=orphan).json()["total_reviews"] == 0)

    # An API key reads its own project's work and nothing else.
    key_reviews = client.get("/api/reviews?limit=100", headers=hdr(gov_key)).json()["reviews"]
    check("an API key sees only its own project's reviews",
          all(x["project_id"] == gov_id for x in key_reviews),
          str(sorted({x["project_id"] for x in key_reviews})))

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
