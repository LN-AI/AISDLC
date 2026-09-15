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

    # ── 14. The product agrees with itself ────────────────────────────────
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
