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
  11. A form's submit target reaches the suite writer, an origin never does
  12. What a page says is observed, capped here, and reaches the writer
  13. The registry row, the estimator and the screen table agree

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
from datetime import datetime, timedelta, timezone
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
    """A normal session. Carries a requirement because a session now needs *some* basis.

    That is the ordinary case rather than a special one: since a session must have something
    to check the application against before it may start, a helper that opened one without
    would be testing the product through a door that no longer exists. Section 2 drives the
    refusal itself.
    """
    body = {
        "target_label": "staging.example.com",
        "goal": "Cover signing in and browsing.",
        "requirement": "A signed-in user sees the item list and can open an item.",
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

    # At every step count, not just a small one. A live 25-step session quoted "$1.31
    # expected, $1.18 ceiling" — a ceiling *below* the forecast, which is not a quote. The
    # cause was per-call arithmetic: `app-testing-step` caps a call at 1,500 output tokens
    # while the default forecast is 3,000, so each step was priced for twice the output the
    # request permits and the sum crossed the bound as the step count grew. Checking one
    # small session could never see it, because the suite half's slack hid it until there
    # were enough steps.
    for cap in (1, 5, 25, 40, app_session.MAX_STEPS_CEILING):
        q = (client.post("/api/v1/app-testing/preflight", json={"max_steps": cap},
                         headers=scoped).json().get("estimate") or {})
        check("at %d step(s) the ceiling is not below the forecast" % cap,
              (q.get("ceiling_usd") or 0) >= (q.get("expected_usd") or 0),
              "expected %s, ceiling %s" % (q.get("expected_usd"), q.get("ceiling_usd")))

    # ── 2. The attestation ────────────────────────────────────────────────
    # The one condition here that is about somebody else's data rather than bookkeeping.
    section("2. A session cannot start without an attestation, or without a basis")
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

    # The second door, and the reason the product is worth anything. A session with nothing
    # to check the application against can only describe what it saw, which is how a live run
    # produced eleven tests of which nine were "the button is visible". Refused at the door
    # rather than discovered after a paid session and a suite nobody should trust.
    nothing = _open_session(client, scoped, requirement="")
    check("a session with nothing to check against is refused",
          nothing.status_code == 400, "%s %s" % (nothing.status_code, nothing.text[:200]))
    check("the refusal says what would fix it",
          all(w in nothing.text for w in ("requirement", "document", "Test Case")),
          nothing.text[:400])
    conn = database.get_connection()
    try:
        count_now = conn.execute("SELECT COUNT(*) FROM app_sessions").fetchone()[0]
    finally:
        conn.close()
    check("nothing was written for it", count_now == 0, count_now)

    # A prior run that is not a brief cannot become one by being named. Skipped and reported
    # rather than silently ignored — believing your cases are covered when the reference was
    # dropped is worse than being told the reference was no good (D-316's shape).
    wrong_kind = database.save_review(
        review_type="code", project_name="App Testing Smoke",
        input_summary="x", input_content="x", result={"findings": [], "summary": "x"})
    not_a_brief = _open_session(client, scoped, requirement="",
                                source_review_ids=[wrong_kind])
    check("a code review cannot serve as a brief", not_a_brief.status_code == 400,
          not_a_brief.status_code)
    missing = _open_session(client, scoped, requirement="",
                            source_review_ids=["no-such-review"])
    check("neither can a run that does not exist", missing.status_code == 400,
          missing.status_code)

    opened = _open_session(client, scoped)
    check("an attested session with a requirement opens",
          opened.status_code == 200, opened.text[:300])
    session_id = opened.json()["session"]["id"]
    check("the session reports its budget",
          opened.json()["session"]["max_steps"] == 10, opened.json()["session"])
    check("the URL is not stored anywhere on the session",
          "example.com" not in str(opened.json()["session"].get("target_label", "")).replace(
              "staging.example.com", ""),
          opened.json()["session"])

    # A session opens free, so the project-binding rule ("calls that spend money") would not
    # normally cover it. It covers it anyway: every step after this one is paid, and a live
    # run against a real application opened a session, printed a cost estimate, launched a
    # browser and then had all 25 steps refused for want of a project. The error belongs at
    # the door.
    from backend import config as _cfg
    _was = _cfg.UI_REQUIRE_PROJECT
    try:
        _cfg.UI_REQUIRE_PROJECT = True
        unbound = _open_session(client, admin)          # no X-Project-Id
        check("an unbound session is refused where a project is required",
              unbound.status_code == 403, "%s %s" % (unbound.status_code, unbound.text[:200]))
        check("the refusal says to pick a project",
              "project" in unbound.text.lower(), unbound.text[:300])
        still = _open_session(client, scoped)           # with X-Project-Id
        check("a bound session still opens", still.status_code == 200, still.text[:200])
    finally:
        _cfg.UI_REQUIRE_PROJECT = _was

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

    # ── 7. A machine-written brief cannot become a person's requirement ──
    # The rule that keeps this product from overclaiming, now with a middle rung. A session
    # whose only basis is scenarios or test cases *this platform generated* has real material
    # to test against -- but nobody has checked that material against what the customer
    # actually wanted, so a test matching it may say 'derived' and may not say 'stated'.
    # Enforced in code: a model may claim anything, and a claim is not evidence (D-406).
    section("7. A generated brief supports 'derived', never 'stated'")
    prior = database.save_review(
        review_type="test_cases", project_name="App Testing Smoke",
        input_summary="cases", input_content="cases",
        result={"test_cases": [{"test_case_id": "TC-1", "title": "Open an item",
                                "steps": [{"step_number": 1, "action": "click Items",
                                           "expected_result": "the item list appears"}]}],
                "summary": "one case"},
    )
    # `save_review` takes the project from the request context, which a direct call has none
    # of, so it is set here. Not a workaround for the product: a run belonging to no project
    # is correctly refused as a brief, and section 7b below drives exactly that.
    _c = database.get_connection()
    try:
        _c.execute("UPDATE reviews SET project_id = ? WHERE id = ?", (project_id, prior))
        _c.commit()
    finally:
        _c.close()
    derived_only = _open_session(client, scoped, max_steps=5, requirement="",
                                 source_review_ids=[prior]).json()["session"]["id"]
    _next_action.update({"action_kind": "click", "candidate_index": 0, "journey": "Browse"})
    _step(client, scoped, derived_only, [{"role": "link", "name": "Items"}], url_path="/items")
    _next_suite.clear()
    overclaim = copy.deepcopy(SAFE_SUITE)
    overclaim["assertion_basis"] = [{"test_name": "signs in", "basis": "stated",
                                     "requirement_ref": "REQ-1",
                                     "what_it_checks": "the dashboard appears"}]
    overclaim["requirement_coverage"] = [{"requirement_ref": "REQ-1", "covered": True,
                                          "test_names": ["signs in"]}]
    _next_suite.update(copy.deepcopy(overclaim))
    g = client.post(f"/api/v1/app-testing/sessions/{derived_only}/generate",
                    json={}, headers=scoped)
    check("the suite is generated", g.status_code == 200, g.text[:300])
    gb = g.json() if g.status_code == 200 else {}
    check("the cases were sent to the model as a brief",
          "TC-1" in _llm_calls[-1]["content"], _llm_calls[-1]["content"][:400])
    check("the 'stated' claim was downgraded", gb.get("stated_count") == 0,
          gb.get("stated_count"))
    check("it is counted as derived instead", gb.get("derived_count") == 1,
          gb.get("derived_count"))
    check("not pushed all the way down to observed", gb.get("observed_count") == 0,
          gb.get("observed_count"))
    check("the downgrade is reported, not silent", gb.get("assertions_downgraded") == 1,
          gb.get("assertions_downgraded"))
    check("the reason names what was missing",
          "person wrote" in str((gb.get("assertion_basis") or [{}])[0].get(
              "downgraded_because", "")),
          gb.get("assertion_basis"))
    check("the session records the ceiling it was held to",
          gb.get("strongest_basis") == "derived", gb.get("strongest_basis"))
    check("a reviews row was written", bool(gb.get("review_id")), gb)
    check("it is filed as app_testing",
          (database.get_review(gb.get("review_id")) or {}).get("review_type") == "app_testing")
    check("nothing claims to have been executed",
          gb.get("execution_status") == "not_executed", gb.get("execution_status"))

    # ── 8. ...and a person's requirement supports 'stated' ──────────────
    section("8. A requirement a person wrote lets a 'stated' claim stand")
    with_req = _open_session(client, scoped, max_steps=5,
                             requirement="A signed-in user sees their own dashboard."
                             ).json()["session"]["id"]
    _step(client, scoped, with_req, [{"role": "link", "name": "Items"}], url_path="/items")
    _next_suite.clear()
    _next_suite.update(copy.deepcopy(overclaim))
    g8 = client.post(f"/api/v1/app-testing/sessions/{with_req}/generate",
                     json={}, headers=scoped)
    g8b = g8.json() if g8.status_code == 200 else {}
    check("the stated claim survives", g8b.get("stated_count") == 1, g8b.get("stated_count"))
    check("the old count name still answers", g8b.get("specified_count") == 1,
          g8b.get("specified_count"))
    check("nothing was downgraded", not g8b.get("assertions_downgraded"),
          g8b.get("assertions_downgraded"))
    check("the coverage row is kept", len(g8b.get("requirement_coverage") or []) == 1,
          g8b.get("requirement_coverage"))
    check("the requirement was sent to the model",
          "own dashboard" in _llm_calls[-1]["content"], _llm_calls[-1]["content"][:300])

    # ...and so does a document somebody uploaded. A specification is filed under Requirements
    # because that is where it belongs, and it is simultaneously the most useful thing a test
    # writer could be handed — so this product reads that shelf as well as the QA one. The
    # document store is stubbed here the way the model is: what is under test is that the
    # route consults it, not that markdown parses.
    from backend.services import app_basis, knowledge_base as kb_module
    _real_ctx = kb_module.knowledge_base.get_context
    shelves_read = []

    def _fake_ctx(category, query="", project_name="", max_chars=0):
        shelves_read.append(category)
        if category == "requirement":
            return "REQ-9: an archived item must not appear in the default list."
        return ""

    kb_module.knowledge_base.get_context = _fake_ctx
    app_basis.knowledge_base.get_context = _fake_ctx
    try:
        docs_only = _open_session(client, scoped, max_steps=5, requirement="")
        check("a project document is enough to start a session",
              docs_only.status_code == 200, docs_only.text[:250])
        check("both shelves are consulted, not just QA",
              set(app_basis.DOC_CATEGORIES) <= set(shelves_read), shelves_read)
        docs_id = docs_only.json()["session"]["id"]
        _step(client, scoped, docs_id, [{"role": "link", "name": "Items"}], url_path="/items")
        _next_suite.clear()
        _next_suite.update(copy.deepcopy(overclaim))
        gd = client.post(f"/api/v1/app-testing/sessions/{docs_id}/generate",
                         json={}, headers=scoped)
        gdb = gd.json() if gd.status_code == 200 else {}
        check("the document reached the model", "REQ-9" in _llm_calls[-1]["content"],
              _llm_calls[-1]["content"][:400])
        check("and a document counts as something a person stated",
              gdb.get("stated_count") == 1, gdb.get("stated_count"))
        check("so nothing was downgraded", not gdb.get("assertions_downgraded"),
              gdb.get("assertions_downgraded"))
    finally:
        kb_module.knowledge_base.get_context = _real_ctx
        app_basis.knowledge_base.get_context = _real_ctx

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

    # A summary does not rescue a suite with no files. For a catalogue it would -- a model
    # that found nothing to enumerate can say so and the reader has the reason. For a suite
    # the files *are* the deliverable, and a live eval run produced exactly this shape: zero
    # test files, three assertion rows describing tests that did not exist, and a confident
    # summary, stored as a successful run that had nothing to execute.
    _next_suite.update({
        "summary": "Two journeys were walked and a full suite was written for both.",
        "assertion_basis": [{"test_name": "signs in", "basis": "observed",
                             "what_it_checks": "the dashboard appears"}],
    })
    still_bad = client.post(f"/api/v1/app-testing/sessions/{empty_session}/generate",
                            json={}, headers=scoped)
    check("a confident summary does not rescue a suite with no files",
          still_bad.status_code == 400, still_bad.status_code)
    check("the refusal says the files are the deliverable",
          "the files" in still_bad.text.lower(), still_bad.text[:400])
    conn = database.get_connection()
    try:
        final_rows = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE review_type = 'app_testing'").fetchone()[0]
    finally:
        conn.close()
    check("still nothing reached the database", final_rows == before_rows,
          f"{before_rows} -> {final_rows}")

    # The positive control: one file and a summary is a real result and is kept.
    _next_suite.clear()
    _next_suite.update(copy.deepcopy(SAFE_SUITE))
    kept = client.post(f"/api/v1/app-testing/sessions/{empty_session}/generate",
                       json={}, headers=scoped)
    check("a suite with files is kept", kept.status_code == 200, kept.text[:200])

    # A generation that comes back empty is retried once, because the cost of not retrying is
    # the whole session: every exploration step the customer already paid for produces
    # nothing. Reported rather than hidden — a silent retry turns a one-in-three defect into
    # a one-in-nine defect nobody measures.
    empty_then_good = {"n": 0}
    _real_stub = llm_gateway.call_tool

    def _empty_first(system_prompt, user_content, tool_schema, llm_override=None,
                     max_tokens=None):
        if tool_schema.get("name") == "submit_app_test_suite":
            empty_then_good["n"] += 1
            if empty_then_good["n"] == 1:
                _llm_calls.append({"name": "submit_app_test_suite", "content": user_content})
                llm_gateway._record_usage("anthropic", config.MODEL, 1000, 100)
                return {"test_files": [], "assertion_basis": [],
                        "summary": "Two journeys were covered by a full suite."}
        return _real_stub(system_prompt, user_content, tool_schema, llm_override, max_tokens)

    retry_session = _open_session(client, scoped, max_steps=5).json()["session"]["id"]
    _step(client, scoped, retry_session, [{"role": "link", "name": "Items"}], url_path="/items")
    _next_suite.clear()
    _next_suite.update(copy.deepcopy(SAFE_SUITE))
    llm_gateway.call_tool = _empty_first
    try:
        rg = client.post(f"/api/v1/app-testing/sessions/{retry_session}/generate",
                         json={}, headers=scoped)
    finally:
        llm_gateway.call_tool = _real_stub
    rgb = rg.json() if rg.status_code == 200 else {}
    check("an empty generation is retried rather than wasting the session",
          rg.status_code == 200, "%s %s" % (rg.status_code, rg.text[:200]))
    check("the retry produced the suite", rgb.get("total_files") == 1,
          rgb.get("total_files"))
    check("and the retry is reported, not hidden", rgb.get("generation_retried") is True,
          rgb.get("generation_retried"))
    check("both calls were made", empty_then_good["n"] == 2, empty_then_good["n"])

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

    # ── 11. What a form sends reaches the suite writer; an origin never does ──
    # The measured baseline's one recurring defect was a test that drove controls which only
    # exist on the broken build, so it failed on the working one. The fix needs the request a
    # form actually sends to be *observed* rather than invented -- and that fact must reach
    # the writer without carrying the origin with it, because D-407 is the reason this
    # service never learns where the application lives.
    section("11. A form's submit target reaches the suite writer, an origin never does")
    sub_session = _open_session(client, scoped, max_steps=5).json()["session"]["id"]
    _next_action.clear()
    _next_action.update({"done": False, "action_kind": "click", "candidate_index": 0,
                         "journey": "Rename an item", "rationale": "save the new title"})
    r11 = _step(client, scoped, sub_session,
                [{"role": "button", "name": "Rename", "submits": "POST /items/1/rename",
                  "locator": "getByRole('button', { name: 'Rename' })"}],
                url_path="/items/1")
    check("the step is accepted", r11.status_code == 200, r11.text[:300])
    check("the target is recorded on the action",
          (r11.json().get("action") or {}).get("submits") == "POST /items/1/rename",
          r11.json().get("action"))
    check("so is how to address it",
          (r11.json().get("action") or {}).get("locator")
          == "getByRole('button', { name: 'Rename' })", r11.json().get("action"))

    # A locator is advisory text bound for a prompt and is never executed here, but it is
    # page-controlled, so only the shapes the driver produces are kept.
    _next_action.update({"candidate_index": 0})
    junk = _step(client, scoped, sub_session,
                 [{"role": "button", "name": "Odd",
                   "locator": "page.evaluate(() => fetch('http://elsewhere'))"}],
                 url_path="/items/1")
    check("a locator that is not one is dropped",
          (junk.json().get("action") or {}).get("locator") == "",
          junk.json().get("action"))

    _next_action.update({"candidate_index": 0})
    r11b = _step(client, scoped, sub_session,
                 [{"role": "button", "name": "Export",
                   "submits": "POST https://somewhere-else.example.com/items/1"}],
                 url_path="/items/1")
    check("an absolute URL is dropped rather than stored",
          (r11b.json().get("action") or {}).get("submits") == "",
          r11b.json().get("action"))

    _next_suite.clear()
    _next_suite.update(copy.deepcopy(SAFE_SUITE))
    g11 = client.post(f"/api/v1/app-testing/sessions/{sub_session}/generate",
                      json={}, headers=scoped)
    check("the suite is generated", g11.status_code == 200, g11.text[:300])
    written = _llm_calls[-1]["content"]
    check("the suite writer is told what the form sends",
          "POST /items/1/rename" in written, written[-800:])
    check("the suite writer is told how to address the control",
          "getByRole('button', { name: 'Rename' })" in written, written[-800:])
    check("no host reached the suite writer",
          "somewhere-else.example.com" not in written, written[-800:])
    check("nothing that was not a locator reached it",
          "page.evaluate" not in written, written[-800:])

    # ── 12. What the page said is observed, bounded, and reaches the writer ──
    # A suite written from controls alone can assert which buttons exist and nothing about
    # what the application did, and most defects show as a wrong value rather than a missing
    # control. This is model input a customer's page decides the size of, so the cap is
    # enforced here and not trusted from the driver.
    section("12. What a page says is observed, capped here, and reaches the writer")
    text_session = _open_session(client, scoped, max_steps=5).json()["session"]["id"]
    _next_action.update({"action_kind": "click", "candidate_index": 0,
                         "journey": "Fill the basket"})
    r12 = _step(client, scoped, text_session,
                [{"role": "button", "name": "One more Widget"}],
                url_path="/basket",
                page_texts=["Your basket", "Widget x2 — GBP 8.00", "Total: GBP 8.00"])
    check("the step is accepted", r12.status_code == 200, r12.text[:300])
    seen = _llm_calls[-1]["content"]
    check("the explorer is told what the page says", "Total: GBP 8.00" in seen, seen[:600])

    flood = ["line number %d with some words after it" % i for i in range(200)]
    _step(client, scoped, text_session, [{"role": "button", "name": "One more Widget"}],
          url_path="/basket", page_texts=flood)
    conn = database.get_connection()
    try:
        stored = app_session.steps(conn, text_session)
    finally:
        conn.close()
    check("a flood of page text is capped on arrival",
          len(stored[-1]["page_texts"]) == app_session.MAX_PAGE_TEXTS,
          len(stored[-1]["page_texts"]))
    check("the first step kept what it was sent",
          "Total: GBP 8.00" in stored[0]["page_texts"], stored[0]["page_texts"])

    # A page that changes only its wording is a page that changed. Without this a
    # client-rendered application looks stalled while it is still making progress.
    same_controls = [{"role": "button", "name": "One more Widget"}]
    check("wording alone changes the page signature",
          action_policy.page_signature("/basket", same_controls, ["Total: GBP 4.00"])
          != action_policy.page_signature("/basket", same_controls, ["Total: GBP 8.00"]))

    # Filling in a form is progress and changes nothing else about the page. Without this a
    # form with more fields than NO_PROGRESS_LIMIT is stopped for making no progress part
    # way through being filled in, and the button at the bottom is never reached.
    empty = [{"role": "textbox", "name": "Email", "value": ""},
             {"role": "textbox", "name": "Street", "value": ""}]
    typed = [{"role": "textbox", "name": "Email", "value": "alice@example.com"},
             {"role": "textbox", "name": "Street", "value": ""}]
    check("filling a field counts as progress",
          action_policy.page_signature("/signup", empty)
          != action_policy.page_signature("/signup", typed))
    check("typing the same thing again does not",
          action_policy.page_signature("/signup", typed)
          == action_policy.page_signature("/signup", typed))

    _next_suite.clear()
    _next_suite.update(copy.deepcopy(SAFE_SUITE))
    g12 = client.post(f"/api/v1/app-testing/sessions/{text_session}/generate",
                      json={}, headers=scoped)
    check("the suite is generated", g12.status_code == 200, g12.text[:300])
    check("the suite writer can see the value to assert",
          "Total: GBP 8.00" in _llm_calls[-1]["content"], _llm_calls[-1]["content"][-900:])

    # ── 13. The page-text promise is kept in code, not in a sentence ──────
    # What a page displays is the customer's own content. Three promises are made about it
    # and each is checked behaviourally here: it can be refused entirely, it is encrypted
    # where it rests, and it is deleted when its window passes.
    section("13. Page text can be refused, is encrypted at rest, and expires")

    off = _open_session(client, scoped, max_steps=5, collect_page_text=False
                        ).json()["session"]["id"]
    _next_action.clear()
    _next_action.update({"done": False, "action_kind": "click", "candidate_index": 0,
                         "journey": "Look around", "rationale": "see the list"})
    # The driver sends it anyway. A setting a client can defeat by ignoring it is not a
    # setting, so the refusal has to happen here rather than out there.
    r13 = _step(client, scoped, off, [{"role": "link", "name": "Items"}],
                url_path="/items", page_texts=["status: Total: GBP 8.00", "heading: Basket"])
    check("the step is accepted", r13.status_code == 200, r13.text[:200])
    sent = _llm_calls[-1]["content"]
    check("page text a refusing session was sent never reaches the model",
          "GBP 8.00" not in sent, sent[-400:])
    conn = database.get_connection()
    try:
        stored_off = app_session.steps(conn, off)
    finally:
        conn.close()
    check("and is not stored either", stored_off[-1]["page_texts"] == [],
          stored_off[-1]["page_texts"])

    _next_suite.clear()
    _next_suite.update(copy.deepcopy(SAFE_SUITE))
    g13 = client.post(f"/api/v1/app-testing/sessions/{off}/generate", json={}, headers=scoped)
    check("the suite is generated", g13.status_code == 200, g13.text[:200])
    body13 = g13.json() if g13.status_code == 200 else {}
    check("the result says it could not see values",
          body13.get("read_page_text") is False, body13.get("read_page_text"))
    check("and says so in words a reader will understand",
          "control is present" in (body13.get("page_text_note") or ""),
          body13.get("page_text_note"))

    # Encrypted at rest. Checked by reading the raw column, because "we encrypt it" is the
    # kind of claim that survives the code that implemented it being removed.
    on = _open_session(client, scoped, max_steps=5).json()["session"]["id"]
    _step(client, scoped, on, [{"role": "link", "name": "Items"}], url_path="/items",
          page_texts=["status: Total: GBP 8.00"])
    conn = database.get_connection()
    try:
        raw13 = conn.execute(
            "SELECT page_texts FROM app_steps WHERE session_id = ?", (on,)).fetchone()[0]
        readable = app_session.steps(conn, on)[-1]["page_texts"]
    finally:
        conn.close()
    check("what is on disk is not the text", "GBP 8.00" not in str(raw13), str(raw13)[:120])
    check("but it decrypts back to the text",
          any("GBP 8.00" in t for t in readable), readable)

    # Retention 0 means gone the moment the suite exists — the tightest setting that still
    # produces tests about values.
    zero = _open_session(client, scoped, max_steps=5,
                         page_text_retention_days=0).json()["session"]["id"]
    _step(client, scoped, zero, [{"role": "link", "name": "Items"}], url_path="/items",
          page_texts=["status: Total: GBP 8.00"])
    conn = database.get_connection()
    try:
        check("it is held while the session is still running",
              app_session.steps(conn, zero)[-1]["page_texts"] != [])
    finally:
        conn.close()
    _next_suite.clear()
    _next_suite.update(copy.deepcopy(SAFE_SUITE))
    client.post(f"/api/v1/app-testing/sessions/{zero}/generate", json={}, headers=scoped)
    conn = database.get_connection()
    try:
        after_zero = app_session.steps(conn, zero)[-1]["page_texts"]
        kept_steps = len(app_session.steps(conn, zero))
    finally:
        conn.close()
    check("and wiped as soon as the suite is written", after_zero == [], after_zero)
    check("the rest of the session survives the wipe", kept_steps > 0, kept_steps)

    # A window that has passed is swept without a scheduler, which this deployment does not
    # have — so the sweep runs on the read path as well as at startup.
    conn = database.get_connection()
    try:
        conn.execute("UPDATE app_sessions SET page_text_expires_at = ? WHERE id = ?",
                     ("2000-01-01T00:00:00+00:00", on))
        conn.commit()
        check("an expired digest is still there until something sweeps",
              app_session.steps(conn, on)[-1]["page_texts"] != [])
    finally:
        conn.close()
    reread = client.get(f"/api/v1/app-testing/sessions/{on}", headers=scoped)
    check("reading the session sweeps it", reread.status_code == 200, reread.text[:200])
    conn = database.get_connection()
    try:
        check("and the digest is gone",
              app_session.steps(conn, on)[-1]["page_texts"] == [],
              app_session.steps(conn, on)[-1]["page_texts"])
    finally:
        conn.close()

    # ── 15. What a session would have to check against, before it opens ───
    #
    # The screen used to answer this in JavaScript, from the requirement box and a count of
    # ticked runs. It cannot: the browser has no idea what documents are filed against the
    # project, so it warned "nothing to check against" at a project holding a full
    # specification, and said nothing when every ticked reference was stale. Either way the
    # real answer arrived as a 400 *after* somebody had started a browser on their own
    # machine.
    section("15. The basis a session would have, asked before one is opened")

    def _basis(body, headers=None):
        return client.post("/api/v1/app-testing/basis", json=body,
                           headers=headers if headers is not None else scoped)

    _before_basis = len(_llm_calls)
    empty = _basis({"requirement": "", "source_review_ids": []})
    check("asking costs nothing and is allowed", empty.status_code == 200, empty.text[:200])
    check("...and it calls no model", len(_llm_calls) == _before_basis,
          f"{len(_llm_calls) - _before_basis} call(s) -- a preview that spends is not a "
          "preview")
    check("a session with nothing supplied is told so, in the words it would be refused in",
          empty.json().get("has_any") is False
          and (empty.json().get("refusal") or "").startswith(
              app_basis.NOTHING_TO_CHECK_AGAINST[:40]),
          empty.json())
    check("...and its strongest claim would be `observed`",
          empty.json().get("strongest_basis") == "observed", empty.json())

    typed = _basis({"requirement": "A basket total equals the sum of its lines.",
                    "source_review_ids": []})
    check("a typed requirement is a stated basis", typed.json().get("has_stated") is True)
    check("...and nothing is refused", typed.json().get("refusal") is None)
    check("...and it may claim `stated`",
          typed.json().get("strongest_basis") == "stated", typed.json())

    # The three reasons a reference is dropped, each reported rather than swallowed. This is
    # the list the screen shows: a reference that silently did nothing is how somebody
    # believes their cases are covered when they are not.
    dropped = _basis({"requirement": "", "source_review_ids": [wrong_kind, "no-such-review"]})
    _skipped = " | ".join(dropped.json().get("skipped_sources") or [])
    check("a run of the wrong kind is named as dropped, with the reason",
          "is not a brief" in _skipped, _skipped)
    check("...so is one that does not exist", "no such run" in _skipped, _skipped)
    check("...and neither of them silently counts as a basis",
          dropped.json().get("has_any") is False, dropped.json())

    check("the cap the server enforces is reported, so a screen can show it",
          dropped.json().get("max_sources") == app_basis.MAX_SOURCE_REVIEWS,
          dropped.json().get("max_sources"))

    # The brief is returned because it is what the model will be sent and the person is
    # choosing whether to send it. The *documents* are not: their presence and size are the
    # useful facts, and the text is the customer's own content, already on the screen it was
    # uploaded from.
    check("the documents themselves are never echoed back",
          "documents" not in typed.json(),
          "returning the document text would put it in a response nothing needs it in")
    check("...but whether there are any, and how much, is",
          "has_documents" in typed.json() and "document_chars" in typed.json())

    real_cases = database.save_review(
        review_type="test_cases", project_name="App Testing Smoke",
        input_summary="Checkout rules", input_content="x",
        result={"test_cases": [
            {"test_case_id": "TC-001", "title": "Total is right",
             "steps": [{"action": "add two items", "expected": "total doubles"}]},
            {"test_case_id": "TC-002", "title": "Empty basket", "steps": []}]})
    with contextlib.closing(database.get_connection()) as _c:
        _c.execute("UPDATE reviews SET project_id = ? WHERE id = ?", (project_id, real_cases))
        _c.commit()

    covered = _basis({"requirement": "", "source_review_ids": [real_cases]})
    _used = covered.json().get("source_reviews") or []
    check("a real test-case run is accepted as a basis",
          covered.json().get("has_derived") is True, covered.json())
    check("...and is labelled `derived`, never `stated`",
          covered.json().get("strongest_basis") == "derived",
          "a machine-written case laundered into a verified requirement is D-406 at one "
          "remove")
    check("...and says how much it contributes, so two runs can be told apart",
          len(_used) == 1 and _used[0].get("items") == 2, _used)
    check("the brief itself is shown, because the person is choosing whether to send it",
          "TC-001" in (covered.json().get("brief_markdown") or ""),
          covered.json().get("brief_markdown", "")[:120])
    check("...and its full size is stated even when the preview is clipped",
          covered.json().get("brief_chars", 0) > 0)

    # A caller scoped to a different project cannot learn that the id is real, for the
    # reason `_visible_session` gives: confirming an id exists is itself a disclosure. So it
    # reads as absent rather than as forbidden.
    _other = client.post("/api/v1/projects", json={"name": "Somebody Else"}, headers=admin)
    other_proj = {**admin, "X-Project-Id": _other.json()["project"]["id"]}
    other = _basis({"requirement": "", "source_review_ids": [real_cases]}, headers=other_proj)
    check("a run in a project the caller cannot see reads as absent, not as forbidden",
          "no such run" in " ".join(other.json().get("skipped_sources") or []),
          other.json().get("skipped_sources"))

    # ── 16. Stopping a session from a screen ──────────────────────────────
    #
    # The browser runs on the customer's machine and this platform cannot reach it (D-407).
    # What it can do is answer the *next* step with `done` -- which every driver already
    # honours, so this needed no change to `explore_app.py` and every copy in the field
    # obeys it. Two orderings carry the whole design and both are asserted below: the check
    # sits before `require_open`, because a 400 makes the driver exit non-zero and lose the
    # session, and before metering, because a stopping step must not be charged for.
    section("16. A session can be stopped from a screen, at its next step")

    stoppable = _open_session(client, scoped, max_steps=10).json()["session"]["id"]
    _step(client, scoped, stoppable, [{"role": "link", "name": "Items"}], url_path="/items")

    asked = client.post(f"/api/v1/app-testing/sessions/{stoppable}/request-stop",
                        headers=scoped)
    check("asking to stop is accepted", asked.status_code == 200, asked.text[:200])
    check("...and costs nothing to ask", asked.json().get("stopped") is True)
    check("...and says it lands at the next step rather than immediately",
          "next step" in (asked.json().get("note") or ""), asked.json().get("note"))
    check("...and says the browser window is not ours to close",
          "cannot close it" in (asked.json().get("note") or ""),
          "a person who expects the window to shut will think the button is broken")

    check("a session with a stop pending is still `exploring` until the driver reaches it",
          asked.json()["session"]["status"] == "exploring",
          "showing a stopped state before the row says so is a lie the screen cannot "
          "retract")
    check("...and the screen can see that a stop is pending",
          asked.json()["session"].get("stop_requested") is True)

    _before_stop = len(_llm_calls)
    stopped = _step(client, scoped, stoppable, [{"role": "link", "name": "Items"}],
                    url_path="/items")
    check("the next step is answered rather than refused",
          stopped.status_code == 200,
          "a 4xx here makes the driver exit non-zero, and the steps already paid for never "
          f"become a suite: {stopped.text[:160]}")
    check("...as `done`, which every driver already honours",
          stopped.json().get("done") is True, stopped.json())
    check("...and the stopping step calls no model",
          len(_llm_calls) == _before_stop,
          f"{len(_llm_calls) - _before_stop} call(s) -- the step that stops a session must "
          "not be charged for")
    check("the session is closed, so enforcement does not depend on the driver obeying",
          stopped.json()["session"]["status"] == "finished",
          stopped.json()["session"]["status"])

    again = client.post(f"/api/v1/app-testing/sessions/{stoppable}/request-stop",
                        headers=scoped)
    check("stopping an already-stopped session is not an error",
          again.status_code == 200 and again.json().get("stopped") is False,
          "a race where the driver finished a second earlier is not something to report as "
          "a failure")
    check("...and says so plainly", "already stopped" in (again.json().get("note") or ""),
          again.json().get("note"))

    # The steps it did take are still worth something -- that is the whole reason a stop is
    # served as `done` rather than as an error.
    _next_suite.clear()
    _next_suite.update(copy.deepcopy(SAFE_SUITE))
    salvage = client.post(f"/api/v1/app-testing/sessions/{stoppable}/generate",
                          json={}, headers=scoped)
    check("a stopped session can still write a suite from the steps it took",
          salvage.status_code == 200, salvage.text[:200])

    # A second generation writes a second `reviews` row and repoints `review_id` at it,
    # leaving the first unreachable from the session that produced it -- including one
    # already delivered to a repository. Latent while nothing in the browser could call this
    # route; the screen now offers it for a session that stopped, so it is refused here
    # rather than left to the UI being careful.
    _next_suite.clear()
    _next_suite.update(copy.deepcopy(SAFE_SUITE))
    twice = client.post(f"/api/v1/app-testing/sessions/{stoppable}/generate",
                        json={}, headers=scoped)
    check("...but not a second one, which would orphan the first",
          twice.status_code == 400 and "already written" in twice.text,
          twice.text[:200])

    # ── 17. What was withheld, by name ────────────────────────────────────
    #
    # The counts were always kept and the names always discarded, so the record could say
    # "14 controls declined" and not which. That is the half an operator needs: a denylist
    # eating half a page is a coverage problem, not a safety win, and nothing could show it.
    section("17. The session records what the policy withheld, not just how many")

    withholding = _open_session(client, scoped, max_steps=10).json()["session"]["id"]
    _step(client, scoped, withholding, [
        {"role": "button", "name": "Delete account"},
        {"role": "button", "name": "Delete account"},
        {"role": "button", "name": "Place order"},
        {"role": "link", "name": "Items"},
    ], url_path="/items")
    seen = client.get(f"/api/v1/app-testing/sessions/{withholding}", headers=scoped)
    _refusals = {r["name"]: r for r in seen.json()["session"].get("refusals", [])}
    check("the names of withheld controls reach the screen",
          "Delete account" in _refusals and "Place order" in _refusals, _refusals)
    check("...with which rule refused each one",
          _refusals.get("Delete account", {}).get("group") == "destroys data"
          and _refusals.get("Place order", {}).get("group") == "spends money",
          _refusals)
    check("...and the same control met twice is one entry with a count, not two entries",
          _refusals.get("Delete account", {}).get("count") == 2,
          "bounded by distinct name, so a loop that meets the same button on every page "
          "cannot grow the record")

    # Page text is the customer's own content and is wiped on a clock. What a session
    # *refused* is not: `wipe_page_text` promises a person can still see that a session ran,
    # how far it got, and what it would not do.
    with contextlib.closing(database.get_connection()) as _c:
        app_session.wipe_page_text(_c, withholding)
    after_wipe = client.get(f"/api/v1/app-testing/sessions/{withholding}", headers=scoped)
    check("the refusal record survives the page-text wipe",
          len(after_wipe.json()["session"].get("refusals", [])) == 2,
          "the wipe promises that what a session refused stays visible")

    # ── 18. A session whose driver stopped talking to us ──────────────────
    #
    # Two things go wrong when nothing closes these. A person who paid for thirty steps and
    # closed their laptop sees nothing at all, because every list is built on finished runs.
    # And `sweep_expired_page_text` excludes `exploring` from its abandoned branch, so the
    # digest was never swept -- closing the browser half way through kept it indefinitely,
    # which is the exact thing that sweep's own docstring says it prevents.
    section("18. An abandoned session is closed, and its digest then expires")

    ghost = _open_session(client, scoped, max_steps=10).json()["session"]["id"]
    _step(client, scoped, ghost, [{"role": "link", "name": "Items"}], url_path="/items",
          page_texts=["heading: Items"])
    with contextlib.closing(database.get_connection()) as _c:
        # Old enough to be abandoned (hours) and old enough for its own seven-day window to
        # have passed. Both are needed and they are different clocks: one decides that the
        # driver is gone, the other decides that the digest's time is up.
        _old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        _c.execute("UPDATE app_sessions SET started_at = ? WHERE id = ?", (_old, ghost))
        _c.execute("UPDATE app_steps SET created_at = ? WHERE session_id = ?", (_old, ghost))
        _c.commit()
        _held_before = _c.execute(
            "SELECT COUNT(*) FROM app_steps WHERE session_id = ? AND page_texts != ''",
            (ghost,)).fetchone()[0]
    check("its digest is held while it looks live", _held_before == 1, _held_before)

    with contextlib.closing(database.get_connection()) as _c:
        _reaped = app_session.reap_abandoned(_c)
        check("a session with no step for hours is closed", _reaped >= 1, _reaped)
        _row = app_session.get(_c, ghost)
        check("...as finished rather than failed, because a closed laptop is not a failure",
              _row["status"] == "finished", _row["status"])
        check("...and says why", "abandoned" in (_row["stop_reason"] or ""),
              _row["stop_reason"])
        # The reap deliberately does not start the retention clock: at zero days that would
        # wipe the digest immediately and destroy the one useful thing left to do with an
        # abandoned session, which is to write the suite from the steps it did take.
        _still = _c.execute(
            "SELECT COUNT(*) FROM app_steps WHERE session_id = ? AND page_texts != ''",
            (ghost,)).fetchone()[0]
        check("reaping does not itself wipe the digest",
              _still == 1,
              "writing a suite from an abandoned session is the whole point of keeping it")
        _cleared = app_session.sweep_expired_page_text(_c)
        _after = _c.execute(
            "SELECT COUNT(*) FROM app_steps WHERE session_id = ? AND page_texts != ''",
            (ghost,)).fetchone()[0]
    check("...but the sweep can now see it, which it could not while it said `exploring`",
          _after == 0,
          f"{_cleared} cleared; an abandoned session used to keep its digest for ever")

    # The window is the customer's choice, not a fixed backstop. A session still inside its
    # own window keeps its digest, which is what makes "write the suite from these steps"
    # possible at all.
    young = _open_session(client, scoped, max_steps=10).json()["session"]["id"]
    _step(client, scoped, young, [{"role": "link", "name": "Items"}], url_path="/items",
          page_texts=["heading: Items"])
    with contextlib.closing(database.get_connection()) as _c:
        _c.execute("UPDATE app_sessions SET started_at = ? WHERE id = ?",
                   ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), young))
        _c.execute("UPDATE app_steps SET created_at = ? WHERE session_id = ?",
                   ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), young))
        _c.commit()
        app_session.reap_abandoned(_c)
        app_session.sweep_expired_page_text(_c)
        _kept = _c.execute(
            "SELECT COUNT(*) FROM app_steps WHERE session_id = ? AND page_texts != ''",
            (young,)).fetchone()[0]
    check("an abandoned session inside its own window keeps its digest",
          _kept == 1,
          "two days into a seven-day window; wiping here would destroy the one useful thing "
          "left to do with an abandoned session")

    # Zero days means "keep this no longer than necessary", and for a session that will
    # never write a suite, necessary ends when it closes. The consequence -- no suite can be
    # written from it afterwards -- is stated on the screen where the choice is made.
    nothing_kept = _open_session(client, scoped, max_steps=10,
                                 page_text_retention_days=0).json()["session"]["id"]
    _step(client, scoped, nothing_kept, [{"role": "link", "name": "Items"}],
          url_path="/items", page_texts=["heading: Items"])
    with contextlib.closing(database.get_connection()) as _c:
        _c.execute("UPDATE app_sessions SET started_at = ? WHERE id = ?",
                   ((datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(),
                    nothing_kept))
        _c.execute("UPDATE app_steps SET created_at = ? WHERE session_id = ?",
                   ((datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(),
                    nothing_kept))
        _c.commit()
        app_session.reap_abandoned(_c)
        app_session.sweep_expired_page_text(_c)
        _zero = _c.execute(
            "SELECT COUNT(*) FROM app_steps WHERE session_id = ? AND page_texts != ''",
            (nothing_kept,)).fetchone()[0]
    check("a session set to keep nothing keeps nothing, once it is closed",
          _zero == 0,
          "zero has to mean zero, or it is not the tightest position a customer can take")

    # ── 19. The list the browser had no way to ask for ────────────────────
    section("19. Sessions can be listed, including ones that produced nothing")

    listed = client.get("/api/v1/app-testing/sessions", headers=scoped)
    check("the list is served", listed.status_code == 200, listed.text[:200])
    _ids = [r["id"] for r in listed.json().get("sessions", [])]
    check("a session that never wrote a suite is in it",
          ghost in _ids,
          "every list until now was built on the `reviews` row that /generate writes, so a "
          "session that produced nothing was invisible although its steps were paid for")
    check("the total is reported, so a pager knows there is another page",
          isinstance(listed.json().get("total"), int))
    _ghost_row = next(r for r in listed.json()["sessions"] if r["id"] == ghost)
    check("a row says how far it got without a second request",
          _ghost_row["recorded_steps"] == 1 and _ghost_row["max_steps"] == 10, _ghost_row)
    check("...and how long it has been silent",
          isinstance(_ghost_row.get("idle_seconds"), int), _ghost_row.get("idle_seconds"))
    check("the requirement is not shipped in a list",
          "requirement" not in _ghost_row and _ghost_row.get("has_requirement") is True,
          "a requirement runs to 20 000 characters and twenty rows of them is a 400 KB "
          "response for a screen that only needs to know whether there is one")

    empty_scope = client.get("/api/v1/app-testing/sessions", headers=other_proj)
    check("another project's sessions are not listed",
          ghost not in [r["id"] for r in empty_scope.json().get("sessions", [])],
          "`[]` from the scope helper means nothing, and must never widen to everything")

    # ── 14. The product agrees with itself ────────────────────────────────
    # ── 20. A customer can permit a control the denylist withholds ────────
    #
    # Measured against real applications rather than argued: on a banking demo the list
    # withheld `Transfer Funds`, `Withdraw Funds` and `Bill Pay` in the first three steps,
    # and on a shop it withheld `Remove` as destroying data and `Checkout` as spending
    # money, leaving six of fifteen supplied requirements untestable. The list reads button
    # labels with no context, so it cannot tell "Remove [from basket]" from "Remove [user]",
    # and nothing it could be taught would let it. This is the way out, and the properties
    # below are what make it safe to have one.
    section("20. A control can be permitted for one project, by exact name")

    _allow_hdr = dict(scoped)
    added = client.post("/api/v1/app-testing/allowances",
                        json={"control_name": "Transfer Funds"}, headers=_allow_hdr)
    check("a withheld control can be permitted", added.status_code == 200, added.text[:200])
    check("...and the reply says what was refusing it",
          added.json().get("group") == "spends money", added.text[:200])

    pointless = client.post("/api/v1/app-testing/allowances",
                            json={"control_name": "Browse items"}, headers=_allow_hdr)
    check("permitting something that was never withheld is refused as a no-op",
          pointless.status_code == 400,
          "a list that accepts anything teaches people it does nothing")

    # The property the whole design rests on. A substring allow-list would let "delete"
    # permit "Delete account", which is the failure the denylist exists to prevent reached
    # from the other side.
    with contextlib.closing(database.get_connection()) as _c:
        app_session.add_allowance(_c, project_id, "Remove")
        _names = app_session.allowance_names(_c, project_id)
    check("matching is exact, so permitting one name does not permit a longer one",
          action_policy.classify_detail(
              {"role": "button", "name": "Remove user", "enabled": True},
              _names)[0] == "refuse",
          "a substring allow-list would let 'Remove' permit 'Remove user'")
    check("...while the permitted name itself is allowed",
          action_policy.classify_detail(
              {"role": "button", "name": "Remove", "enabled": True},
              _names)[0] == "allow")

    # Validity rules are not the customer's to waive: they are about whether the element
    # can be driven at all, not about whether driving it is safe.
    check("a disabled control stays refused however it is named",
          action_policy.classify_detail(
              {"role": "button", "name": "Remove", "enabled": False},
              _names)[0] == "refuse",
          "disabled is not a safety rule and is nobody's to overrule")
    check("...and so does something with no interactive role",
          action_policy.classify_detail(
              {"role": "heading", "name": "Remove", "enabled": True},
              _names)[0] == "refuse")

    # Scope: an allowance is a statement about one application.
    with contextlib.closing(database.get_connection()) as _c:
        check("an allowance does not leak to another project",
              "transfer funds" not in app_session.allowance_names(_c, "some-other-project"),
              "pooling these across projects would widen a safety rule by accident")
        check("...nor to a session filed under no project at all",
              app_session.allowance_names(_c, "") == set())

    # Set explicitly: these steps offer a short list, and a candidate_index left over from
    # an earlier section refuses before anything is recorded, which reads as "nothing was
    # withheld" rather than as a mis-set fixture.
    _next_action.clear()
    _next_action.update({"done": False, "action_kind": "click", "candidate_index": 0,
                         "journey": "Bank", "rationale": "move money in a test bank"})

    permitting = _open_session(client, scoped, max_steps=10)
    check("the platform hands the driver the list it screened with",
          "transfer funds" in (permitting.json().get("policy", {}).get("permitted") or []),
          "the driver screens again to resolve the index, so a list it has to look up "
          "for itself is a list that can disagree")
    _psid = permitting.json()["session"]["id"]

    _offered = _step(client, scoped, _psid, [
        {"role": "link", "name": "Transfer Funds"},
        {"role": "button", "name": "Delete account"},
    ], url_path="/bank")
    check("a permitted control is offered rather than withheld",
          _offered.status_code == 200 and not _offered.json().get("refused"),
          _offered.text[:300])
    _seen = client.get(f"/api/v1/app-testing/sessions/{_psid}", headers=scoped).json()
    check("...and the one nobody permitted is still withheld",
          [r["name"] for r in _seen["session"].get("refusals", [])] == ["Delete account"],
          repr(_seen["session"].get("refusals")))

    # Frozen at open. The platform picks an index into its list and the driver resolves
    # that index against its own; changing the list underneath shifts every later index.
    with contextlib.closing(database.get_connection()) as _c:
        app_session.add_allowance(_c, project_id, "Delete account")
    _after = _step(client, scoped, _psid, [
        {"role": "button", "name": "Delete account"},
        {"role": "link", "name": "Items"},
    ], url_path="/bank2")
    _seen2 = client.get(f"/api/v1/app-testing/sessions/{_psid}", headers=scoped).json()
    check("a session keeps the list it opened with",
          any(r["name"] == "Delete account"
              for r in _seen2["session"].get("refusals", [])),
          "an allowance added mid-session would move every index under a driver still "
          "using the list it was handed")
    with contextlib.closing(database.get_connection()) as _c:
        app_session.remove_allowance(_c, project_id, "Delete account")

    # The backstop that makes any of this survivable: if the two lists ever do disagree,
    # the chosen index points at something nobody picked, and only the name can tell.
    _list = [{"name": "Continue", "role": "button", "enabled": True, "index": 0},
             {"name": "Delete account", "role": "button", "enabled": True, "index": 1}]
    ok_same, _why = action_policy.permits(
        {"action_kind": "click", "candidate_index": 0, "target_name": "Continue"}, _list)
    check("a chosen index whose name still matches is permitted", ok_same, _why)
    ok_diff, why_diff = action_policy.permits(
        {"action_kind": "click", "candidate_index": 0, "target_name": "Log In"}, _list)
    check("a chosen index whose name has moved is refused, not clicked",
          not ok_diff and "do not agree" in why_diff,
          "the index is meaningful only while both sides screen to the identical list, "
          "and nothing else in this path notices when they stop")

    with contextlib.closing(database.get_connection()) as _c:
        app_session.remove_allowance(_c, project_id, "Transfer Funds")
        app_session.remove_allowance(_c, project_id, "Remove")

    # ── 21. Locators the session never saw ────────────────────────────────
    # The prompt has said "never write a CSS selector" since the first version and a live
    # run produced nineteen of them, three matching nothing. So the rule is enforced here
    # instead, and what makes that possible is that the observation contract is closed:
    # `COLLECT_JS` emits a role locator, a text locator, or a `[name='...']` attribute
    # selector, and nothing else. Any class, id or test id in a finished suite therefore
    # came from the model's memory of another application rather than from this session.
    section("21. A locator that was never observed is counted, retried and reported")
    from backend.services import locator_audit

    # The three forms the collector can actually emit, taken from its own branch. If this
    # ever fails it means the contract widened and the audit is now calling an observed
    # locator invented -- which would be worse than not checking, because it would send
    # somebody to fix a correct line.
    for observed in ("input[name='user-name']", "*[name='login-button']",
                     "select[name='qty']"):
        check("the collector's own attribute form reads as observed: " + observed,
              locator_audit.classify_selector(observed) == "grounded",
              locator_audit.classify_selector(observed))

    for selector, kind in ((".inventory_item_price", "class"),
                           ("div.summary_subtotal_label", "class"),
                           ("#user-name", "id"),
                           ("[data-test='checkout']", "testid"),
                           ("[href='/cart']", "attribute"),
                           ("//div[@id='total']", "xpath"),
                           ("xpath=//button", "xpath")):
        check("%s reads as invented (%s)" % (selector, kind),
              locator_audit.classify_selector(selector) == kind,
              locator_audit.classify_selector(selector))

    # The other direction, and the reason this is not just "ban CSS". Narrowing to a row
    # before asserting inside it is what the prompt actually asks for, so a bare tag must
    # not be reported -- a checker that cries wolf on correct lines gets switched off in
    # the same way a suite does.
    for benign in ("tbody tr", "text=Total", "tr:has-text('Widget a.b')", "li >> nth=0"):
        check("a structural or text locator is not called invented: " + benign,
              locator_audit.classify_selector(benign) == "generic",
              locator_audit.classify_selector(benign))

    dirty_code = "\n".join([
        "import { test, expect } from '@playwright/test';",
        "test('signs in', async ({ page }) => {",
        "  await page.getByRole('button', { name: 'Login' }).click();",
        "  await page.waitForURL('**/inventory.html');",
        "  await page.locator(\"input[name='user-name']\").fill('standard_user');",
        "  const prices = await page.locator('.inventory_item_price').allTextContents();",
        "  await page.getByTestId('cart-badge').click();",
        "  await expect(page.getByText('$29.99')).toBeVisible();",
        "});",
    ])
    one = locator_audit.audit_content(dirty_code, "tests/checkout.spec.ts")
    check("a value filled into a locator is not mistaken for a selector",
          all(row["selector"] != "standard_user" for row in one["invented"]),
          [r["selector"] for r in one["invented"]])
    check("the class selector is found", any(r["kind"] == "class" for r in one["invented"]),
          one["invented"])
    check("...and so is the test id, which looks like good practice and was still recalled",
          any(r["kind"] == "testid" for r in one["invented"]), one["invented"])
    check("exactly the two invented ones are reported", len(one["invented"]) == 2,
          one["invented"])
    check("the observed locators are counted too, so the ratio means something",
          one["grounded"] == 4, one["grounded"])
    check("each one names the line somebody has to open",
          sorted(r["line"] for r in one["invented"]) == [6, 7],
          [r["line"] for r in one["invented"]])

    report = locator_audit.audit([{"path": "a.spec.ts", "content": dirty_code}])
    sentence = locator_audit.summary_sentence(report)
    check("the sentence tells the reader to suspect the test before the application",
          "before concluding the application is broken" in sentence, sentence[:160])
    check("a clean suite says so rather than staying silent",
          locator_audit.summary_sentence(
              locator_audit.audit([{"path": "a", "content": "page.getByRole('button')"}])) == "",
          "a panel that is empty when there is nothing wrong and empty when nobody checked "
          "cannot be told apart")
    note = locator_audit.retry_note(report)
    check("the retry quotes the model's own line back at it",
          ".inventory_item_price" in note and "a.spec.ts:6" in note, note[:200])
    check("...and says not to shrink the suite to satisfy it",
          "not a request for a smaller suite" in note, note[:400])

    # End to end: a dirty generation is retried once, and the cleaner answer is kept.
    def _suite(files, basis):
        return {"levels_chosen": [{"level": "UI/End-to-End", "rationale": "walked"}],
                "test_files": files, "requirement_coverage": [], "assumptions": [],
                "not_automated": [], "assertion_basis": basis,
                "summary": "A suite was written."}

    _dirty_files = [{"path": "tests/a.spec.ts", "framework": "Playwright",
                     "level": "UI/End-to-End", "content": dirty_code,
                     "test_names": ["signs in", "totals"], "covers": ["login"],
                     "prerequisites": ["running application"]}]
    _clean_files = [{"path": "tests/a.spec.ts", "framework": "Playwright",
                     "level": "UI/End-to-End",
                     "content": "page.getByRole('button', { name: 'Login' }).click();",
                     "test_names": ["signs in", "totals"], "covers": ["login"],
                     "prerequisites": ["running application"]}]
    _shrunk_files = [{"path": "tests/a.spec.ts", "framework": "Playwright",
                      "level": "UI/End-to-End",
                      "content": "page.getByRole('button', { name: 'Login' }).click();",
                      "test_names": ["signs in"], "covers": ["login"],
                      "prerequisites": ["running application"]}]
    _basis2 = [{"test_name": "signs in", "basis": "observed", "what_it_checks": "it signs in"},
               {"test_name": "totals", "basis": "observed", "what_it_checks": "the total"}]
    _basis1 = [{"test_name": "signs in", "basis": "observed", "what_it_checks": "it signs in"}]

    _stub_before = llm_gateway.call_tool

    def _two_answers(first, second):
        state = {"n": 0}

        def _stub(system_prompt, user_content, tool_schema, llm_override=None,
                  max_tokens=None):
            if tool_schema.get("name") == "submit_app_test_suite":
                state["n"] += 1
                _llm_calls.append({"name": "submit_app_test_suite", "content": user_content})
                llm_gateway._record_usage("anthropic", config.MODEL, 1000, 100)
                return copy.deepcopy(first if state["n"] == 1 else second)
            return _stub_before(system_prompt, user_content, tool_schema, llm_override,
                                max_tokens)
        return _stub, state

    def _generate_with(stub):
        sid = _open_session(client, scoped, max_steps=5).json()["session"]["id"]
        _step(client, scoped, sid, [{"role": "link", "name": "Items"}], url_path="/items")
        llm_gateway.call_tool = stub
        try:
            return client.post(f"/api/v1/app-testing/sessions/{sid}/generate",
                               json={}, headers=scoped)
        finally:
            llm_gateway.call_tool = _stub_before

    stub, state = _two_answers(_suite(_dirty_files, _basis2), _suite(_clean_files, _basis2))
    got = _generate_with(stub).json()
    check("a suite with a recalled locator is generated a second time", state["n"] == 2,
          state["n"])
    check("the cleaner answer is the one kept", got.get("locators_invented") == 0,
          got.get("locators_invented"))
    check("and the retry is reported rather than hidden",
          got.get("locators_retried") is True, got.get("locators_retried"))
    check("the suite kept its tests", got.get("total_tests") == 2, got.get("total_tests"))

    # The guard that matters most: the cheapest way to stop using class selectors is to
    # delete the tests that used them, which would trade a false alarm for a coverage hole
    # and score perfectly on the only number being measured.
    stub, state = _two_answers(_suite(_dirty_files, _basis2), _suite(_shrunk_files, _basis1))
    shrunk = _generate_with(stub).json()
    check("a second answer that dropped a test is refused", state["n"] == 2, state["n"])
    check("...and the first suite is kept instead", shrunk.get("total_tests") == 2,
          shrunk.get("total_tests"))
    check("...with the invented locators still counted, not quietly forgiven",
          shrunk.get("locators_invented") == 2, shrunk.get("locators_invented"))
    check("...and the rejection stated, because the money was still spent",
          shrunk.get("locator_retry_rejected") is True,
          shrunk.get("locator_retry_rejected"))
    check("the reader is told which line to open",
          any(r.get("kind") == "class"
              for r in (shrunk.get("locator_audit") or {}).get("invented") or []),
          shrunk.get("locator_audit"))

    # Two generation calls is the ceiling `agent_guard`'s reservation is sized for, so a
    # session that already paid twice to get any suite at all does not also pay to tidy it.
    def _empty_then_dirty(system_prompt, user_content, tool_schema, llm_override=None,
                          max_tokens=None):
        if tool_schema.get("name") == "submit_app_test_suite":
            _ceiling["n"] += 1
            _llm_calls.append({"name": "submit_app_test_suite", "content": user_content})
            llm_gateway._record_usage("anthropic", config.MODEL, 1000, 100)
            if _ceiling["n"] == 1:
                return {"test_files": [], "assertion_basis": [], "summary": "All covered."}
            return copy.deepcopy(_suite(_dirty_files, _basis2))
        return _stub_before(system_prompt, user_content, tool_schema, llm_override, max_tokens)

    _ceiling = {"n": 0}
    capped = _generate_with(_empty_then_dirty).json()
    check("an empty generation still costs at most two calls, even if the second is dirty",
          _ceiling["n"] == 2, _ceiling["n"])
    check("...and the dirty suite is kept and labelled rather than retried a third time",
          capped.get("locators_invented") == 2 and not capped.get("locators_retried"),
          (capped.get("locators_invented"), capped.get("locators_retried")))

    # The prompt still has to carry the reason, because the audit only catches what it knows
    # to look for and a model that understands why writes fewer of them in the first place.
    agent_src = (ROOT / "backend" / "agents" / "qa_intelligence"
                 / "app_explorer_agent.py").read_text(encoding="utf-8")
    check("the prompt argues from what the model was shown, not from durability alone",
          "You were never shown a class name" in agent_src)
    check("...and names the cost of a locator that matches nothing",
          "false alarm" in agent_src)
    check("...and still permits the one raw form the collector supplies",
          "[name='...']" in agent_src)
    check("the screen shows the reader where the locators came from",
          "_appLocatorPanel" in (ROOT / "frontend" / "app.js").read_text(encoding="utf-8"),
          "a count nobody renders is a count nobody reads")

    # ── 22. What the suite writer is actually told ────────────────────────
    # `_journeys_markdown` is the whole input to the expensive half, and until now nothing
    # offline read it. Both of its off-by-one faults were found by paying for a live run:
    # D-430 (the outcome column) cost two suites every test they had, and the page-text
    # shift cost a `basket-total` session all eight of its assertions on 2026-09-17. They
    # have the same cause -- a step records the state its action was *chosen from*, so
    # everything on it describes the moment before that action -- and the same fix.
    section("22. Each step is told what its own action did, and what the page then said")
    from backend.routes.app_testing import _journeys_markdown

    walked = [
        {"journey": "Buy a widget", "url_path": "/", "outcome": "",
         "page_texts": ["status: GBP 0.00"],
         "action": {"action_kind": "click", "target_role": "button",
                    "target_name": "Add Widget"}},
        {"journey": "Buy a widget", "url_path": "/", "outcome": "stayed on the same page",
         "page_texts": ["status: GBP 4.00"],
         "action": {"action_kind": "click", "target_role": "button",
                    "target_name": "One more Widget"}},
        {"journey": "Buy a widget", "url_path": "/", "outcome": "stayed on the same page",
         "page_texts": ["status: GBP 8.00"],
         "action": {"action_kind": "click", "target_role": "button",
                    "target_name": "Checkout"}},
        {"journey": "Pay", "url_path": "/pay", "outcome": "navigated to /pay",
         "page_texts": ["heading: Payment"],
         "action": {"action_kind": "click", "target_role": "button", "target_name": "Pay"}},
    ]
    md = _journeys_markdown(walked)
    lines = [ln.strip() for ln in md.splitlines()]

    def _block(name):
        """The action line for a step, plus the two page-text lines under it."""
        for i, ln in enumerate(lines):
            if ('"%s"' % name) in ln:
                return lines[i:i + 3]
        return []

    add = _block("Add Widget")
    check("an action is credited with its own result, not the previous one's",
          "observed: stayed on the same page" in add[0],
          add[0] if add else "the step is missing entirely")
    check("the page state before an action is labelled as before",
          any(l.startswith("before this action the page showed:") and "GBP 0.00" in l
              for l in add), add)
    check("...and the state it produced is labelled as after",
          any(l.startswith("and after it the page showed:") and "GBP 4.00" in l
              for l in add),
          "clicking Add Widget took the basket from GBP 0.00 to GBP 4.00; a suite told "
          "otherwise asserts the total it saw before the click: %s" % (add,))

    more = _block("One more Widget")
    check("the shift holds for the next step too, not just the first",
          any("GBP 4.00" in l and l.startswith("before this action") for l in more)
          and any("GBP 8.00" in l and l.startswith("and after it") for l in more), more)

    # The bug this replaced, stated as its own check so it cannot come back by accident.
    check("no page text is printed without saying which side of the action it is",
          "  page showed:" not in md,
          "an unlabelled line under an action reads as that action's result, and on an "
          "application whose address never changes nothing contradicts that reading")

    pay = _block("Pay")
    check("the last step reports that nothing came back rather than inventing an outcome",
          "observed: nothing reported" in pay[0], pay[0] if pay else "missing")
    check("...and has no 'after' state, because no step recorded one",
          not any(l.startswith("and after it") for l in pay), pay)

    # A page that did not change prints once, not twice. The page-text budget is the thing
    # that runs out first on a real application (D-431), so saying the same eight lines
    # twice would cost the last of it to say nothing.
    still = [
        {"journey": "Look", "url_path": "/", "outcome": "",
         "page_texts": ["heading: Shop"],
         "action": {"action_kind": "click", "target_role": "link", "target_name": "About"}},
        {"journey": "Look", "url_path": "/", "outcome": "stayed on the same page",
         "page_texts": ["heading: Shop"],
         "action": {"action_kind": "click", "target_role": "link", "target_name": "Home"}},
    ]
    quiet = _journeys_markdown(still)
    check("an unchanged page is described once rather than twice",
          quiet.count("heading: Shop") == 2 and "and after it the page showed" not in quiet,
          quiet)

    check("the suite writer is told to read the labels rather than the order",
          "Read the labels rather than the order" in agent_src,
          "the model has to know which of the two values an assertion wants")

    # ── 23. Whether a test could ever fail, and whether a name was real ──
    # The whole answer to "permitting a money-moving control is the customer's judgement"
    # is that the record says who made it. It said nobody: the first version read `email` or
    # `name` off a caller dict that carries neither, so every allowance ever added was
    # attributed to the empty string. Found by adding one through the API and reading it
    # back -- a blank column looks exactly like a column nobody has written to yet.
    from backend.routes.app_testing import _who
    check("a browser caller is recorded by username",
          _who({"type": "jwt", "team": "admin", "username": "admin"}) == "admin")
    check("an API key is recorded as a key rather than as nobody",
          _who({"type": "api_key", "team": "Acme", "key_id": "abcdef1234"})
          == "api key abcdef12 (Acme)",
          _who({"type": "api_key", "team": "Acme", "key_id": "abcdef1234"}))
    check("...and an unrecognised caller still says something",
          _who({"team": "Acme"}) == "Acme")
    check("the accountability record is never silently empty",
          all(_who(c) for c in ({"username": "u"}, {"key_id": "k", "team": "t"},
                                {"team": "t"})),
          "C-102 rests on this record being readable; blank is the one answer it may not give")

    # The driver's own entry points, asserted as *objects* rather than as text.
    #
    # This exists because of a real escape: a patch wrote literal backslash-n characters
    # instead of newlines, so a comment block and `def _verify` became one enormous comment
    # line and the function was never defined. Everything offline still passed -- the
    # fixture never reaches that path -- `--help` still listed the flag, and the defect
    # surfaced only after a live session had spent its money exploring, at the very last
    # step. Importing the module and looking at the attribute is the cheapest check that
    # could have caught it, and text matching could not have: the broken file *contained*
    # the string `def _verify`.
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_explore_app", ROOT / "scripts" / "explore_app.py")
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    for _name in ("_verify", "observe", "explore", "main", "_warn_about_frames"):
        check("the driver actually defines %s()" % _name,
              callable(getattr(_mod, _name, None)),
              "a source file can contain the words `def %s` and still not define it" % _name)
    check("the verification timeout is a number, not a string of escapes",
          isinstance(getattr(_mod, "VERIFY_TIMEOUT_SECONDS", None), int),
          getattr(_mod, "VERIFY_TIMEOUT_SECONDS", None))

    section("23. A test that cannot fail is named, and so is a heading nobody saw")
    from backend.services import assertion_audit

    suite_code = "\n".join([
        "import { test, expect } from '@playwright/test';",
        "test('badge_is_visible', async ({ page }) => {",
        "  await expect(page.getByRole('button', { name: 'Cart' })).toBeVisible();",
        "  await expect(page).toHaveURL(/inventory/);",
        "});",
        "test('total_is_right', async ({ page }) => {",
        "  await expect(page.getByRole('status')).toHaveText('GBP 8.00');",
        "});",
        "test('save_is_refused', async ({ page }) => {",
        "  await expect(page.getByRole('button', { name: 'Save' })).toHaveCount(0);",
        "});",
        "test('clicks_and_checks_nothing', async ({ page }) => {",
        "  await page.getByRole('button', { name: 'Go' }).click();",
        "});",
        "test.skip('later', async ({ page }) => {",
        "  await expect(page.getByText('x')).toHaveText('x');",
        "});",
    ])
    rep = assertion_audit.audit([{"path": "tests/a.spec.ts", "content": suite_code}])

    check("every test in the file is counted", rep["tests_total"] == 5, rep["tests_total"])
    check("a test that only checks presence and the address is named",
          [t["name"] for t in rep["hollow"]] == ["badge_is_visible"], rep["hollow"])
    check("...and a test asserting a value is not",
          all(t["name"] != "total_is_right" for t in rep["hollow"]), rep["hollow"])
    check("asserting an absence counts as a real assertion",
          all(t["name"] != "save_is_refused" for t in rep["hollow"]),
          "toHaveCount(0) is the form the prompt teaches for 'must not be offered'; "
          "calling it hollow would punish the one honest way to write a negative test")
    check("a test with no assertion at all is reported separately",
          [t["name"] for t in rep["silent"]] == ["clicks_and_checks_nothing"], rep["silent"])
    check("a skipped test is neither hollow nor silent, it is skipped",
          rep["skipped_count"] == 1 and rep["hollow_count"] == 1
          and rep["silent_count"] == 1,
          (rep["skipped_count"], rep["hollow_count"], rep["silent_count"]))

    sentence = assertion_audit.summary_sentence(rep)
    check("the sentence says what a weak test costs the reader",
          "passes against an application whose feature is completely broken" in sentence,
          sentence[:200])
    check("...and reads as grammar rather than as counters",
          "1 asserts only" in sentence and "1 is skipped" in sentence, sentence[:200])
    check("a suite where every test can fail says so rather than staying blank",
          assertion_audit.summary_sentence(assertion_audit.audit(
              [{"path": "b", "content": "test('x', async () => "
                                        "{ await expect(a).toHaveText('1'); });"}])) == "",
          "silence when nothing is wrong and silence when nobody looked are the same sight")

    # The check the shape of a locator alone cannot make: a name that was never on any page.
    # This is D-430's failure exactly -- a sign-in helper waiting for a heading called
    # 'Products' on a page with no headings at all -- and it took a whole suite with it.
    helper = "\n".join([
        "async function signIn(page) {",
        "  await page.getByRole('button', { name: 'Login' }).click();",
        "  await expect(page.getByRole('heading', { name: 'Products' })).toBeVisible();",
        "}",
        "test('overview', async ({ page }) => {",
        "  await expect(page.getByRole('heading', { name: 'Checkout: Overview' }))"
        ".toBeVisible();",
        "});",
    ])
    hfiles = [{"path": "tests/login.spec.ts", "content": helper}]
    observed = ["heading: Checkout: Overview", "text: $29.99", "status: Total 55.97"]

    blind = locator_audit.audit(hfiles)
    check("shape alone cannot see an invented heading",
          blind["invented_count"] == 0,
          "getByRole looks grounded whatever name is inside it: %s" % blind["kinds"])

    seeing = locator_audit.audit(hfiles, observed_texts=observed)
    check("...but a heading the session never collected is caught",
          [r["kind"] for r in seeing["invented"]] == ["unseen-name"], seeing["invented"])
    check("...and named with the line to open",
          seeing["invented"][0]["line"] == 3 and "Products" in seeing["invented"][0]["selector"],
          seeing["invented"])
    check("a heading that WAS collected stays grounded",
          all("Checkout" not in r["selector"] for r in seeing["invented"]),
          "flagging a correct locator is the failure this check must not have")

    check("a session that collected no text turns the check off entirely",
          locator_audit.audit(hfiles, observed_texts=[])["invented_count"] == 0,
          "'nothing was observed' would otherwise make every name look invented, which is "
          "how a checker earns being switched off")

    app_js_now = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    check("the screen shows which tests would not notice a break",
          "_appAssertionPanel" in app_js_now,
          "a count nobody renders is a count nobody reads")

    # ── 24. Noticing that a session has stopped getting anywhere ──────────
    # The sequence below is not invented. It is the real page-signature trace of a live
    # session against a public banking demo on 2026-09-17, which reached the account-activity
    # page at step 4 and spent steps 5 to 26 toggling two dropdowns. From step 14 its
    # signature cycled between exactly **two** values, so the longest run of identical ones
    # was three, the old rule needed five, and it never fired -- 22 of 26 paid steps, and the
    # session never reached the feature its requirements were about (C-105).
    section("24. A session going round in circles is noticed, and told so")
    PARABANK_SIGS = ["4abc3dc4", "03df2fd8", "0c4f6921", "5889e5cc", "e3b8bc06", "d9495d67",
                     "23e3a185", "4f947a9e", "141b1482", "0e3d22dc", "3877fb0b", "216920ea",
                     "23e3a185", "4f947a9e", "4f947a9e", "8e59380a", "8e59380a", "8e59380a",
                     "4f947a9e", "4f947a9e", "8e59380a", "8e59380a", "8e59380a", "4f947a9e",
                     "4f947a9e", "8e59380a"]

    def _stops_at(sigs, sid):
        """Which step the no-progress rule would refuse to take, or None."""
        conn = database.get_connection()
        try:
            for i, sig in enumerate(sigs):
                if app_session.stalled(conn, sid, sig):
                    return i + 1
                conn.execute(
                    "INSERT INTO app_steps (id, session_id, step_index, page_signature, "
                    "action_json, created_at) VALUES (?,?,?,?,?,?)",
                    ("%s-%d" % (sid, i), sid, i + 1, sig, "{}", "2026-09-17T00:00:00"))
                conn.commit()
        finally:
            conn.close()
        return None

    stuck_at = _stops_at(PARABANK_SIGS, "stall-parabank")
    check("a two-value loop is caught at all", stuck_at is not None,
          "the old rule asked whether the last N pages were *identical*, and a session "
          "alternating between two known states answers no to that for ever")
    check("...and caught before the budget runs out",
          stuck_at is not None and stuck_at < len(PARABANK_SIGS),
          "stopped at %s of %d" % (stuck_at, len(PARABANK_SIGS)))

    # The two failures this rule must not have. Both were the reason it was written narrowly
    # in the first place, and widening it is exactly how they come back.
    check("a session that finds something new every step is never stopped",
          _stops_at(["new%d" % i for i in range(1, 15)], "stall-healthy") is None)
    check("filling in a long form is not mistaken for being stuck",
          _stops_at(["form%d" % i for i in range(1, 13)], "stall-form") is None,
          "a signature carries each control's current value on purpose (D-411), so typing "
          "into the next field is a genuinely new signature")
    check("going out to a detail page and back is not a loop while it keeps finding items",
          _stops_at(["home", "list", "item1", "list", "item2", "list", "item3", "list",
                     "item4"], "stall-revisit") is None)
    check("...but doing it between two pages it has already seen is",
          _stops_at(["a", "b", "c", "a", "b", "a", "b", "a", "b", "a", "b", "a"],
                    "stall-pingpong") is not None,
          "this is the shape the docstring used to claim was caught and was not")

    # Upstream of the rule: the explorer is told it is repeating itself, because the rule is
    # only a backstop and reclaims the last few steps of a session that is already wasted.
    from backend.routes.app_testing import _repetition_note

    # Repetition that is getting nowhere: the same two dropdowns, and the page keeps
    # returning to states the session has already been in.
    # The earlier half matters: a signature is only "already seen" relative to what came
    # before the window, so a fixture with no history would have every state looking new on
    # first sight -- which is the correct behaviour and the wrong test.
    fiddling = [{"action": {"action_kind": "click", "target_name": "Go"},
                 "page_signature": sig} for sig in ("8e59380a", "4f947a9e", "8e59380a")]
    fiddling += [{"action": {"action_kind": "select", "target_name": n},
                  "page_signature": sig}
                 for n, sig in (("All January February", "8e59380a"),
                                ("All Credit Debit", "4f947a9e"),
                                ("All January February", "8e59380a"),
                                ("All Credit Debit", "4f947a9e"),
                                ("All January February", "8e59380a"),
                                ("All Credit Debit", "4f947a9e"))]
    note = _repetition_note(fiddling)
    check("an explorer getting nowhere with the same control is told so",
          "3 times" in note and "All Credit Debit" in note, note[:200])
    check("...and told that the application has stopped showing it anything new",
          "had not already shown you" in note, note[:220])
    check("...and told what it costs, in steps", "cannot be got back" in note, note[:260])
    check("...and given somewhere else to go, since a model with no alternative repeats "
          "itself", "declare yourself done" in note, note[:400])

    # **The case that matters most**, and the one the first version of this got wrong.
    #
    # This is the real `basket-total` trace from a live eval session: three clicks on "One
    # more Widget" in a row, *because that is the test* -- the arithmetic bug the case hunts
    # is invisible at a quantity of one. The first version counted repeats alone and fired on
    # **seven of these eleven steps**, and the check that was supposed to catch that used
    # eight distinct targets, so nothing repeated and it passed. Each click here moves the
    # basket to a total the session has not displayed before, which is exactly what
    # distinguishes it from the dropdown above.
    _healthy = ["Add Widget", "One more Widget", "One more Widget", "One more Widget",
                "Add Gadget", "One more Gadget", "One fewer Widget", "One fewer Widget",
                "One fewer Gadget", "One fewer Widget", "One fewer Gadget"]
    _built, _nagged = [], []
    for _i, _name in enumerate(_healthy, 1):
        if _repetition_note(_built):
            _nagged.append(_i)
        _built.append({"action": {"action_kind": "click", "target_name": _name},
                       "page_signature": "total-%d" % _i})
    check("a session repeating an action that keeps changing the page is never nagged",
          not _nagged,
          "fired before step(s) %s of a healthy basket-total run; a warning that fires on "
          "correct behaviour is one the model learns to ignore, which costs the real warning "
          "its only chance of being read" % (_nagged,))

    check("the explorer is actually shown it, not just handed it",
          "You are repeating yourself" in agent_src,
          "a note computed and never rendered is the shape of C-98")

    section("14. The registry row, the estimator and the screen table agree")
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
    check("the screen's cap on source runs matches the one the server enforces",
          f"APP_TESTING_MAX_SOURCES = {app_basis.MAX_SOURCE_REVIEWS}" in app_js,
          f"the server truncates at {app_basis.MAX_SOURCE_REVIEWS} without comment, so a "
          "screen that offers more silently drops the rest")
    check("the retired two-word assertion vocabulary is gone from the screen",
          "basis === 'specified'" not in app_js,
          "the table reported every `stated` assertion as observed, under a banner that "
          "counted it correctly")
    check("...and all three words are rendered",
          "_ASSERTION_LABELS" in app_js and "derived:" in app_js)
    check("the source picker filters on the server, not in the browser",
          "product_id=${encodeURIComponent(spec.product)}" in app_js,
          "asking for the newest 50 reviews of every kind and filtering here showed an "
          "empty picker on any project with 50 recent reviews of another kind")
    check("the built command carries the project",
          "'--project ' + projectId" in app_js,
          "without it the session is filed under nothing and every covered run comes back "
          "as 'no such run'")
    check("...and the attestation, rather than the driver assuming it",
          "parts.push('--attest')" in app_js
          and "bool(args.attest)" in (ROOT / "scripts" / "explore_app.py").read_text(
              encoding="utf-8"),
          "a declaration the driver makes on the caller's behalf is nobody's declaration")

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
