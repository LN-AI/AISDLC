"""Offline smoke test for admin-managed LLM connections.

Run it:

    python scripts/smoke_llm_providers.py

Covers, in order:

  1. CRUD over `/api/v1/llm-providers/connections`, including the two things that are
     easy to get wrong: the plaintext key must never come back out, and an edit that
     doesn't re-enter the key must not wipe it.
  2. Authorization — a non-super-admin can see *which* LLM is serving calls but cannot
     read, create or change credentials.
  3. Resolution order. This is the point of the whole change: a browser call has no
     override of its own, so it has to pick up the active connection, while a caller who
     brought their own LLM must still win.
  4. That a real UI review call lands on the active connection's endpoint — end to end
     through `/api/review/code`, not just at the resolver.
  5. That `_require_llm()` accepts an active connection, so ANTHROPIC_API_KEY can be
     empty, and 503s when there is genuinely nothing to call.

Same conventions as the suites beside it (D-09): plain script, no framework, exits 0 or
1, driven through `TestClient` against a throwaway SQLite file, no server, no API key and
no network.

Unlike the other suites this one stubs the *provider* functions (`_call_anthropic` /
`_call_openai`) rather than `call_tool` itself — the resolution logic under test lives
inside `call_tool`, so stubbing it would stub out the thing being tested.
"""
import contextlib
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP_DB = os.path.join(tempfile.gettempdir(), f"smoke_llm_providers_{uuid.uuid4().hex}.db")
os.environ["DATABASE_PATH"] = _TMP_DB
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-smoke-test-placeholder")
os.environ["LEGACY_ROUTES_REQUIRE_AUTH"] = "false"
os.environ["UI_REQUIRE_PROJECT"] = "false"

from backend import config  # noqa: E402

config.DATABASE_PATH = _TMP_DB

from backend.services import llm_gateway  # noqa: E402

# ── Provider stubs ─────────────────────────────────────────────────────────
# Each records exactly what credentials/endpoint it was handed, which is what the
# resolution checks below assert on.
_sent = []


def _record(provider, api_key, base_url, model, max_tokens, timeout):
    _sent.append({
        "provider": provider, "api_key": api_key, "base_url": base_url,
        "model": model, "max_tokens": max_tokens, "timeout": timeout,
    })
    llm_gateway._record_usage(provider, model or "?", 1000, 100)
    return {
        "findings": [], "risk_score": 5.0, "gate_status": "Green",
        "summary": "stubbed", "recommendations": [], "ok": True,
    }


def _stub_anthropic(api_key, base_url, model, system_prompt, user_content, tool_schema,
                    max_tokens=None, timeout=None):
    return _record("anthropic", api_key, base_url, model, max_tokens, timeout)


def _stub_openai(api_key, base_url, model, system_prompt, user_content, tool_schema,
                 max_tokens=None, timeout=None):
    return _record("openai", api_key, base_url, model, max_tokens, timeout)


llm_gateway._call_anthropic = _stub_anthropic
llm_gateway._call_openai = _stub_openai

from fastapi.testclient import TestClient  # noqa: E402

import backend.database as database  # noqa: E402
import backend.main as main  # noqa: E402
from backend.services import llm_settings_service  # noqa: E402

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


