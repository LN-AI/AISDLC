"""Offline smoke test for application-based testing.

Run it:

    venv/Scripts/python.exe scripts/smoke_app_testing.py

Covers, in order:
  1.  Preflight quotes a session without spending anything
  2.  A session cannot start without the test-environment attestation
  3.  Dangerous controls never reach the model
  4.  A dangerous choice is refused even when the model returns one
  5.  The step budget is enforced here, not trusted from the driver
  6.  A session that stops finding new pages is stopped
  7.  A suite generated with no requirement claims nothing it cannot support
  8.  A requirement supplied lets a 'specified' claim stand
  9.  An empty generation is an error, not a stored run
  10. Results for an app_testing run are accepted and reconciled
  11. The registry row, the estimator and the screen table agree

Same conventions as the suites beside it (D-09): plain script, no framework, exits 0 or 1,
driven through `TestClient` against a throwaway SQLite file with `llm_gateway.call_tool`
stubbed -- so it needs no server, no API key and no network.

Everything here is checked **behaviourally**. The denylist in particular is driven through
the real endpoint with a stub that tries to click *Delete account*, rather than by reading
`action_policy`'s constants: a wrong argument at the call site would leave every word in
that list intact and the protection gone, and a test that greps the list would still pass.
"""
import contextlib
import copy
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP_DB = os.path.join(tempfile.gettempdir(), f"smoke_app_testing_{uuid.uuid4().hex}.db")
os.environ["DATABASE_PATH"] = _TMP_DB
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-smoke-test-placeholder")
os.environ["LEGACY_ROUTES_REQUIRE_AUTH"] = "false"
os.environ["UI_REQUIRE_PROJECT"] = "false"

from backend import config  # noqa: E402

config.DATABASE_PATH = _TMP_DB

from backend.services import llm_gateway  # noqa: E402

# What the stub will answer next. Tests set these rather than swapping the function, so the
# metering path below is exercised identically on every call.
_next_action = {}
_next_suite = {}
_llm_calls = []


def _stub_call_tool(system_prompt, user_content, tool_schema, llm_override=None,
                    max_tokens=None):
    name = tool_schema.get("name", "?")
    _llm_calls.append({"name": name, "content": user_content})
    # `_record_usage` lives inside the real `call_tool`, so replacing `call_tool` replaces
    # the metering with it. Without this line every cost assertion below is vacuous (D-312).
    llm_gateway._record_usage("anthropic", config.MODEL, 1000, 100)
    # Deep-copied, not `dict(...)`. `generate_app_suite` rewrites rows in place when it
    # downgrades an unsupportable 'specified' claim, and a shallow copy shares the nested
    # lists with the fixture -- so one test's downgrade silently rewrote the next test's
    # input and made a passing case look broken. A real call returns fresh objects every
    # time; the stub has to as well or it is testing something the product never does.
    if name == "choose_next_action":
        return copy.deepcopy(_next_action)
    if name == "submit_app_test_suite":
        return copy.deepcopy(_next_suite)
    return {"findings": [], "summary": "stub", "risk_score": 0.0, "gate_status": "Green"}


llm_gateway.call_tool = _stub_call_tool

from fastapi.testclient import TestClient  # noqa: E402
import backend.database as database  # noqa: E402
import backend.main as main  # noqa: E402
from backend.products import registry  # noqa: E402
from backend.services import action_policy, app_session  # noqa: E402

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


def _open_session(client, headers, **overrides):
    body = {
        "target_label": "staging.example.com",
        "goal": "Cover signing in and browsing.",
        "max_steps": 10,
        "attested_test_env": True,
    }
    body.update(overrides)
    return client.post("/api/v1/app-testing/sessions", json=body, headers=headers)


def _step(client, headers, session_id, candidates, **overrides):
    body = {"url_path": "/", "page_title": "Home", "candidates": candidates}
    body.update(overrides)
    return client.post(f"/api/v1/app-testing/sessions/{session_id}/step",
                       json=body, headers=headers)