OMNI = {
    "name": "OmniRoute local",
    "provider": "openai",
    "preset": "omniroute",
    "api_key": "sk-omni-secret-value-12345",
    "base_url": "http://localhost:20128/api/v1",
    "model": "auto/best-fast",
    "timeout_seconds": 90,
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

    made = client.post("/api/auth/users", headers=admin, json={
        "username": "dev1", "password": "Devpass@1234", "role": "developer", "email": "dev1@example.com",
    })
    check("a non-admin user can be created", made.status_code in (200, 201), made.text[:300])
    dev_login = client.post("/api/auth/login", json={"username": "dev1", "password": "Devpass@1234"})
    dev = {"Authorization": f"Bearer {dev_login.json()['token']}"}

    # ── 1. CRUD ────────────────────────────────────────────────────────────
    section("1. Connections — create, read, edit, delete")

    presets = client.get("/api/v1/llm-providers/presets", headers=admin)
    check("presets are listed", presets.status_code == 200, presets.text[:200])
    preset_ids = {p["id"] for p in presets.json()["presets"]}
    check("the presets cover the providers we claim to support",
          {"anthropic", "omniroute", "ollama", "openai", "azure", "custom"} <= preset_ids,
          str(sorted(preset_ids)))
    omni_preset = next(p for p in presets.json()["presets"] if p["id"] == "omniroute")
    check("the OmniRoute preset keeps the /api/v1 suffix the SDK needs",
          omni_preset["base_url"].endswith("/api/v1"), omni_preset["base_url"])

    created = client.post("/api/v1/llm-providers/connections", headers=admin, json=OMNI)
    check("a connection can be created", created.status_code == 201, created.text[:300])
    conn = created.json()
    conn_id = conn["id"]

    check("the plaintext key is never returned",
          OMNI["api_key"] not in created.text, "the secret appeared in the response body")
    check("a masked form is returned instead",
          conn["api_key_masked"].startswith("sk-o") and "•" in conn["api_key_masked"],
          conn["api_key_masked"])
    check("has_api_key reports the credential is stored", conn["has_api_key"] is True)
    check("a new connection is not active until it is chosen", conn["is_active"] is False)

    dupe = client.post("/api/v1/llm-providers/connections", headers=admin, json=OMNI)
    check("a duplicate name is rejected", dupe.status_code == 400, dupe.text[:200])

    bad = client.post("/api/v1/llm-providers/connections", headers=admin,
                      json={**OMNI, "name": "Bad wire", "provider": "omniroute"})
    check("provider must be a wire format, not a vendor name", bad.status_code == 400, bad.text[:200])

    listed = client.get("/api/v1/llm-providers/connections", headers=admin)
    check("connections can be listed", listed.status_code == 200 and len(listed.json()["connections"]) == 1)
    check("the list never carries the plaintext key either", OMNI["api_key"] not in listed.text)

    # The edit form shows a mask, so submitting it unchanged sends an empty api_key. That
    # must mean "keep what's stored", or every rename would silently break the connection.
    edited = client.put(f"/api/v1/llm-providers/connections/{conn_id}", headers=admin,
                        json={**OMNI, "api_key": "", "model": "auto/best-coding"})
    check("an edit succeeds", edited.status_code == 200, edited.text[:300])
    check("the model was changed", edited.json()["model"] == "auto/best-coding")
    check("an edit with a blank key keeps the stored credential",
          edited.json()["has_api_key"] is True, "the credential was wiped by a routine edit")

    stored = database.get_llm_connection(conn_id)
    check("the key is encrypted at rest",
          stored["api_key_encrypted"] and OMNI["api_key"] not in stored["api_key_encrypted"],
          "the raw key is sitting in the database column")

    # ── 2. Authorization ───────────────────────────────────────────────────
    section("2. Authorization")

    check("a non-admin cannot list connections",
          client.get("/api/v1/llm-providers/connections", headers=dev).status_code == 403)
    check("a non-admin cannot create one",
          client.post("/api/v1/llm-providers/connections", headers=dev,
                      json={**OMNI, "name": "Sneaky"}).status_code == 403)
    check("a non-admin cannot change which one is active",
          client.put("/api/v1/llm-providers/active", headers=dev,
                     json={"connection_id": conn_id}).status_code == 403)
    check("an anonymous caller gets 401, not 403",
          client.get("/api/v1/llm-providers/connections").status_code == 401)
    check("a signed-in non-admin CAN see which LLM is serving their reviews",
          client.get("/api/v1/llm-providers/active", headers=dev).status_code == 200)

    # ── 3. Resolution order ────────────────────────────────────────────────
    section("3. Resolution order")

    check("with nothing active, the platform default is .env Anthropic",
          llm_gateway.platform_default() is None)
    summary = client.get("/api/v1/llm-providers/active", headers=admin).json()
    check("the summary says so", summary["active"] is False and summary["provider"] == "anthropic",
          str(summary))

    activated = client.put("/api/v1/llm-providers/active", headers=admin, json={"connection_id": conn_id})
    check("the connection can be made active", activated.status_code == 200, activated.text[:300])
    check("the summary now names it", activated.json()["name"] == OMNI["name"], activated.text[:200])

    resolved = llm_gateway.platform_default()
    check("platform_default() now returns the connection", resolved is not None)
    check("it decrypts the stored key", resolved["api_key"] == OMNI["api_key"])
    check("it carries the endpoint", resolved["base_url"] == OMNI["base_url"])
    check("it is tagged as a platform connection, not BYOL",
          resolved["source"] == "platform_connection", str(resolved.get("source")))

    check("a caller with no override of their own resolves to the active connection",
          (llm_gateway.resolve_override(None) or {}).get("model") == "auto/best-coding")

    byol = {"provider": "anthropic", "api_key": "sk-ant-caller-own", "model": "claude-sonnet-4-6"}
    check("a caller who brought their own LLM still wins",
          llm_gateway.resolve_override(byol)["api_key"] == "sk-ant-caller-own")
    with llm_gateway.with_override(byol):
        check("a BYOL contextvar also wins over the active connection",
              llm_gateway.resolve_override(None)["api_key"] == "sk-ant-caller-own")

    described = llm_gateway.describe(None)
    check("the audit blob names the connection that actually served the call",
          described["provider"] == "openai" and described["model"] == "auto/best-coding",
          str(described))

    # ── 4. End to end through the browser UI's own route ───────────────────
    section("4. A UI review call reaches the active connection")

    _sent.clear()
    review = client.post("/api/review/code", headers=admin,
                         json={"code": "def add(a, b): return a + b", "language": "python"})
    check("the UI review route answers 200", review.status_code == 200, review.text[:300])
    check("exactly one provider call was made", len(_sent) == 1, str(_sent))
    if _sent:
        call = _sent[0]
        check("it went to the OpenAI-compatible path, not Anthropic",
              call["provider"] == "openai", call["provider"])
        check("it used the connection's base_url", call["base_url"] == OMNI["base_url"], str(call))
        check("it used the connection's model", call["model"] == "auto/best-coding", str(call))
        check("it used the connection's key", call["api_key"] == OMNI["api_key"])
        check("the connection's timeout was applied", call["timeout"] == 90, str(call["timeout"]))

    # The UI routes meter through legacy_guard, not agent_guard, and it used to hardcode
    # config.MODEL / "anthropic" — so a review served by a local router was recorded in the
    # ledger as Claude and priced at Claude rates.
    events = database.get_connection().execute(
        "SELECT model, provider, billable FROM usage_events ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    check("the ledger names the model that actually served the UI call",
          events and events["model"] == "auto/best-coding" and events["provider"] == "openai",
          str(dict(events)) if events else "no usage_events row")
    check("...and it is still billable — a platform connection is the platform's own bill",
          events and events["billable"] == 1, str(dict(events)) if events else "")

    # The bug this guards: the OpenAI path used to default to 4096 while Anthropic used
    # config.MAX_TOKENS, so the same agent got half the room on an OpenAI-compatible
    # endpoint and raised LLMTruncatedResponseError instead of finishing.
    _sent.clear()
    llm_gateway.call_tool("sys", "user", {"name": "t", "description": "", "input_schema": {"type": "object"}})
    check("an agent that names no budget gets config.MAX_TOKENS, not a hardcoded 4096",
          _sent and _sent[0]["max_tokens"] == config.MAX_TOKENS, str(_sent))

    # A connection's max_tokens is a *ceiling*, not just a default. Agents ask for up to
    # 32000, which several OpenAI-compatible models refuse outright as an output cap
    # (gpt-4o: 16384) — a clamp truncates at worst, an unclamped request 400s the call.
    schema = {"name": "t", "description": "", "input_schema": {"type": "object"}}
    capped = {"provider": "openai", "api_key": "k", "model": "gpt-4o", "max_tokens": 16384}

    _sent.clear()
    llm_gateway.call_tool("sys", "user", schema, capped, max_tokens=32000)
    check("an agent's request above the connection's ceiling is clamped down to it",
          _sent and _sent[0]["max_tokens"] == 16384, str(_sent))

    _sent.clear()
    llm_gateway.call_tool("sys", "user", schema, capped, max_tokens=8000)
    check("...but a request below the ceiling is left alone, not raised to it",
          _sent and _sent[0]["max_tokens"] == 8000, str(_sent))

    _sent.clear()
    llm_gateway.call_tool("sys", "user", schema,
                          {"provider": "openai", "api_key": "k", "model": "gpt-4o"},
                          max_tokens=32000)
    check("a connection with no ceiling passes the agent's full budget through",
          _sent and _sent[0]["max_tokens"] == 32000, str(_sent))

    _sent.clear()
    llm_gateway.call_tool("sys", "user", schema, capped)
    check("an agent that names no budget still resolves to the ceiling, as before",
          _sent and _sent[0]["max_tokens"] == min(config.MAX_TOKENS, 16384), str(_sent))

    # ── 5. The 503 guard ───────────────────────────────────────────────────
    section("5. The 'no LLM configured' guard")

    original_key = config.ANTHROPIC_API_KEY
    try:
        config.ANTHROPIC_API_KEY = ""
        check("an active connection satisfies the guard with no ANTHROPIC_API_KEY",
              llm_settings_service.is_llm_configured() is True)
        empty_key_review = client.post("/api/review/code", headers=admin,
                                       json={"code": "x = 1", "language": "python"})
        check("...and a review still runs", empty_key_review.status_code == 200,
              empty_key_review.text[:300])

        client.put("/api/v1/llm-providers/active", headers=admin, json={"connection_id": None})
        check("with neither a key nor a connection, nothing is configured",
              llm_settings_service.is_llm_configured() is False)
        blocked = client.post("/api/review/code", headers=admin,
                              json={"code": "x = 1", "language": "python"})
        check("...and the route 503s", blocked.status_code == 503, blocked.text[:200])
        check("the message points at the settings page, not just .env",
              "LLM Providers" in blocked.text, blocked.text[:200])
    finally:
        config.ANTHROPIC_API_KEY = original_key

    # ── 6. Deleting the active connection ──────────────────────────────────
    section("6. Deleting the active connection")

    client.put("/api/v1/llm-providers/active", headers=admin, json={"connection_id": conn_id})
    check("it is active again", llm_gateway.platform_default() is not None)
    removed = client.delete(f"/api/v1/llm-providers/connections/{conn_id}", headers=admin)
    check("it can be deleted", removed.status_code == 200, removed.text[:200])
    check("deleting the active connection reverts to the platform default rather than "
          "leaving a dangling id", llm_gateway.platform_default() is None)
    check("the response says what happened to the active selection",
          removed.json()["active"]["active"] is False, removed.text[:200])
    check("deleting it twice is a 404",
          client.delete(f"/api/v1/llm-providers/connections/{conn_id}", headers=admin).status_code == 404)

    # ── 7. An unusable Anthropic connection must not black-hole every call ──
    section("7. A misconfigured connection degrades rather than breaks")

    keyless = client.post("/api/v1/llm-providers/connections", headers=admin, json={
        "name": "Anthropic no key", "provider": "anthropic", "preset": "anthropic",
        "api_key": "", "base_url": "", "model": "claude-sonnet-4-6",
    })
    check("a keyless Anthropic connection can be saved", keyless.status_code == 201, keyless.text[:200])
    client.put("/api/v1/llm-providers/active", headers=admin,
               json={"connection_id": keyless.json()["id"]})
    check("but it resolves to None, so calls fall back to .env instead of sending an empty key",
          llm_gateway.platform_default() is None)

    stack.close()

    print(f"\n{'─' * 60}")
    if _failures:
        print(f"FAILED — {len(_failures)} of {_checks} checks failed:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"OK — all {_checks} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        code = main_()
    finally:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(_TMP_DB + suffix)
    sys.exit(code)