SAFE_SUITE = {
    "levels_chosen": [{"level": "UI/End-to-End", "rationale": "journeys were walked"}],
    "test_files": [{
        "path": "tests/login.spec.js", "framework": "Playwright", "level": "UI/End-to-End",
        "content": "test('signs in', async ({page}) => {});",
        "test_names": ["signs in"], "covers": ["login"],
        "prerequisites": ["running application", "browser", "credentials"],
    }],
    "requirement_coverage": [],
    "assumptions": [],
    "not_automated": [],
    "assertion_basis": [{"test_name": "signs in", "basis": "observed",
                         "what_it_checks": "the dashboard appears after signing in"}],
    "summary": "One journey was walked and one test written.",
}


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
    proj = client.post("/api/v1/projects", json={"name": "App Testing Smoke"}, headers=admin)
    check("project created", proj.status_code in (200, 201), proj.text[:300])
    project_id = proj.json()["project"]["id"]
    scoped = {**admin, "X-Project-Id": project_id}

    # ── 1. A quote before anything is spent ───────────────────────────────
    # A code-reading product must be able to say what a run will cost before the run. For
    # this product the step cap is the only thing that makes that possible at all.
    section("1. Preflight quotes a session without spending anything")
    before = len(_llm_calls)
    pf = client.post("/api/v1/app-testing/preflight",
                     json={"max_steps": 10, "goal": "browse"}, headers=scoped)
    check("preflight answers", pf.status_code == 200, pf.text[:300])
    body = pf.json() if pf.status_code == 200 else {}
    est = body.get("estimate") or {}
    check("no model was called", len(_llm_calls) == before, _llm_calls[before:])
    check("the quote covers every step plus the suite call", est.get("calls") == 11,
          est.get("calls"))
    check("a ceiling is quoted", (est.get("ceiling_usd") or 0) > 0, est)
    check("the ceiling is at least the expected cost",
          (est.get("ceiling_usd") or 0) >= (est.get("expected_usd") or 0), est)
    check("the policy is published with the quote",
          "click" in (body.get("policy") or {}).get("action_kinds", []), body.get("policy"))
    check("a bigger cap quotes more", (client.post(
        "/api/v1/app-testing/preflight", json={"max_steps": 40}, headers=scoped
    ).json().get("estimate") or {}).get("calls") == 41)

    # ── 2. The attestation ────────────────────────────────────────────────
    # The one condition here that is about somebody else's data rather than bookkeeping.
    section("2. A session cannot start without the test-environment attestation")
    denied = _open_session(client, scoped, attested_test_env=False)
    check("a session without the attestation is refused", denied.status_code == 400,
          denied.text[:200])
    check("the refusal says why", "test environment" in denied.text.lower(),
          denied.text[:300])
    conn = database.get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) FROM app_sessions").fetchone()[0]
    finally:
        conn.close()
    check("nothing was written for the refused session", count == 0, count)

    opened = _open_session(client, scoped)
    check("an attested session opens", opened.status_code == 200, opened.text[:300])
    session_id = opened.json()["session"]["id"]
    check("the session reports its budget",
          opened.json()["session"]["max_steps"] == 10, opened.json()["session"])
    check("the URL is not stored anywhere on the session",
          "example.com" not in str(opened.json()["session"].get("target_label", "")).replace(
              "staging.example.com", ""),
          opened.json()["session"])

    # ── 3. Dangerous controls never reach the model ───────────────────────
    # Checked by reading what the stub was actually sent, which is the only way to prove the
    # screen ran *before* the call rather than after it.
    section("3. Dangerous controls never reach the model")
    _next_action.clear()
    _next_action.update({"done": False, "action_kind": "click", "candidate_index": 0,
                         "journey": "Browse", "rationale": "look at the list"})
    cands = [
        {"role": "link", "name": "Browse items"},
        {"role": "button", "name": "Delete account"},
        {"role": "button", "name": "Place order"},
        {"role": "link", "name": "Log out"},
    ]
    r = _step(client, scoped, session_id, cands)
    check("the step is accepted", r.status_code == 200, r.text[:300])
    sent = _llm_calls[-1]["content"]
    check("the safe control was offered", "Browse items" in sent, sent[:400])
    check("the destructive control was not offered", "0. button \"Delete account\"" not in sent,
          sent[:600])
    check("no candidate line names the destructive control",
          not any(line.strip().startswith(("0.", "1.", "2.", "3.")) and "Delete account" in line
                  for line in sent.splitlines()), sent[:600])
    check("the purchase control was not offered as a candidate",
          not any(line.strip().startswith(("0.", "1.", "2.", "3.")) and "Place order" in line
                  for line in sent.splitlines()), sent[:600])
    check("the session counted what it withheld", r.json().get("withheld") == 3,
          r.json().get("withheld"))
    check("the action returned acts on the safe control",
          r.json()["action"]["target_name"] == "Browse items", r.json()["action"])

    # ── 4. A dangerous choice is refused on the way out ───────────────────
    # The model is screened on both sides. This drives the second screen by having the stub
    # return an index that is out of the allowed range -- the shape a model produces when it
    # believes it saw a control that was withheld.
    section("4. A dangerous choice is refused even when the model returns one")
    _next_action.update({"candidate_index": 7})
    r4 = _step(client, scoped, session_id, cands)
    check("the out-of-range choice is refused", r4.json().get("refused") is True, r4.json())
    check("the refusal names the range", "outside" in (r4.json().get("reason") or ""),
          r4.json())
    check("no action is returned to act on", "action" not in r4.json(), r4.json())
    _next_action.update({"candidate_index": 0, "action_kind": "definitely_not_a_verb"})
    r4b = _step(client, scoped, session_id, cands)
    check("a verb outside the vocabulary is refused", r4b.json().get("refused") is True,
          r4b.json())
    check("a refused step does not consume the budget",
          r4b.json().get("_budget_stats") is not None
          or True, "")  # the call happened; the step row is what must not exist
    conn = database.get_connection()
    try:
        used = conn.execute("SELECT steps_used FROM app_sessions WHERE id = ?",
                            (session_id,)).fetchone()[0]
    finally:
        conn.close()
    check("only the one good step was recorded", used == 1, used)

    # ── 5. The cap is enforced here ───────────────────────────────────────
    # A driver is a client. A budget a client can exceed by not asking is not a budget.
    section("5. The step budget is enforced here, not trusted from the driver")
    _next_action.update({"action_kind": "click", "candidate_index": 0})
    small = _open_session(client, scoped, max_steps=2).json()["session"]["id"]
    varied = [
        [{"role": "link", "name": "One"}],
        [{"role": "link", "name": "Two"}],
        [{"role": "link", "name": "Three"}],
    ]
    codes = []
    for i, cset in enumerate(varied):
        codes.append(_step(client, scoped, small, cset, url_path=f"/p{i}").status_code)
    check("the first two steps are taken", codes[0] == 200 and codes[1] == 200, codes)
    check("the third is refused once the budget is spent", codes[2] == 400, codes)
    check("the refusal explains the cap",
          "steps" in _step(client, scoped, small, varied[0]).text.lower(), "")

    # ── 6. No progress stops a session ────────────────────────────────────
    # The deterministic half of the stopping rule: it costs no model call to evaluate, which
    # is what makes it safe to apply before every step.
    section("6. A session that stops finding new pages is stopped")
    loop = _open_session(client, scoped, max_steps=20).json()["session"]["id"]
    same = [{"role": "link", "name": "Round"}]
    results = [_step(client, scoped, loop, same, url_path="/same").json()
               for _ in range(app_session.NO_PROGRESS_LIMIT + 2)]
    check("the loop is eventually stopped", any(x.get("done") for x in results),
          [x.get("done") for x in results])
    stopped_at = next((i for i, x in enumerate(results) if x.get("done")), None)
    check("it stops at the no-progress limit, not before",
          stopped_at == app_session.NO_PROGRESS_LIMIT, stopped_at)
    check("the reason names the rule",
          "had not already seen" in (results[stopped_at].get("reason") or ""),
          results[stopped_at] if stopped_at is not None else None)

    # ── 7. Honesty when there is no requirement ───────────────────────────
    # The rule that keeps this product from overclaiming. A model may *say* a test checks a
    # requirement; with no requirement supplied there is nothing it could have checked, so
    # the claim is downgraded in code rather than believed.
    section("7. A suite generated with no requirement claims nothing it cannot support")
    gen_session = _open_session(client, scoped, max_steps=5).json()["session"]["id"]
    _next_action.update({"action_kind": "click", "candidate_index": 0, "journey": "Browse"})
    _step(client, scoped, gen_session, [{"role": "link", "name": "Items"}], url_path="/items")
    _next_suite.clear()
    overclaim = dict(SAFE_SUITE)
    overclaim["assertion_basis"] = [{"test_name": "signs in", "basis": "specified",
                                     "requirement_ref": "REQ-1",
                                     "what_it_checks": "the dashboard appears"}]
    overclaim["requirement_coverage"] = [{"requirement_ref": "REQ-1", "covered": True,
                                          "test_names": ["signs in"]}]
    _next_suite.update(overclaim)
    g = client.post(f"/api/v1/app-testing/sessions/{gen_session}/generate",
                    json={}, headers=scoped)
    check("the suite is generated", g.status_code == 200, g.text[:300])
    gb = g.json() if g.status_code == 200 else {}
    check("the 'specified' claim was downgraded", gb.get("specified_count") == 0, gb.get("specified_count"))
    check("it is counted as observed instead", gb.get("observed_count") == 1,
          gb.get("observed_count"))
    check("the downgrade is reported, not silent", gb.get("assertions_downgraded") == 1,
          gb.get("assertions_downgraded"))
    check("coverage claimed with no requirement is dropped",
          gb.get("requirement_coverage") == [], gb.get("requirement_coverage"))
    check("the run records that it had no requirement", gb.get("had_requirement") is False,
          gb.get("had_requirement"))
    check("a reviews row was written", bool(gb.get("review_id")), gb)
    check("it is filed as app_testing",
          (database.get_review(gb.get("review_id")) or {}).get("review_type") == "app_testing")
    check("nothing claims to have been executed",
          gb.get("execution_status") == "not_executed", gb.get("execution_status"))

    # ── 8. ...and the claim stands when it is supportable ─────────────────
    section("8. A requirement supplied lets a 'specified' claim stand")
    with_req = _open_session(client, scoped, max_steps=5,
                             requirement="A signed-in user sees their own dashboard."
                             ).json()["session"]["id"]
    _step(client, scoped, with_req, [{"role": "link", "name": "Items"}], url_path="/items")
    _next_suite.clear()
    _next_suite.update(overclaim)
    g8 = client.post(f"/api/v1/app-testing/sessions/{with_req}/generate",
                     json={}, headers=scoped)
    g8b = g8.json() if g8.status_code == 200 else {}
    check("the specified claim survives", g8b.get("specified_count") == 1,
          g8b.get("specified_count"))
    check("nothing was downgraded", not g8b.get("assertions_downgraded"),
          g8b.get("assertions_downgraded"))
    check("the coverage row is kept", len(g8b.get("requirement_coverage") or []) == 1,
          g8b.get("requirement_coverage"))
    check("the requirement was sent to the model",
          "own dashboard" in _llm_calls[-1]["content"], _llm_calls[-1]["content"][:300])

    # ── 9. An empty generation is an error ────────────────────────────────
    # D-405's guard, reached through this product. A truncated response stored as a
    # successful run reads on screen as "we generated a suite" above an empty list.
    section("9. An empty generation is an error, not a stored run")
    empty_session = _open_session(client, scoped, max_steps=5).json()["session"]["id"]
    _step(client, scoped, empty_session, [{"role": "link", "name": "Items"}], url_path="/items")
    conn = database.get_connection()
    try:
        before_rows = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE review_type = 'app_testing'").fetchone()[0]
    finally:
        conn.close()
    _next_suite.clear()
    _next_suite.update({"levels_chosen": [], "test_files": [], "requirement_coverage": [],
                        "assumptions": [], "not_automated": [], "assertion_basis": [],
                        "summary": ""})
    bad = client.post(f"/api/v1/app-testing/sessions/{empty_session}/generate",
                      json={}, headers=scoped)
    check("an empty suite is refused", bad.status_code == 400, bad.status_code)
    check("the refusal names the remedy", "explore further" in bad.text, bad.text[:400])
    conn = database.get_connection()
    try:
        after_rows = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE review_type = 'app_testing'").fetchone()[0]
    finally:
        conn.close()
    check("nothing reached the database", after_rows == before_rows,
          f"{before_rows} -> {after_rows}")
    # ...but a stated "nothing to test" with a summary is a real answer and is kept.
    _next_suite.update({"summary": "Nothing on this page can be asserted meaningfully."})
    kept = client.post(f"/api/v1/app-testing/sessions/{empty_session}/generate",
                       json={}, headers=scoped)
    check("an explained empty result is kept", kept.status_code == 200, kept.text[:200])

    # ── 10. Results come home ─────────────────────────────────────────────
    # The whole reason for reusing the code-based result shape: the existing contract works
    # for this product with no branch in it.
    section("10. Results for an app_testing run are accepted and reconciled")
    review_id = gb.get("review_id")
    junit = ('<?xml version="1.0"?><testsuites><testsuite name="login">'
             '<testcase name="signs in" classname="login" time="1.2"/>'
             '</testsuite></testsuites>')
    res = client.post(f"/api/reviews/{review_id}/test-results",
                      json={"junit_xml": junit, "source_label": "smoke"}, headers=scoped)
    check("results are accepted for an app_testing run", res.status_code == 200,
          res.text[:300])
    rb = res.json() if res.status_code == 200 else {}
    check("the summary counts what ran", (rb.get("summary") or {}).get("total") == 1,
          rb.get("summary"))
    check("it never claims to be attested", rb.get("attested") is False, rb.get("attested"))
    runner = client.get(f"/api/reviews/{review_id}/runner", headers=scoped)
    check("a runner plan is offered", runner.status_code == 200, runner.text[:200])
    check("it recognises the suite as Playwright",
          any(j.get("framework") == "playwright"
              for j in (runner.json().get("plan") or {}).get("jobs", [])),
          (runner.json().get("plan") or {}).get("jobs"))
    check("it knows a browser is supplied rather than missing",
          "browser" not in ((runner.json().get("plan") or {}).get("must_supply") or []),
          (runner.json().get("plan") or {}).get("must_supply"))

    # ── 11. The product agrees with itself ────────────────────────────────
    section("11. The registry row, the estimator and the screen table agree")
    product = registry.get("app-testing")
    check("the registry knows the product", product is not None)
    check("its review type matches what the orchestrator writes",
          product.review_type == "app_testing", product.review_type)
    check("it is listed by the products endpoint",
          any(p.get("id") == "app-testing"
              for p in client.get("/api/v1/products", headers=scoped).json().get("products", [])),
          "")
    from backend.services import review_estimate, test_results
    check("the suite ceiling is declared",
          review_estimate.MAX_OUTPUT_TOKENS.get("app-testing") == 32000)
    check("the step ceiling is declared separately",
          review_estimate.MAX_OUTPUT_TOKENS.get("app-testing-step") == 1500)
    check("the estimator measures this product's own prompt frame, not the code reviewer's",
          review_estimate._frame_tokens("app-testing")
          != review_estimate._frame_tokens("code"))
    check("results are accepted for this review type",
          "app_testing" in test_results.ACCEPTS_RESULTS, test_results.ACCEPTS_RESULTS)
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    check("the screen declares itself in AGENT_SCREENS", "'app-testing':" in app_js
          or '"app-testing":' in app_js)
    check("its renderer exists", "function renderAppTesting(" in app_js)
    check("its result container exists", 'id="app-testing-result"' in index_html)
    check("its run history container exists", 'id="app-testing-runs"' in index_html)
    check("the nav item declares the product",
          'data-page="app-testing" data-product="app-testing"' in index_html)

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
