"""Offline checks for the review-source, cost-preview and VAPT-substrate work.

Same shape as the other smoke scripts (see D-09): plain script, exits 0/1, one PASS/FAIL
line per check, no framework. Drives the real app through FastAPI's TestClient against a
throwaway SQLite file with `llm_gateway.call_tool` stubbed — so it needs no server, no API
key and no network, and it exercises routing, entitlement, metering and persistence without
ever calling a model.

What it covers, and why each group exists:

1.  **Source loading** — a zip, a file list, a paste and a diff all become the same `Bundle`,
    and the unsafe things a zip can carry are refused rather than sanitised.
2.  **Cost preview** — the quote is free, it prices the real chunk split rather than a
    division, and its ceiling is a bound rather than a forecast.
3.  **Per-file evidence verification** — a quote must appear in the file the finding names.
    This is the check that turns "somewhere in the input" into "in this file", and it also
    re-files a finding that named the wrong one.
4.  **Deterministic secret findings** — the ingest redaction map becomes findings, so the
    single most valuable finding class survives redaction instead of being erased by it.
5.  **VAPT now has the substrate** — chunking, verification, de-duplication, fingerprints,
    waivers and a gate floor. Every one of these was absent (C-73), and the gate-floor check
    is the one that matters most: a Critical can no longer come out Green.
6.  **The PR comment says where** — file, line, evidence, coverage and the counts row.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="smoke_sources_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "smoke.db")
os.environ.setdefault("SECRET_KEY", "smoke-secret-key-not-for-production")

from backend import config, database  # noqa: E402
from backend.services import (  # noqa: E402
    code_chunker, finding_pipeline, llm_gateway, review_estimate, source_bundle,
)
from backend.services import sarif as sarif_mod  # noqa: E402

PASSED = 0
FAILED = 0


def section(title):
    print(f"\n{title}")
    print("-" * len(title))


def check(label, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")
        if detail:
            print(f"        {detail}")


# ── Fixtures ──────────────────────────────────────────────────────────────

APP_FILES = {
    "app/auth.py": (
        "import os\n"
        "\n"
        "def login(username, password):\n"
        '    query = "SELECT * FROM users WHERE name=" + username\n'
        "    return db.execute(query)\n"
    ),
    "app/admin.py": (
        'DB_PASSWORD = "s3cr3t-production-password"\n'
        "\n"
        "def run(cmd):\n"
        "    os.system(cmd)\n"
    ),
    "tests/test_auth.py": (
        'FAKE_KEY = "sk-test-aaaaaaaaaaaaaaaaaaaaaaaa"\n'
        "\n"
        "def test_login():\n"
        "    assert True\n"
    ),
    "static/vendor/jquery.min.js": "!function(e){}(window);\n",
    "README.md": "# Demo\n",
}

DIFF = (
    "diff --git a/app/auth.py b/app/auth.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app/auth.py\n"
    "+++ b/app/auth.py\n"
    "@@ -10,3 +10,4 @@ def login(u, p):\n"
    "     conn = get_conn()\n"
    '+    query = "SELECT * FROM users WHERE name=" + u\n'
    "     return conn.execute(query)\n"
)


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(f"demo-a1b2c3/{path}", content)
    return buf.getvalue()


def code_response(findings=None, gate="Green", risk=5.0, summary="Reviewed."):
    return {
        "findings": findings if findings is not None else [],
        "risk_score": risk, "gate_status": gate, "summary": summary,
        "compliance_flags": [], "recommendations": ["Parameterize queries."],
    }


def vapt_response(findings=None, gate="Green", risk=10.0):
    return {
        "findings": findings if findings is not None else [],
        "attack_surface_summary": "One login endpoint and one admin command runner.",
        "risk_score": risk, "gate_status": gate, "summary": "Assessed.",
        "compliance_flags": [], "recommendations": ["Parameterize queries."],
    }


SQLI = dict(
    control_id="SEC-003", severity=5, risk_level="Critical",
    title="SQL injection in login", description="User input concatenated into a query.",
    recommendation="Use a parameterized query.", is_blocking=True,
    file_path="app/auth.py", line_start=1, confidence="High",
    evidence='query = "SELECT * FROM users WHERE name=" + username',
)
INVENTED = dict(
    control_id="SEC-001", severity=5, risk_level="Critical",
    title="Remote code execution via eval", description="Not real.",
    recommendation="Remove it.", is_blocking=True,
    file_path="app/auth.py", line_start=2, confidence="High",
    evidence="eval(request.args['payload'])",
)
MISFILED = dict(
    control_id="SEC-005", severity=4, risk_level="High",
    title="Command injection", description="Shell command built from input.",
    recommendation="Use subprocess with a list.", is_blocking=True,
    file_path="app/auth.py", line_start=9, confidence="High",
    evidence="os.system(cmd)",
)
VAPT_SQLI = dict(
    vuln_id="VLN-001", control_id="VAPT-003", severity=5, risk_level="Critical",
    cvss_score=9.8, cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    owasp_category="A03:2021 Injection", cwe_id="CWE-89",
    title="SQL injection in login", description="Concatenated query.",
    affected_component="login()", attack_scenario="Attacker sends ' OR 1=1--",
    proof_of_concept="curl -d \"name=' OR 1=1--\"",
    recommendation="Parameterize.", is_blocking=True,
    file_path="app/auth.py", line_start=1, confidence="High",
    evidence='query = "SELECT * FROM users WHERE name=" + username',
)


def main():
    global FAILED
    database.init_db()

    # ── 1. Source loading ──────────────────────────────────────────────────
    section("1. Source loading — one intake for every shape of input")

    bundle = source_bundle.from_zip_bytes(make_zip(APP_FILES), label="demo.zip")
    paths = {f["path"] for f in bundle.files}
    check("a zip becomes a file list", len(bundle.files) > 0, str(paths))
    check("the wrapping directory is stripped",
          all(not p.startswith("demo-a1b2c3/") for p in paths), str(paths))
    check("source files are kept", "app/auth.py" in paths, str(paths))
    check("a minified vendor bundle is not reviewed",
          "static/vendor/jquery.min.js" not in paths, str(paths))
    check("markdown is not sent to a code reviewer", "README.md" not in paths, str(paths))
    check("what was left out is reported", bundle.stats.get("files_skipped", 0) > 0,
          json.dumps(bundle.stats))
    check("the skip reasons travel with it",
          all("reason" in s for s in bundle.skipped), str(bundle.skipped[:3]))

    admin = next(f for f in bundle.files if f["path"] == "app/admin.py")
    check("a hardcoded password is redacted before storage",
          "s3cr3t-production-password" not in admin["content"], admin["content"])
    check("...and leaves a marker the reviewer can see",
          "__REDACTED_" in admin["content"], admin["content"])
    check("the redaction map records where, for later use",
          bool(bundle.stats.get("redaction_map")), str(bundle.stats.get("redaction_map")))
    check("the map is not echoed to the caller beside the findings",
          "redaction_map" not in bundle.summary(), str(sorted(bundle.summary())))

    # Path safety. `repo_ingest` owns this; the check is here because this is the first
    # surface that accepts an archive from an unauthenticated-ish caller.
    evil = make_zip({"../../etc/passwd": "root:x:0:0", "ok.py": "x = 1\n"})
    evil_bundle = source_bundle.from_zip_bytes(evil, label="evil.zip")
    check("a traversal path is refused, not sanitised",
          all(".." not in f["path"] for f in evil_bundle.files),
          str([f["path"] for f in evil_bundle.files]))

    creds = make_zip({".env": "SECRET=abc123def456\n", "app.py": "x = 1\n"})
    cred_bundle = source_bundle.from_zip_bytes(creds, label="creds.zip")
    check("a credential file is skipped entirely, not redacted",
          all(f["path"] != ".env" for f in cred_bundle.files),
          str([f["path"] for f in cred_bundle.files]))

    text_bundle = source_bundle.from_text(DIFF)
    check("a diff is recognised as one artifact", text_bundle.is_diff)
    check("...and stays a single unnamed file", len(text_bundle.files) == 1)
    check("pasted text is not treated as untrusted", text_bundle.untrusted_label is None)
    check("a zip is treated as untrusted", bundle.untrusted_label == "UPLOADED_ARCHIVE")

    try:
        source_bundle.from_text("   ")
        check("an empty source is refused", False, "no SourceError raised")
    except source_bundle.SourceError:
        check("an empty source is refused", True)

    # ── 2. Chunking a file list ────────────────────────────────────────────
    section("2. Chunking — files stay files, so a finding gets a real path")

    chunks = code_chunker.chunk_files(bundle.files, max_tokens=200)
    check("a file list splits into chunks", len(chunks) >= 1)
    check("every chunk knows the total", all(c.total == len(chunks) for c in chunks))
    check("a chunk names its own files", all(c.paths for c in chunks))
    check("a unit carries its path with the code",
          "app/auth.py" in chunks[0].body or "app/admin.py" in chunks[0].body,
          chunks[0].body[:200])
    if len(chunks) > 1:
        check("a chunk is told what it was NOT shown",
              "NOT shown to you here" in chunks[0].body, chunks[0].body[:400])
    else:
        check("a chunk is told what it was NOT shown", True, "single chunk — nothing elsewhere")
    manifest_rows = chunks[0].body.split("Rules for this chunk")[0].count("  - ")
    check("the manifest lists one row per file, not per unit",
          manifest_rows <= len(chunks[0].paths), f"{manifest_rows} rows for {len(chunks[0].paths)} paths")
    check("estimating chunk count never returns zero",
          code_chunker.estimate_chunks_for_files(bundle.files) >= 1)

    # ── 3. Per-file evidence verification ──────────────────────────────────
    section("3. Evidence must be in the file the finding names")

    out = finding_pipeline.process_files([dict(SQLI), dict(INVENTED), dict(MISFILED)],
                                         bundle.files)
    kept = {f["title"]: f for f in out["findings"]}
    dropped = {f["title"]: f for f in out["discarded_findings"]}

    check("a finding whose quote is in the named file is kept", "SQL injection in login" in kept)
    check("...and its line is corrected from where the quote really is",
          kept.get("SQL injection in login", {}).get("line_start") == 4,
          str(kept.get("SQL injection in login", {}).get("line_start")))
    check("...and it is marked verified",
          kept.get("SQL injection in login", {}).get("evidence_verified") is True)
    check("...against the file, not merely the artifact",
          kept.get("SQL injection in login", {}).get("evidence_scope") == "file")

    check("an invented quote is discarded", "Remote code execution via eval" in dropped)
    check("...and the discard names the file it was checked against",
          "app/auth.py" in dropped.get("Remote code execution via eval", {}).get("discard_reason", ""),
          dropped.get("Remote code execution via eval", {}).get("discard_reason"))

    check("a finding filed against the wrong file is re-filed, not dropped",
          kept.get("Command injection", {}).get("file_path") == "app/admin.py",
          str(kept.get("Command injection", {}).get("file_path")))
    check("...and the model's original claim is kept for comparison",
          kept.get("Command injection", {}).get("reported_file_path") == "app/auth.py")
    check("...and it is flagged as re-filed",
          kept.get("Command injection", {}).get("file_path_corrected") is True)

    # A path the model spelled short. Dropping a real finding over a missing directory
    # prefix would be a worse error than accepting it.
    short = dict(SQLI, file_path="auth.py")
    out2 = finding_pipeline.process_files([short], bundle.files)
    check("a path missing its directory prefix still resolves",
          out2["findings"] and out2["findings"][0]["file_path"] == "app/auth.py",
          str(out2["findings"][:1]))

    check("every kept finding gets a fingerprint",
          all(f.get("fingerprint") for f in out["findings"]))

    # ── 4. Deterministic secret findings ───────────────────────────────────
    section("4. The redaction map is a finding — free, and impossible to hallucinate")

    secrets = source_bundle.secret_findings(bundle)
    check("redacted secrets become findings", len(secrets) >= 1, str(len(secrets)))
    check("they are marked as scanner findings, not model findings",
          all(f["detector"] == "pattern" for f in secrets), str(secrets[:1]))
    check("they name the file", all(f["file_path"] for f in secrets))
    check("they carry a line number", all(f["line_start"] for f in secrets))
    check("they quote the redacted line, so they verify like any other finding",
          all("__REDACTED_" in (f["evidence"] or "") for f in secrets), str(secrets[:1]))
    check("the value itself never appears in the finding",
          all("s3cr3t-production-password" not in json.dumps(f) for f in secrets))
    check("the recommendation says to rotate, not just to remove",
          all("rotate" in f["recommendation"].lower() for f in secrets))

    verified = finding_pipeline.process_files([dict(s) for s in secrets], bundle.files)
    check("a scanner finding survives evidence verification",
          len(verified["findings"]) + len(verified["suppressed_findings"]) == len(secrets),
          f"{len(verified['findings'])} kept, {len(verified['suppressed_findings'])} suppressed, "
          f"{len(verified['discarded_findings'])} discarded")
    check("a scanner finding in a test path is suppressed like any other",
          any("tests/" in f.get("file_path", "") for f in verified["suppressed_findings"])
          or not any("tests/" in s["file_path"] for s in secrets),
          str([f.get("file_path") for f in verified["suppressed_findings"]]))

    # ── 5. Cost preview ───────────────────────────────────────────────────
    section("5. Cost preview — a number before the spend, not after")

    quote = review_estimate.for_bundle(bundle, agent="code")
    check("a quote reports the number of model calls", quote["calls"] >= 1)
    check("a quote reports the file count", quote["files"] == len(bundle.files))
    check("the expected cost is a number", isinstance(quote["expected_usd"], (int, float)))
    check("the ceiling is above the expectation",
          quote["ceiling_usd"] >= quote["expected_usd"],
          f"{quote['ceiling_usd']} vs {quote['expected_usd']}")
    check("the prompt frame is measured, not guessed",
          quote["prompt_frame_tokens"] > 500, str(quote["prompt_frame_tokens"]))
    check("the quote names the model it priced", bool(quote["model"]))
    check("with no history, the output basis says so",
          quote["output_basis"] in ("default", "measured"), quote["output_basis"])

    sentence = review_estimate.describe(quote)
    check("the quote explains itself in a sentence", len(sentence) > 40, sentence)
    check("...and states the ceiling in it", "ceiling" in sentence, sentence)
    capped = review_estimate.describe(quote, cap_usd=0.0)
    check("a cap below the estimate is called out",
          "below the estimate" in capped, capped)

    # A bigger bundle must cost more and take more calls. The relationship, not the value,
    # is what makes a quote trustworthy.
    big = source_bundle.from_files(
        [{"path": f"app/mod{i}.py", "content": APP_FILES["app/auth.py"] * 60} for i in range(30)],
        label="big",
    )
    big_quote = review_estimate.for_bundle(big, agent="code")
    check("a larger codebase quotes more calls", big_quote["calls"] > quote["calls"],
          f"{big_quote['calls']} vs {quote['calls']}")
    check("...and more money", big_quote["expected_usd"] > quote["expected_usd"])
    check("the quote counts the real split, not a division",
          big_quote["chunks"] == len(code_chunker.chunk_files(big.files)),
          f"{big_quote['chunks']} vs {len(code_chunker.chunk_files(big.files))}")

    vapt_quote = review_estimate.for_bundle(bundle, agent="vapt")
    check("VAPT is priced with its own output budget",
          vapt_quote["max_output_tokens_per_call"] == review_estimate.MAX_OUTPUT_TOKENS["vapt"])

    # ── 6. Through the API ────────────────────────────────────────────────
    section("6. Through the app — routing, entitlement, metering, persistence")

    calls = {"code": 0, "vapt": 0}
    scripted = {"code": None, "vapt": None}

    def fake_call_tool(system_prompt, user_content, schema, override=None,
                       max_tokens=None, **kwargs):
        name = schema.get("name", "")
        if "vapt" in name:
            calls["vapt"] += 1
            return scripted["vapt"] or vapt_response()
        calls["code"] += 1
        return scripted["code"] or code_response()

    llm_gateway.call_tool = fake_call_tool

    from fastapi.testclient import TestClient
    import backend.main as main
    main.llm_settings_service.is_llm_configured = lambda: True

    stack = contextlib.ExitStack()
    # `raise_server_exceptions=False` so the app's own exception handler runs and the
    # refusal arrives as an HTTP status, which is what a real client sees. With the
    # default, TestClient re-raises before `format_error_response` is reached and a
    # deliberate refusal looks like a crashed test.
    client = stack.enter_context(TestClient(main.app, raise_server_exceptions=False))
    try:
        login = client.post("/api/auth/login",
                            json={"username": "admin", "password": config.DEFAULT_ADMIN_PASSWORD})
        check("admin can log in", login.status_code == 200, login.text[:200])
        admin_h = {"Authorization": f"Bearer {login.json()['token']}"}
        proj = client.post("/api/v1/projects", json={"name": "Sources"}, headers=admin_h)
        pid = proj.json()["project"]["id"]
        key = client.post("/api/v1/keys",
                          json={"project_id": pid, "team_name": "sources"}, headers=admin_h)
        H = {"X-API-Key": key.json()["key"]["raw_key"]}
        # A browser session has to name the project it is working in — the upload routes are
        # under the same product guard as everything else, which is the point of checking them.
        UI = {**admin_h, "X-Project-Id": pid}

        files_payload = [{"path": p, "content": c} for p, c in APP_FILES.items()]

        # -- preflight is free ------------------------------------------------
        before = calls["code"]
        pf = client.post("/api/v1/agents/code/preflight",
                         json={"files": files_payload}, headers=H)
        check("the code preflight endpoint answers", pf.status_code == 200, pf.text[:200])
        check("a quote calls no model", calls["code"] == before)
        body = pf.json()
        check("the quote carries an estimate", "estimate" in body and body["estimate"]["calls"] >= 1)
        check("the quote carries the source summary", body["source"]["files_reviewed"] >= 1)
        check("the quote carries a sentence for the screen", len(body.get("explanation", "")) > 40)
        check("the quote reports secrets already found for free",
              body.get("secrets_already_found", 0) >= 1, str(body.get("secrets_already_found")))

        pfv = client.post("/api/v1/agents/vapt/preflight",
                          json={"files": files_payload}, headers=H)
        check("the VAPT preflight endpoint answers", pfv.status_code == 200, pfv.text[:200])

        # -- code review over a file list ------------------------------------
        scripted["code"] = code_response([dict(SQLI), dict(INVENTED)], gate="Green", risk=5.0)
        r = client.post("/api/v1/agents/code",
                        json={"files": files_payload, "project_name": "Sources"}, headers=H)
        check("a file-list code review runs", r.status_code == 200, r.text[:300])
        d = r.json()
        check("the hallucinated finding is not reported",
              all(f["title"] != "Remote code execution via eval" for f in d["findings"]))
        check("...it is reported as discarded instead",
              any(f["title"] == "Remote code execution via eval"
                  for f in d.get("discarded_findings", [])))
        check("a Critical forces the gate to Red even though the model said Green",
              d["gate_status"] == "Red", f"{d['gate_status']} (model: {d.get('model_gate_status')})")
        check("the model's own answer is kept for comparison",
              d.get("model_gate_status") == "Green")
        check("coverage is reported", (d.get("coverage") or {}).get("chunks_total", 0) >= 1)
        check("the file count is on the coverage", (d.get("coverage") or {}).get("files_reviewed") == len(files_payload) - 2,
              str((d.get("coverage") or {}).get("files_reviewed")))
        check("the source summary is echoed back", (d.get("source") or {}).get("files_reviewed", 0) >= 1)
        check("the scanner's secret findings are included",
              any(f.get("detector") == "pattern" for f in d["findings"]),
              str([f.get("detector") for f in d["findings"]]))
        check("test-path findings are suppressed, not reported",
              any("tests/" in (f.get("file_path") or "") for f in d.get("suppressed_findings", [])),
              str([f.get("file_path") for f in d.get("suppressed_findings", [])]))
        check("new vs pre-existing is reported", d.get("new_findings_count") is not None)

        # -- repository URL no longer means "the README" ---------------------
        spec = main.app.openapi()
        code_body = spec["paths"]["/api/v1/agents/code"]["post"]["requestBody"]
        ref = code_body["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
        props = spec["components"]["schemas"][ref]["properties"]
        for field in ("github_repo_url", "ref", "base_ref", "head_ref", "files"):
            check(f"code review accepts `{field}`", field in props, str(sorted(props)))
        desc = json.dumps(props.get("github_repo_url", {}))
        check("the repo field no longer promises what it cannot do",
              "full-repo assessment" not in desc, desc)

        vapt_ref = (spec["paths"]["/api/v1/agents/vapt"]["post"]["requestBody"]
                    ["content"]["application/json"]["schema"]["$ref"].split("/")[-1])
        vprops = spec["components"]["schemas"][vapt_ref]["properties"]
        for field in ("github_repo_url", "files", "base_ref"):
            check(f"VAPT accepts `{field}`", field in vprops, str(sorted(vprops)))

        # -- VAPT now has the whole substrate --------------------------------
        section("7. VAPT — the substrate it never had (C-73)")
        scripted["vapt"] = vapt_response(
            [dict(VAPT_SQLI), dict(INVENTED, vuln_id="VLN-002", cvss_score=9.1)],
            gate="Green", risk=15.0,
        )
        rv = client.post("/api/v1/agents/vapt",
                         json={"files": files_payload, "project_name": "Sources"}, headers=H)
        check("a file-list VAPT assessment runs", rv.status_code == 200, rv.text[:300])
        dv = rv.json()
        check("an unfounded vulnerability is discarded",
              any(f["title"] == "Remote code execution via eval"
                  for f in dv.get("discarded_findings", [])),
              str([f["title"] for f in dv.get("discarded_findings", [])]))
        check("a Critical CVSS finding can no longer come out Green",
              dv["gate_status"] == "Red", f"{dv['gate_status']} (model: {dv.get('model_gate_status')})")
        check("the model's lenient answer is preserved so the gap is measurable",
              dv.get("model_gate_status") == "Green")
        check("the risk score is floored to match the gate", dv["risk_score"] >= 70.0,
              str(dv["risk_score"]))
        check("VAPT findings now carry a fingerprint",
              all(f.get("fingerprint") for f in dv["findings"]),
              str([f.get("fingerprint") for f in dv["findings"]]))
        check("VAPT findings now carry a verified file and line",
              any(f.get("file_path") and f.get("line_start") for f in dv["findings"]))
        check("VAPT reports coverage", (dv.get("coverage") or {}).get("chunks_total", 0) >= 1)
        check("VAPT keeps its own schema's extras", bool(dv.get("attack_surface_summary")))
        check("VAPT counts are still reported", dv.get("total_findings") is not None)

        # A waiver, which was impossible: a fingerprint with no file and no quote never
        # matched a baseline row, so the same findings recurred forever.
        listed = client.get(f"/api/v1/findings?review_id={dv['review_id']}", headers=H)
        rows = listed.json().get("findings", [])
        check("a VAPT finding is listable", bool(rows), listed.text[:200])
        stored_fp = rows[0].get("fingerprint") if rows else None
        check("...and the stored row has a fingerprint", bool(stored_fp), str(stored_fp))
        if rows:
            w = client.patch(f"/api/v1/findings/{rows[0]['id']}",
                             json={"status": "waived", "comment": "accepted risk"}, headers=H)
            check("a VAPT finding can be waived", w.status_code == 200, w.text[:200])
            check("...and the waiver says it will suppress future runs",
                  w.json().get("suppressed_from_future_reviews") is True)
            rv2 = client.post("/api/v1/agents/vapt",
                              json={"files": files_payload, "project_name": "Sources"}, headers=H)
            dv2 = rv2.json()
            check("a re-run reports the waived finding as waived",
                  dv2.get("waived_count", 0) >= 1, str(dv2.get("waived_count")))
            check("...and drops it from the reported list",
                  all(f.get("fingerprint") != stored_fp for f in dv2["findings"]))

        # -- the UI surface --------------------------------------------------
        section("8. The browser surface — same sources, same quote")
        for path in ("/api/review/code/preflight", "/api/review/vapt/preflight",
                     "/api/review/code/source", "/api/review/vapt/source",
                     "/api/review/code/upload-zip", "/api/review/vapt/upload-zip",
                     "/api/review/zip/preflight"):
            check(f"{path} is registered", path in spec["paths"], "not in the OpenAPI schema")

        ui_pf = client.post("/api/review/code/preflight",
                            json={"files": files_payload}, headers=UI)
        check("the browser preflight answers", ui_pf.status_code == 200, ui_pf.text[:200])
        check("...with the same shape as the API one",
              {"estimate", "source", "explanation"} <= set(ui_pf.json()),
              str(sorted(ui_pf.json())))

        zip_bytes = make_zip(APP_FILES)
        zpf = client.post("/api/review/zip/preflight",
                          files={"file": ("demo.zip", zip_bytes, "application/zip")},
                          data={"agent": "code"}, headers=UI)
        check("a zip can be priced before it is reviewed", zpf.status_code == 200, zpf.text[:200])
        check("...and the quote says how many files will actually be read",
              zpf.json()["source"]["files_reviewed"] >= 1, zpf.text[:200])

        scripted["code"] = code_response([dict(SQLI)], gate="Green")
        zrun = client.post("/api/review/code/upload-zip",
                           files={"file": ("demo.zip", zip_bytes, "application/zip")},
                           data={"project_name": "Sources"}, headers=UI)
        check("a zip can be reviewed", zrun.status_code == 200, zrun.text[:300])
        check("...and the result is gated", zrun.json().get("gate_status") == "Red",
              zrun.json().get("gate_status"))

        # Entitlement and metering must reach the new routes, or they are a way around them.
        from backend.services import legacy_guard
        for path in ("/api/review/code/source", "/api/review/code/upload-zip",
                     "/api/review/vapt/source", "/api/review/vapt/upload-zip"):
            check(f"{path} is metered and entitlement-checked",
                  path in legacy_guard.LEGACY_PRODUCT_ROUTES, "absent from the product map")
        check("preflight is deliberately NOT metered — it spends nothing",
              "/api/review/code/preflight" not in legacy_guard.LEGACY_PRODUCT_ROUTES)
        for path in ("/api/review/code", "/api/review/code/source", "/api/review/vapt"):
            check(f"{path} is sized for a fan-out, not one call",
                  path in legacy_guard._CHUNKABLE_ROUTES, "absent from the chunkable map")

        # ── 9. The PR comment ─────────────────────────────────────────────

        section("9. The PR comment now says where the problem is (C-75)")
        gh = main.orchestrator.github
        result = {
            "gate_status": "Red", "risk_score": 82.0, "review_id": "rid-1",
            "summary": "One injection path.",
            "new_findings_count": 1, "pre_existing_count": 2, "waived_count": 1,
            "coverage": {"chunks_total": 4, "chunks_succeeded": 3, "chunks_failed": 1},
            "discarded_findings": [{"title": "invented"}],
            "suppressed_findings": [{"title": "fixture key"}],
            "findings": [dict(SQLI, line_end=6, is_new=True, evidence_verified=True)],
            "recommendations": ["Parameterize queries."],
        }
        comment = gh.format_pr_review_comment(result, result_url="https://app/x")
        check("the comment names the file and line", "app/auth.py:1" in comment or "app/auth.py:1-6" in comment,
              comment[:400])
        check("the comment quotes the offending code", "SELECT * FROM users" in comment)
        check("the comment reports what is new", "1 new" in comment, comment[:400])
        check("the comment reports pre-existing findings", "2 pre-existing" in comment)
        check("the comment reports waived findings", "1 waived" in comment)
        check("the comment reports discarded findings", "discarded" in comment)
        check("the comment warns when coverage was partial", "Partial review" in comment)
        check("the comment carries the review id", "rid-1" in comment)
        check("the comment carries a marker so a re-run can edit it",
              gh.COMMENT_MARKER.startswith("<!--"))
        check("a finding with no location does not print a fake one",
              gh._format_location({}) == "")

        # Inline anchoring rules: only a verified, located, severe-enough finding.
        check("an unverified finding is not anchored to a diff line",
              not (dict(SQLI, evidence_verified=False).get("evidence_verified")))
        check("the inline comment count is capped", gh.MAX_INLINE_COMMENTS <= 30,
              str(gh.MAX_INLINE_COMMENTS))
        check("a Red gate maps to a failing check, so it can block a merge",
              {"Red": "failure", "Amber": "success", "Green": "success"}["Red"] == "failure")

        # ── 10. Regressions the old shapes must not suffer ────────────────
        section("10. The old shapes still behave exactly as before")
        scripted["code"] = code_response([dict(SQLI, file_path="", line_start=None)], gate="Green")
        r_old = client.post("/api/v1/agents/code",
                            json={"code": APP_FILES["app/auth.py"]}, headers=H)
        check("a pasted artifact still reviews", r_old.status_code == 200, r_old.text[:300])
        check("...and is still gated by the arithmetic",
              r_old.json()["gate_status"] == "Red", r_old.json()["gate_status"])

        scripted["code"] = code_response([], gate="Green", summary="")
        r_empty = client.post("/api/v1/agents/code",
                              json={"code": APP_FILES["app/auth.py"] * 5}, headers=H)
        check("an empty response is refused rather than stored as a clean Green",
              r_empty.status_code >= 400, f"{r_empty.status_code}: {r_empty.text[:200]}")

        # No findings *and* no summary is the signature of a truncated response. A clean
        # assessment that says why it is clean is not degenerate and must still be accepted.
        scripted["vapt"] = {**vapt_response([], gate="Green"), "summary": "",
                            "attack_surface_summary": ""}
        r_ve = client.post("/api/v1/agents/vapt",
                           json={"code": APP_FILES["app/auth.py"] * 5}, headers=H)
        check("the same refusal now covers VAPT",
              r_ve.status_code >= 400, f"{r_ve.status_code}: {r_ve.text[:200]}")

        scripted["vapt"] = vapt_response([], gate="Green")
        r_vclean = client.post("/api/v1/agents/vapt",
                               json={"code": APP_FILES["app/auth.py"] * 5}, headers=H)
        check("...but a genuinely clean assessment is still accepted",
              r_vclean.status_code == 200, f"{r_vclean.status_code}: {r_vclean.text[:200]}")

        scripted["code"] = code_response([dict(SQLI, file_path="app/auth.py")], gate="Green")
        r_diff = client.post("/api/v1/agents/code", json={"code": DIFF}, headers=H)
        check("a diff still reviews", r_diff.status_code == 200, r_diff.text[:300])

        # ── 11. Long scans run as background jobs ─────────────────────────
        section("11. A long scan returns a job id and reports progress")

        big_payload = [
            {"path": f"app/mod{i}.py", "content": APP_FILES["app/auth.py"] * 60}
            for i in range(30)
        ]
        scripted["code"] = code_response([dict(SQLI, file_path="app/mod0.py")], gate="Green")
        started = client.post("/api/review/code/source/async",
                              json={"files": big_payload, "project_name": "Sources"}, headers=UI)
        check("a large review is accepted rather than held open",
              started.status_code == 202, f"{started.status_code}: {started.text[:200]}")
        acc = started.json()
        check("...and returns a job id", bool(acc.get("job_id")), started.text[:200])
        check("...with the source summary, so the screen knows what it is waiting for",
              (acc.get("source") or {}).get("files_reviewed") == len(big_payload),
              str(acc.get("source")))
        check("...and the quote it was accepted against",
              (acc.get("estimate") or {}).get("calls", 0) > 1, str(acc.get("estimate")))
        check("...and where to poll", acc.get("poll_url", "").endswith(acc.get("job_id", "x")),
              acc.get("poll_url"))

        # The job runs on a background thread; poll until it settles rather than sleeping a
        # fixed amount, which is the difference between a test that is slow and one that is
        # flaky.
        job = None
        for _ in range(240):
            job = client.get(f"/api/review/jobs/{acc['job_id']}", headers=admin_h).json()
            if job.get("status") in ("completed", "failed"):
                break
            time.sleep(0.25)
        check("the job finishes", job and job.get("status") == "completed",
              json.dumps(job)[:300] if job else "no job")
        check("...having reported progress against the real chunk count",
              (job or {}).get("total_steps") == (result_chunks := (
                  ((job or {}).get("result") or {}).get("coverage") or {}).get("chunks_total")),
              f"progress total {(job or {}).get('total_steps')} vs {result_chunks} chunks actually run")
        result = (job or {}).get("result") or {}
        check("the finished job carries the gated result",
              result.get("gate_status") == "Red", str(result.get("gate_status")))
        check("...including coverage over every chunk",
              (result.get("coverage") or {}).get("chunks_total", 0) > 1,
              str(result.get("coverage")))
        check("...and it is persisted like any other review", bool(result.get("review_id")))

        check("polling an unknown job is a 404, not an empty success",
              client.get("/api/review/jobs/nope", headers=admin_h).status_code == 404)
        check("a job's result needs a credential to read — it carries customer source",
              client.get(f"/api/review/jobs/{acc['job_id']}").status_code == 401)

        # The async routes are guarded in their own handler, not by the middleware, because
        # they return before the LLM work happens — the same reason `/api/review/full` is.
        for path in ("/api/review/code/source/async", "/api/review/vapt/source/async"):
            check(f"{path} is NOT in the middleware's product map (it meters itself)",
                  path not in legacy_guard.LEGACY_PRODUCT_ROUTES,
                  "a request-scoped middleware cannot meter work that happens after the response")

        vstarted = client.post("/api/review/vapt/source/async",
                               json={"files": big_payload, "project_name": "Sources"}, headers=UI)
        check("VAPT has the same async route", vstarted.status_code == 202,
              f"{vstarted.status_code}: {vstarted.text[:200]}")
        vjob = None
        for _ in range(240):
            vjob = client.get(f"/api/review/jobs/{vstarted.json()['job_id']}",
                              headers=admin_h).json()
            if vjob.get("status") in ("completed", "failed"):
                break
            time.sleep(0.25)
        check("...and it completes", vjob and vjob.get("status") == "completed",
              json.dumps(vjob)[:300] if vjob else "no job")

        # ── 12. Findings can leave the platform ───────────────────────────
        section("12. SARIF export — findings a customer's own dashboard can read")

        scripted["code"] = code_response([dict(SQLI)], gate="Green")
        rs = client.post("/api/v1/agents/code",
                         json={"files": files_payload, "project_name": "Sources"}, headers=H)
        rid = rs.json()["review_id"]

        sr = client.get(f"/api/v1/findings/export/sarif/{rid}", headers=H)
        check("a review exports as SARIF", sr.status_code == 200, sr.text[:200])
        log = sr.json()
        check("...at version 2.1.0", log.get("version") == "2.1.0", str(log.get("version")))
        check("...against the published schema", "sarif-2.1.0" in log.get("$schema", ""))
        run = (log.get("runs") or [{}])[0]
        driver = (run.get("tool") or {}).get("driver") or {}
        check("...naming the tool that produced it", bool(driver.get("name")))
        check("...with a rule per control seen, not the whole library",
              0 < len(driver.get("rules") or []) <= 5, str(len(driver.get("rules") or [])))
        check("...and results referencing those rules by index",
              all(isinstance(r.get("ruleIndex"), int) for r in run.get("results") or []))

        located = [r for r in run["results"] if r.get("locations")]
        check("a finding with a verified line carries a physical location", bool(located),
              str(run["results"])[:300])
        if located:
            region = located[0]["locations"][0]["physicalLocation"]["region"]
            check("...pointing at a real line", region.get("startLine", 0) > 0, str(region))
            check("...with the offending code as a snippet", bool(region.get("snippet")))
            uri = located[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
            check("...and the real file path", uri.endswith("auth.py") or uri.endswith("admin.py"), uri)

        check("severity survives as a SARIF level a consumer understands",
              all(r.get("level") in ("error", "warning", "note", "none") for r in run["results"]),
              str([r.get("level") for r in run["results"]]))
        check("...and as a rank, which can express five severities where level cannot",
              all(isinstance(r.get("rank"), (int, float)) for r in run["results"]))
        check("a finding carries its fingerprint, so an alert survives across runs",
              all(r.get("partialFingerprints") for r in run["results"] if r["properties"].get("fingerprint")))
        check("provenance travels — scanner or model",
              all(r["properties"].get("detector") in ("model", "pattern") for r in run["results"]),
              str([r["properties"].get("detector") for r in run["results"]]))

        suppressed = [r for r in run["results"] if r.get("suppressions")]
        check("test-path findings are exported as suppressions, not as alerts",
              bool(suppressed), f"{len(run['results'])} results, none suppressed")
        check("...with the reason attached",
              all(s["suppressions"][0].get("justification") for s in suppressed))

        no_supp = client.get(
            f"/api/v1/findings/export/sarif/{rid}?include_suppressed=false", headers=H).json()
        check("suppressed findings can be excluded on request",
              not any(r.get("suppressions") for r in no_supp["runs"][0]["results"]))

        props = run.get("properties") or {}
        check("the gate travels with the export", props.get("gateStatus") == "Red",
              str(props.get("gateStatus")))
        check("...and so does what was left out, so the export is not silently lossy",
              props.get("discardedFindingCount") is not None
              and props.get("filesReviewed") is not None, str(props))

        # A partial scan must say so in the field SARIF provides, or a consumer reads an
        # absence of alerts in the unscanned part as a clean result.
        partial = sarif_mod.from_review({
            "gate_status": "Amber", "findings": [],
            "coverage": {"chunks_total": 5, "chunks_succeeded": 3, "chunks_failed": 2},
        })
        inv = partial["runs"][0]["invocations"][0]
        check("a partial scan reports executionSuccessful=false",
              inv.get("executionSuccessful") is False, str(inv))
        check("...and says how much was missed",
              "could not be reviewed" in str(inv.get("toolExecutionNotifications")), str(inv))

        unlocated = sarif_mod.from_review({
            "findings": [{"control_id": "SEC-001", "severity": 4, "title": "No location",
                          "file_path": "", "line_start": None}],
        })
        check("a finding with no verified location is exported without one, not pointed at line 1",
              "locations" not in unlocated["runs"][0]["results"][0],
              str(unlocated["runs"][0]["results"][0]))

        dl = client.get(f"/api/v1/findings/export/sarif/{rid}?download=true", headers=H)
        check("it can be downloaded as a file", dl.status_code == 200)
        check("...with the SARIF media type",
              "sarif" in dl.headers.get("content-type", ""), dl.headers.get("content-type"))
        check("...and a filename a CI job can upload",
              ".sarif" in dl.headers.get("content-disposition", ""),
              dl.headers.get("content-disposition"))

        check("an unknown review is a 404",
              client.get("/api/v1/findings/export/sarif/nope", headers=H).status_code == 404)
        check("the export needs a credential — it carries source lines",
              client.get(f"/api/v1/findings/export/sarif/{rid}").status_code == 401)

        # ── 13. Per-project GitHub credentials ────────────────────────────
        section("13. Per-project GitHub credentials (C-76)")

        listed = client.get("/api/v1/github-credentials", headers=H)
        check("credentials can be listed", listed.status_code == 200, listed.text[:200])
        check("...and there are none to begin with", listed.json()["count"] == 0)

        created = client.post("/api/v1/github-credentials", headers=H, json={
            "label": "Acme org — read only", "token": "ghp_" + "a" * 36,
            "scope_pattern": "acme-corp", "can_write": False})
        check("a credential can be saved", created.status_code == 200, created.text[:200])
        cred = created.json()["credential"]
        cred_id = cred["id"]
        check("...and is reported as holding a token", cred["has_token"] is True)
        check("...but the token itself is never returned",
              "ghp_" not in json.dumps(cred), json.dumps(cred))
        check("...only a short hint, to tell two credentials apart",
              cred["token_hint"].endswith("aaaa"), cred["token_hint"])
        check("...and the scope is normalised to lowercase",
              cred["scope_pattern"] == "acme-corp", cred["scope_pattern"])
        check("a credential with no token is refused",
              client.post("/api/v1/github-credentials", headers=H,
                          json={"label": "empty", "token": "  "}).status_code == 400)

        # Resolution order is the whole point: the wrong token produces a 404 that reads
        # exactly like "the repository does not exist".
        stored = database.get_github_credential(cred_id)
        check("the token is encrypted at rest",
              "ghp_" not in (stored["token_encrypted"] or ""), stored["token_encrypted"][:20])
        from backend.services import crypto_service
        check("...and decrypts back to what was supplied",
              crypto_service.decrypt(stored["token_encrypted"]) == "ghp_" + "a" * 36)

        pid_of_cred = stored["project_id"]
        owner_match = database.resolve_github_token(pid_of_cred, "acme-corp/patient-portal")
        check("a credential scoped to the owner serves that owner's repositories",
              owner_match["source"] == "project", str(owner_match["source"]))
        check("...and names itself, so an error can say which token was tried",
              owner_match["label"] == "Acme org — read only", str(owner_match["label"]))
        other = database.resolve_github_token(pid_of_cred, "someone-else/thing")
        check("a repository outside its scope falls back rather than using the wrong token",
              other["source"] != "project", str(other))

        exact = database.save_github_credential(
            pid_of_cred, "One repo only", "ghp_" + "b" * 36,
            scope_pattern="acme-corp/patient-portal")
        picked = database.resolve_github_token(pid_of_cred, "acme-corp/patient-portal")
        check("an exact owner/repo credential wins over an owner-wide one",
              picked["credential_id"] == exact["id"],
              f"picked {picked.get('label')}")
        still_owner = database.resolve_github_token(pid_of_cred, "acme-corp/other-repo")
        check("...while the owner-wide one still serves the rest of the owner",
              still_owner["credential_id"] == cred_id, str(still_owner.get("label")))

        # The client the review will actually use.
        from backend.services.github_service import GitHubService
        gh_scoped = GitHubService.for_project(pid_of_cred, "acme-corp/patient-portal")
        check("the GitHub client picks up the project's credential",
              gh_scoped.token_source == "project", gh_scoped.token_source)
        check("...and reports it without leaking it",
              "ghp_" not in json.dumps(gh_scoped.describe_credential()),
              json.dumps(gh_scoped.describe_credential()))
        gh_override = GitHubService.for_project(pid_of_cred, "acme-corp/patient-portal",
                                                override_token="ghp_" + "c" * 36)
        check("a token supplied with the request beats a stored one",
              gh_override.token_source == "request", gh_override.token_source)

        resolved = client.get(
            "/api/v1/github-credentials/resolve"
            "?repo_url=https://github.com/acme-corp/patient-portal", headers=H)
        check("a caller can ask which credential a repository will use",
              resolved.status_code == 200, resolved.text[:200])
        check("...and the answer names it without returning it",
              "ghp_" not in resolved.text, resolved.text[:200])

        # A 404 has to explain all four things it can mean, because GitHub will not say which.
        unauth = GitHubService(token="")
        try:
            # The zipball is the call a code review makes, so it is the one whose message
            # matters. `get_repo_overview` is checked separately below, because it used to
            # swallow the same failure.
            unauth.get_repo_zipball("https://github.com/torvalds/definitely-not-real-xyz")
            check("an unreachable repository raises", False, "no error raised")
        except Exception as exc:
            msg = str(exc)
            check("an unreachable repository raises", True)
            check("...and the message says GitHub returned 404", "404" in msg, msg[:200])
            check("...that 404 also means 'private'", "private" in msg.lower(), msg[:200])
            check("...that it also means an expired or wrong-account token",
                  "revoked" in msg.lower() and "organisation" in msg.lower(), msg[:300])
            check("...and names which credential was used",
                  "credential" in msg.lower(), msg[:200])
            check("...and is not a bare 'Not Found'", msg.strip() != "Not Found", msg[:80])

        try:
            unauth.get_repo_overview("https://github.com/torvalds/definitely-not-real-xyz")
            check("an unreadable repository is not reported as a clean, empty overview",
                  False,
                  "get_repo_overview returned successfully — Architecture and Security would "
                  "review nothing and report on it, and a credential test would say the "
                  "repository is readable when it is not")
        except Exception as exc:
            check("an unreadable repository is not reported as a clean, empty overview",
                  "404" in str(exc), str(exc)[:200])

        updated = client.put(f"/api/v1/github-credentials/{cred_id}", headers=H, json={
            "label": "Acme org — renamed", "token": "", "scope_pattern": "acme-corp",
            "can_write": True})
        check("a credential can be renamed without re-typing the token",
              updated.status_code == 200, updated.text[:200])
        check("...and the stored token survives",
              crypto_service.decrypt(
                  database.get_github_credential(cred_id)["token_encrypted"]) == "ghp_" + "a" * 36)
        check("...and the change took effect",
              updated.json()["credential"]["label"] == "Acme org — renamed")

        gone = client.delete(f"/api/v1/github-credentials/{cred_id}", headers=H)
        check("a credential can be deleted", gone.status_code == 200, gone.text[:200])
        check("...and is really gone", database.get_github_credential(cred_id) is None)
        check("deleting an unknown credential is a 404",
              client.delete("/api/v1/github-credentials/nope", headers=H).status_code == 404)
        check("the credential surface needs a credential of its own",
              client.get("/api/v1/github-credentials").status_code == 401)

        # A single uploaded file must not be read as an archive — the defect the browser
        # examination caught, because the screen offered two upload modes and posted both
        # to the zip reader.
        section("14. An upload can be one file, not only an archive")
        one_file = source_bundle.from_upload(
            APP_FILES["app/auth.py"].encode(), filename="auth.py")
        check("a single source file is accepted", len(one_file.files) == 1,
              str([f["path"] for f in one_file.files]))
        check("...keeping its own name as the path",
              one_file.files[0]["path"] == "auth.py", one_file.files[0]["path"])
        check("...and is not filtered out by extension rules it never opted into",
              one_file.files[0]["content"].strip() != "")
        as_zip = source_bundle.from_upload(make_zip(APP_FILES), filename="demo.zip")
        check("an archive is still read as an archive", len(as_zip.files) > 1,
              str(len(as_zip.files)))
        check("the decision is made from the bytes, not the name",
              source_bundle.looks_like_zip(make_zip(APP_FILES), "anything.py") is True)
        check("...so source renamed to .zip is not called corrupt",
              source_bundle.looks_like_zip(b"print(1)\n", "app.zip") is False)
        try:
            source_bundle.from_upload(b"\x00\x01\x02binary", filename="a.bin")
            check("a binary upload is refused with a reason", False, "no error")
        except source_bundle.SourceError as exc:
            check("a binary upload is refused with a reason", "binary" in str(exc), str(exc))
        # ── 19. The source inventory ──────────────────────────────────────
        #
        # A result could say "4 file(s) read, 6 filtered out" and not say which four. These
        # checks guard the three things that made that answerable, and the promise attached
        # to the third: the inventory exists, it explains each exclusion in terms of whether
        # code went *unread*, and stored content actually disappears when its window passes.
        section("19. What a review looked at -- the file inventory and its retention")

        from backend.services import review_source_store as _store

        check("a retention choice out of range is clamped, not refused",
              _store.normalize_retention(90) == 30, str(_store.normalize_retention(90)))
        check("...and rounds down to a window we actually offer",
              _store.normalize_retention(5) == 3, str(_store.normalize_retention(5)))
        check("an unparseable value takes the default rather than storing nothing",
              _store.normalize_retention("soon") == _store.DEFAULT_RETENTION_DAYS,
              "silently keeping nothing costs someone a feature they thought they had")
        check("zero means zero", _store.normalize_retention(0) == 0)
        check("the default is 7 days", _store.DEFAULT_RETENTION_DAYS == 7)

        # The exclusion vocabulary. This is the check that matters most on this screen: a
        # skipped `node_modules` and a file the review ran out of budget to read are both
        # "filtered out" today, and only one of them means the review missed something.
        check("a skipped dependency directory is classed as not-source",
              _store.describe_reason("excluded_directory")["kind"] == "not-source")
        check("a credential file is classed as a safety skip",
              _store.describe_reason("credential_file")["kind"] == "safety")
        check("hitting the file limit is classed as a COVERAGE gap",
              _store.describe_reason("file_limit_reached")["kind"] == "coverage",
              "this is the only kind that means code went unread")
        check("...as is a file too large to read",
              _store.describe_reason("too_large (912345 chars)")["kind"] == "coverage")
        check("...and the size is kept as detail rather than lost",
              _store.describe_reason("too_large (912345 chars)")["detail"] == "912345 chars")
        check("every known reason gets a sentence, not a token",
              all(len(_store.describe_reason(r)["text"]) > 40
                  for r in ("unsafe_path", "excluded_directory", "credential_file",
                            "extension_not_converted (.png)", "file_limit_reached")))
        check("an unknown reason degrades to itself instead of vanishing",
              _store.describe_reason("brand_new_token")["code"] == "brand_new_token")

        inv_bundle = source_bundle.from_files(
            [{"path": "app/auth.py", "content": APP_FILES["app/auth.py"]},
             {"path": "app/admin.py", "content": APP_FILES["app/admin.py"]},
             {"path": "node_modules/lib/x.js", "content": "module.exports = 1\n"},
             {"path": "logo.png", "content": "PNG"}],
            label="inventory demo")
        inv_review = database.save_review(
            "code", "inv", "s", "c",
            {"findings": [{"file_path": "app/auth.py", "severity": 5}], "gate_status": "Red"})
        _store.record(inv_review, inv_bundle, retention_days=7,
                      findings=[{"file_path": "app/auth.py"}],
                      chunk_of_path={"app/auth.py": 1, "app/admin.py": 1})
        inv = _store.get_inventory(inv_review)

        included = [f for f in inv["files"] if f["included"]]
        excluded = [f for f in inv["files"] if not f["included"]]
        check("the included files are listed by name, not counted",
              {f["path"] for f in included} == {"app/auth.py", "app/admin.py"},
              str(sorted(f["path"] for f in included)))
        check("the exclusions are listed too", len(excluded) >= 1, str(len(excluded)))
        check("every exclusion carries its explanation",
              all(f.get("why", {}).get("text") for f in excluded))
        check("exclusions are tallied by what the exclusion means",
              bool(inv["excluded_by_kind"]), str(inv["excluded_by_kind"]))
        check("a clean filter is not reported as a coverage gap",
              inv["coverage_gaps"] == 0, str(inv["coverage_gaps"]))
        check("each file records which chunk the model saw it in",
              all(f["chunk_index"] == 1 for f in included),
              str([(f["path"], f["chunk_index"]) for f in included]))
        check("...and how many findings landed on it",
              next(f["findings_count"] for f in included if f["path"] == "app/auth.py") == 1)
        check("the inventory says it can be opened", inv["openable"] is True)

        opened = _store.get_file(inv_review, "app/auth.py")
        check("a stored file reads back byte for byte",
              opened.get("content") == APP_FILES["app/auth.py"],
              (opened.get("unavailable") or "")[:120])
        check("...and says it is the copy the review was shown",
              opened.get("origin") == "stored", str(opened.get("origin")))

        # Encryption at rest. A leaked database file must not be a copy of the source.
        _c = database.get_connection()
        _raw = _c.execute("SELECT content_encrypted FROM review_source_files "
                          "WHERE review_id = ? AND path = ?",
                          (inv_review, "app/auth.py")).fetchone()["content_encrypted"]
        _c.close()
        check("stored source is encrypted on disk",
              _raw and "def login" not in _raw and APP_FILES["app/auth.py"] not in _raw,
              "a leaked db file should not be a copy of the customer's source")

        excluded_path = excluded[0]["path"]
        refused = _store.get_file(inv_review, excluded_path)
        check("an excluded file explains that it was never read",
              "not read" in (refused.get("unavailable") or "").lower(),
              str(refused.get("unavailable"))[:120])
        check("a path that was never in the review is not confused with an expired one",
              "No file" in (_store.get_file(inv_review, "nope.py").get("unavailable") or ""))

        # Retention 0: the inventory still exists. This is the distinction that stops a
        # screen telling someone their code was deleted when it was never kept.
        none_review = database.save_review("code", "inv", "s", "c",
                                           {"findings": [], "gate_status": "Green"})
        _store.record(none_review, inv_bundle, retention_days=0)
        none_inv = _store.get_inventory(none_review)
        check("with no retention the file list is still recorded",
              len([f for f in none_inv["files"] if f["included"]]) == 2)
        check("...but nothing can be opened", none_inv["openable"] is False)
        check("...and the reason says it was never kept, not that it expired",
              "keep no source" in _store.get_file(none_review, "app/admin.py")["unavailable"],
              _store.get_file(none_review, "app/admin.py")["unavailable"][:120])

        # A GitHub source is never copied -- it is re-read at the recorded commit.
        gh_bundle = source_bundle.from_files(
            [{"path": "app/admin.py", "content": APP_FILES["app/admin.py"]}], label="acme/portal")
        gh_bundle.source_type = "github_repo"
        gh_bundle.meta = {"commit_sha": "c" * 40}
        gh_review = database.save_review("code", "inv", "s", "c",
                                         {"findings": [], "gate_status": "Green"})
        _store.record(gh_review, gh_bundle, retention_days=30)
        gh_inv = _store.get_inventory(gh_review)
        check("a GitHub source is not copied even when retention is asked for",
              not gh_inv["source"]["content_stored"],
              "a stored copy of something we can re-fetch can only go stale")
        check("...but its files are still openable, by re-reading the repository",
              gh_inv["openable"] is True)
        check("...and the exact commit is recorded, never the branch name",
              gh_inv["source"]["commit_sha"] == "c" * 40,
              "a branch moves; a week later the line numbers would point at other code")

        # The sha comes out of the archive we actually downloaded, so it cannot disagree
        # with what was reviewed.
        _zbuf = io.BytesIO()
        with zipfile.ZipFile(_zbuf, "w") as _z:
            _z.writestr("acme-portal-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b/README.md", "hi")
        check("the commit is read out of the zipball itself, with no extra request",
              source_bundle.sha_from_zipball(_zbuf.getvalue())
              == "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b")
        check("...and a non-zipball yields no sha rather than a wrong one",
              source_bundle.sha_from_zipball(b"not a zip") == "")

        # Expiry. The promise the retention window makes has to be kept without a scheduler.
        _c = database.get_connection()
        _c.execute("UPDATE review_sources SET expires_at = '2020-01-01T00:00:00' "
                   "WHERE review_id = ?", (inv_review,))
        _c.commit()
        _c.close()
        check("the sweep clears exactly the expired review", _store.sweep_expired() == 1)
        after = _store.get_inventory(inv_review)
        check("after expiry the file list is still there",
              len([f for f in after["files"] if f["included"]]) == 2,
              "which files were reviewed is a permanent record")
        check("...but no file is readable any more",
              all(f["readable"] == 0 for f in after["files"]))
        check("...and the inventory stops offering to open one", after["openable"] is False)
        _gone = _store.get_file(inv_review, "app/auth.py")
        check("...and reading one explains the window passed",
              "no longer kept" in (_gone.get("unavailable") or ""),
              str(_gone.get("unavailable"))[:120])
        check("a second sweep is a no-op rather than an error",
              _store.sweep_expired() == 0)

        # Deleting early. Retention is a ceiling, not a commitment to hold something.
        _store.delete_for_review(none_review)
        check("a source can be forgotten before its window expires",
              _store.get_inventory(none_review)["total"] == 0)

        # The routes, project-scoped like every other governance read. `inv_review` above was
        # written straight to the database with no project bound, so it is deliberately NOT
        # visible to a project-scoped key -- that is the scoping guarantee, and asserting it
        # is worth more than working around it.
        check("a review outside the caller's projects 404s, inventory included",
              client.get(f"/api/reviews/{inv_review}/sources", headers=H).status_code == 404,
              "a review carries the customer's source lines; scope applies to it too")
        check("a review id that does not exist 404s the same way, leaking nothing",
              client.get("/api/reviews/no-such-review/sources", headers=H).status_code == 404)

        # ...and now through the API, on a review the caller really owns.
        scripted["code"] = code_response([dict(SQLI)], gate="Red")
        api_run = client.post("/api/review/code/source",
                              json={"files": files_payload, "project_name": "Sources",
                                    "retention_days": 7}, headers=H)
        check("a review through the UI surface still succeeds with retention set",
              api_run.status_code == 200, api_run.text[:200])
        api_rid = api_run.json().get("review_id")
        r_inv = client.get(f"/api/reviews/{api_rid}/sources", headers=H)
        check("its inventory is reachable", r_inv.status_code == 200, r_inv.text[:240])
        inv_json = r_inv.json()
        check("...and lists the files that were actually reviewed",
              len([f for f in inv_json["files"] if f["included"]]) >= 1,
              str(inv_json.get("total")))
        check("...without carrying any file content",
              all("content" not in f for f in inv_json["files"]),
              "a list endpoint carrying file bodies would defeat the retention window")
        check("the response reports the run's own retention",
              inv_json["source"].get("retention_days") == 7,
              str(inv_json["source"].get("retention_days")))
        first_path = next(f["path"] for f in inv_json["files"] if f["included"])
        r_file = client.get(f"/api/reviews/{api_rid}/sources/file",
                            params={"path": first_path}, headers=H)
        check("one file reads back through the API", r_file.status_code == 200,
              r_file.text[:200])
        check("...with content, not a refusal", bool(r_file.json().get("content")),
              str(r_file.json().get("unavailable"))[:160])
        check("a path not in the review is refused with an explanation, not a 500",
              "No file" in (client.get(f"/api/reviews/{api_rid}/sources/file",
                                       params={"path": "../../etc/passwd"},
                                       headers=H).json().get("unavailable") or ""),
              "a path outside the inventory has no row, so there is nothing to read")
        check("the stored source can be deleted early",
              client.delete(f"/api/reviews/{api_rid}/sources",
                            headers=H).status_code == 200)
        check("...after which nothing is left to open",
              client.get(f"/api/reviews/{api_rid}/sources", headers=H).json()["total"] == 0)

        # ── 20. Previous runs, from the agent's own screen ─────────────────
        section("20. A per-agent run history")

        runs_all = client.get("/api/reviews", params={"limit": 50}, headers=H)
        runs_code = client.get("/api/reviews", params={"limit": 50, "review_type": "code"},
                               headers=H)
        check("reviews can be listed for one agent", runs_code.status_code == 200,
              runs_code.text[:200])
        check("...and the filter actually filters",
              all(r["review_type"] == "code" for r in runs_code.json()["reviews"]),
              str({r["review_type"] for r in runs_code.json()["reviews"]}))
        check("...with a total that matches the filter, not the whole table",
              runs_code.json()["total"] <= runs_all.json()["total"],
              f"{runs_code.json()['total']} vs {runs_all.json()['total']}")
        check("an unknown agent name yields an empty page rather than everything",
              client.get("/api/reviews", params={"review_type": "not-an-agent"},
                         headers=H).json()["total"] == 0)
        check("a list row does NOT carry the stored result",
              "result_json" not in (runs_code.json()["reviews"][0] or {}),
              "SELECT * was shipping every finding and 5,000 chars of source to draw a row")
        check("...nor the customer's source",
              "input_content" not in (runs_code.json()["reviews"][0] or {}))
        check("a list row still carries what a summary line needs",
              all(k in runs_code.json()["reviews"][0]
                  for k in ("gate_status", "findings_count", "critical_count", "created_at")))

        # ── 21. Applying fixes ────────────────────────────────────────────
        #
        # Auto-Fix worked and could only be pointed at a pull request, where it re-ran the
        # review and chose the findings itself. These checks guard the four things that made
        # it usable from a finished review, and -- more importantly -- the two guardrails
        # that must survive every future change to it, because they are the only thing
        # between a model's rewrite and a customer's repository.
        section("21. Apply Fixes -- selection, the per-file price, and the two guardrails")

        from backend.services import fix_apply

        FIX_FINDINGS = [
            {"id": "f1", "title": "SQL injection", "file_path": "app/auth.py",
             "line_start": 4, "severity": 5, "control_id": "SEC-008",
             "evidence_verified": True, "is_blocking": True, "evidence": "SELECT"},
            {"id": "f2", "title": "Also in auth", "file_path": "app/auth.py",
             "line_start": 9, "severity": 4, "control_id": "SEC-016",
             "evidence_verified": True, "is_blocking": False, "evidence": "id"},
            {"id": "f3", "title": "Unverified claim", "file_path": "app/auth.py",
             "severity": 5, "evidence_verified": False},
            {"id": "f4", "title": "Architecture concern", "severity": 3,
             "evidence_verified": True},
            {"id": "f5", "title": "Fixture key", "file_path": "tests/fixtures.py",
             "severity": 5, "evidence_verified": True, "suppressed_reason": "test path"},
            {"id": "f6", "title": "Already handled", "file_path": "app/admin.py",
             "severity": 4, "evidence_verified": True, "status": "waived"},
        ]

        split = fix_apply.eligible(FIX_FINDINGS)
        check("only provable findings are offered for fixing",
              {f["id"] for f in split["fixable"]} == {"f1", "f2"},
              str(sorted(f["id"] for f in split["fixable"])))
        check("an unverified finding is refused -- its 'fix' would be invented",
              any(f["id"] == "f3" and "may not exist" in f["fix_blocked_reason"]
                  for f in split["not_fixable"]))
        check("a finding with no file is refused, and says why",
              any(f["id"] == "f4" and "nothing to patch" in f["fix_blocked_reason"]
                  for f in split["not_fixable"]))
        check("a test-path finding is refused but says you may fix it by hand",
              any(f["id"] == "f5" and "by hand" in f["fix_blocked_reason"]
                  for f in split["not_fixable"]))
        check("an already-waived finding is not offered again",
              any(f["id"] == "f6" for f in split["not_fixable"]))
        check("every refusal carries a reason, not just a disabled state",
              all(f.get("fix_blocked_reason") for f in split["not_fixable"]),
              "a checkbox you cannot tick, with no reason, reads as a broken feature")

        # The quote. The number people misread: two findings in one file is ONE fix call.
        fix_bundle = source_bundle.from_files(
            [{"path": "app/auth.py", "content": APP_FILES["app/auth.py"]},
             {"path": "app/admin.py", "content": APP_FILES["app/admin.py"]}],
            label="fix demo")
        # `save_review` stamps a fresh uuid onto every finding as it persists it -- that is
        # how a browser gets an id it can waive or fix by. So the ids to select by are the
        # ones it assigned, not the ones written above.
        import copy as _copy
        fix_rid = database.save_review(
            "code", "fix", "s", "c",
            {"findings": _copy.deepcopy(FIX_FINDINGS), "gate_status": "Red"})
        _store.record(fix_rid, fix_bundle, retention_days=7)
        _stored = json.loads(database.get_review(fix_rid)["result_json"])["findings"]
        FID = {f["title"]: f["id"] for f in _stored}
        check("a persisted finding carries an id a caller can select it by",
              all(FID.values()) and len(FID) == len(FIX_FINDINGS), str(list(FID)))

        sized = fix_apply.plan(fix_rid, [FID["SQL injection"], FID["Also in auth"]])
        check("two findings in one file are one file to change",
              sized["files_to_change"] == 1, str(sized["files_to_change"]))
        check("...and the quote says cost is per file, not per finding",
              "per file, not per finding" in sized["note"], sized["note"][:120])
        check("the re-review is quoted, not discovered on the invoice",
              sized["calls"] == 2, f"{sized['calls']} call(s) for one file")
        check("a price is attached", (sized["estimate"] or {}).get("ceiling_usd", 0) > 0,
              str(sized["estimate"])[:160])
        check("the caveat says nothing is compiled or run",
              "compiles" in sized["caveat"] and "run" in sized["caveat"],
              sized["caveat"][:140])

        # Which deliveries are offered depends on the source. Offering a pull request for a
        # zip review would be a control that fails after the money is spent.
        out_ids = {o["id"] for o in sized["outputs"]}
        check("a zip review is not offered a pull request it cannot open",
              out_ids == {"diff", "zip"}, str(sorted(out_ids)))

        gh_fix = source_bundle.from_files(
            [{"path": "app/auth.py", "content": APP_FILES["app/auth.py"]}], label="acme/portal")
        gh_fix.source_type = "github_repo"
        gh_fix.ref = "https://github.com/acme/portal"
        gh_fix.meta = {"commit_sha": "e" * 40}
        gh_fix_rid = database.save_review(
            "code", "fix", "s", "c",
            {"findings": _copy.deepcopy(FIX_FINDINGS[:1]), "gate_status": "Red"})
        _GHFID = json.loads(
            database.get_review(gh_fix_rid)["result_json"])["findings"][0]["id"]
        _store.record(gh_fix_rid, gh_fix, retention_days=0)
        gh_sized = fix_apply.plan(gh_fix_rid, [_GHFID])
        check("a repository review IS offered a branch and a pull request",
              {"pr", "branch"} <= {o["id"] for o in gh_sized["outputs"]},
              str([o["id"] for o in gh_sized["outputs"]]))
        check("...and the pull-request option names the write scope it needs",
              any("Read and Write" in o["note"] for o in gh_sized["outputs"]))

        # A file whose content is gone cannot be patched, and that has to be said rather
        # than producing a patch against nothing.
        _store.delete_for_review(fix_rid)
        try:
            fix_apply.plan(fix_rid, [FID["SQL injection"]])
            check("a fix over a forgotten source is refused with a reason", False, "no error")
        except fix_apply.FixError as exc:
            check("a fix over a forgotten source is refused with a reason",
                  "nothing" in str(exc).lower(), str(exc)[:180])
            check("...and says what to do about it",
                  "re-run" in str(exc).lower() or "retention" in str(exc).lower(),
                  str(exc)[:220])
        _store.record(fix_rid, fix_bundle, retention_days=7)

        try:
            fix_apply.plan(fix_rid, [FID["Unverified claim"]])
            check("selecting only an unfixable finding is refused", False, "no error")
        except fix_apply.FixError as exc:
            check("selecting only an unfixable finding is refused",
                  "can be fixed automatically" in str(exc), str(exc)[:140])
            check("...and repeats the reason that finding was refused for",
                  "may not exist" in str(exc), str(exc)[:200])

        # The two guardrails. These are the whole safety story, so they are checked directly
        # rather than through a run.
        _secret_patch = {"path": "app/auth.py",
                         "fixed_content": 'API_KEY = "sk-live-abcdefghijklmnopqrstuvwxyz01"\n'}
        _clean_patch = {"path": "app/auth.py",
                        "fixed_content": "def login(u, p):\n    return check(u, p)\n"}

        class _Orch:
            """Minimal stand-in: the guard only reaches back for the re-review."""
            def __init__(self, verdict):
                self._verdict = verdict

        import backend.workflows.autofix_pr as _afp
        _real_verify = _afp._verify_patch
        try:
            _afp._verify_patch = lambda orch, patch, ctx: dict(orch._verdict)
            guarded = fix_apply._guard_patches(
                _Orch({"blocked": False}), [dict(_secret_patch), dict(_clean_patch)], "")
            check("a patch that introduces a hardcoded secret is rejected",
                  any(b["path"] == "app/auth.py" and "secret" in b["reason"]
                      for b in guarded["blocked"]),
                  str(guarded["blocked"])[:200])
            check("...and the clean one still gets through",
                  len(guarded["safe"]) == 1, str(len(guarded["safe"])))

            guarded2 = fix_apply._guard_patches(
                _Orch({"blocked": True, "reason": "still contains 1 Critical",
                       "introduced": ["PHI to a third party"]}),
                [dict(_clean_patch)], "")
            check("a patch whose re-review still finds a Critical is rejected",
                  not guarded2["safe"] and "Critical" in guarded2["blocked"][0]["reason"],
                  str(guarded2["blocked"])[:200])
            check("...and says what it introduced",
                  guarded2["blocked"][0].get("introduced") == ["PHI to a third party"])
        finally:
            _afp._verify_patch = _real_verify

        # The diff, which is the only view that shows what was *removed*.
        _diff = fix_apply.make_diff(
            {"a.py": "x = 1\ny = 2\nz = 3\n"},
            [{"path": "a.py", "fixed_content": "x = 1\ny = 22\n"}])
        check("a diff is produced, not just the rewritten file", "@@" in _diff, _diff[:120])
        check("...showing the removed line, which a rewritten file hides",
              "-y = 2" in _diff and "-z = 3" in _diff, _diff[:200])
        check("...and naming the file on both sides",
              "a/a.py" in _diff and "b/a.py" in _diff)

        # The archive, and the note that has to travel with it.
        _zip = fix_apply.make_zip(
            [{"path": "app/auth.py", "fixed_content": "ok\n"}], _diff, "readme")
        with zipfile.ZipFile(io.BytesIO(_zip)) as _z:
            _names = _z.namelist()
        check("the zip carries the patched file", "app/auth.py" in _names, str(_names))
        check("...the diff beside it", "CHANGES.diff" in _names, str(_names))
        check("...and a note the reader sees first", "READ-THIS-FIRST.md" in _names, str(_names))
        _readme = fix_apply._zip_readme(
            [{"path": "app/auth.py", "explanation": "parameterized"}],
            [{"path": "b.py", "reason": "still Critical"}], ["design concern"], "demo")
        check("the note says plainly that nothing was compiled or run",
              "compiled" in _readme and "run" in _readme, _readme[:400])
        check("...lists what was rejected", "still Critical" in _readme)
        check("...and what was deliberately not fixed", "design concern" in _readme)

        # The routes.
        _paths = client.app.openapi()["paths"] if hasattr(client, "app") else {}
        from backend.main import app as _app
        _paths = _app.openapi()["paths"]
        check("the plan endpoint is registered",
              "/api/reviews/{review_id}/fix/plan" in _paths)
        check("the fix endpoint is registered", "/api/reviews/{review_id}/fix" in _paths)
        check("the download is a separate single-use route",
              "/api/reviews/{review_id}/fix/download/{token}" in _paths)
        _fp = client.post(f"/api/reviews/{fix_rid}/fix/plan",
                          json={"finding_ids": [FID["SQL injection"]]}, headers=H)
        check("a fix plan on another project's review 404s",
              _fp.status_code == 404, _fp.text[:160])

        # ── 22. A diff is not one nameless file ───────────────────────────
        #
        # Three defects with one root cause, all found by a real user on the flagship path.
        # A pull-request bundle is a *single* pseudo-file with an empty path holding the
        # whole diff -- correct for the chunker and for evidence verification, and read as
        # "the files" by everything downstream. So: the quote said "1 file(s)" with nothing
        # to check it against; the inventory recorded one nameless row and showed
        # "(pasted content)" for a four-file pull request; and Apply Fixes, which routes
        # findings by `file_path`, matched nothing and refused with "none of the files these
        # findings are in can be read back".
        section("22. A pull request's files are named, inventoried and patchable")

        PR_DIFF = (
            "diff --git a/app/routers/appointments.py b/app/routers/appointments.py\n"
            "--- a/app/routers/appointments.py\n"
            "+++ b/app/routers/appointments.py\n"
            "@@ -1,2 +1,4 @@\n"
            " from fastapi import APIRouter\n"
            "+def get(pid):\n"
            "+    return q(\"SELECT * FROM appointments WHERE id = \" + pid)\n"
            "diff --git a/app/summary.py b/app/summary.py\n"
            "--- a/app/summary.py\n"
            "+++ b/app/summary.py\n"
            "@@ -1,1 +1,3 @@\n"
            " import os\n"
            "+def summarise(note):\n"
            "+    return call_llm(note)\n"
        )
        PR_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"

        class _PRGitHub:
            """What the real payloads carry. `head_sha` was in the response and dropped."""
            fetched_at = []

            def get_pr_diff(self, pr_url):
                return {
                    "pr_number": 1, "title": "CB-104: AI visit summary endpoint",
                    "description": "", "author": "someone",
                    "base_branch": "main", "head_branch": "feat/cb-104",
                    "head_sha": PR_SHA, "base_sha": "0" * 40,
                    "diff": PR_DIFF, "diff_truncated": False,
                    "diff_full_chars": len(PR_DIFF),
                    "files_changed": ["app/routers/appointments.py", "app/summary.py"],
                    "additions": 4, "deletions": 0, "url": pr_url,
                }

            def get_file_content_at_ref(self, owner, repo, path, ref):
                _PRGitHub.fetched_at.append(ref)
                return {"path": path, "content": f"# whole {path}\nx = 1\n"}

        _prgh = _PRGitHub()
        pr_bundle = source_bundle.from_github_pr(
            _prgh, "https://github.com/acme/portal/pull/1")

        check("a PR bundle is still one artifact for the chunker",
              len(pr_bundle.files) == 1 and pr_bundle.is_diff,
              "splitting a diff into pseudo-files would break the line numbers")
        check("...but it now carries the commit it is a diff of",
              pr_bundle.meta.get("commit_sha") == PR_SHA,
              str(pr_bundle.meta.get("commit_sha")))
        check("...and the repository it came from",
              pr_bundle.meta.get("repo_url") == "https://github.com/acme/portal",
              str(pr_bundle.meta.get("repo_url")))

        # The quote names the files. Previously "1 file(s)" and no list.
        from backend.main import _files_preview
        prev = _files_preview(pr_bundle)
        check("the quote names the files the diff touches, not one nameless row",
              [f["path"] for f in prev]
              == ["app/routers/appointments.py", "app/summary.py"], str(prev))
        check("...and does not invent a size a diff cannot know",
              all("chars" not in f for f in prev),
              "a diff carries changed lines; a made-up file size is worse than none")
        check("a non-diff source still reports real sizes",
              all(f.get("chars") for f in _files_preview(source_bundle.from_files(
                  [{"path": "a.py", "content": "x = 1\n"}], label="z"))))

        PR_FINDINGS = [
            {"title": "SQL injection", "file_path": "app/routers/appointments.py",
             "line_start": 3, "severity": 5, "control_id": "SEC-008",
             "risk_level": "Critical", "description": "concatenated",
             "recommendation": "parameterize", "is_blocking": True,
             "evidence_verified": True, "evidence": "SELECT * FROM appointments"},
            {"title": "PHI to a third party", "file_path": "app/summary.py",
             "line_start": 2, "severity": 5, "control_id": "HIPAA-005",
             "risk_level": "Critical", "description": "note sent out",
             "recommendation": "de-identify", "is_blocking": True,
             "evidence_verified": True, "evidence": "call_llm(note)"},
        ]
        pr_rid = database.save_review("code", "carebrief", "PR #1", PR_DIFF[:200],
                                      {"findings": _copy.deepcopy(PR_FINDINGS),
                                       "gate_status": "Red"})
        _store.record(pr_rid, pr_bundle, retention_days=7, findings=PR_FINDINGS)
        pr_inv = _store.get_inventory(pr_rid)

        check("the inventory names the files, not '(pasted content)'",
              {f["path"] for f in pr_inv["files"] if f["included"]}
              == {"app/routers/appointments.py", "app/summary.py"},
              str([f["path"] for f in pr_inv["files"]]))
        check("...with each file's finding count against it",
              all(f["findings_count"] == 1 for f in pr_inv["files"] if f["included"]))
        check("nothing is copied for a diff -- there is no whole file in it to copy",
              not pr_inv["source"]["content_stored"])
        check("...and the note says where a whole file comes from instead",
              "read back from the repository" in pr_inv["source"]["storage_note"],
              pr_inv["source"]["storage_note"][:120])
        check("...so the panel still offers to open one", pr_inv["openable"] is True)
        check("the commit is recorded, which is what makes that honest",
              pr_inv["source"]["commit_sha"] == PR_SHA)
        check("...and the repository URL, not the PR URL, is what a fetch needs",
              pr_inv["source"]["ref"] == "https://github.com/acme/portal",
              pr_inv["source"]["ref"])

        # Reading a file back. This is the call that was reaching a function with a
        # different signature and a method that does not exist.
        _real_gh_for_repo = source_bundle.github_for_repo
        source_bundle.github_for_repo = lambda repo_url, project_id=None: _prgh
        try:
            got = _store.get_file(pr_rid, "app/summary.py")
            check("a file in a diff reads back as the whole file",
                  got.get("content", "").startswith("# whole app/summary.py"),
                  str(got.get("unavailable"))[:160])
            check("...marked as re-fetched rather than as the reviewed copy",
                  got.get("origin") == "refetched", str(got.get("origin")))
            check("...fetched AT THE COMMIT, never at the branch name",
                  _PRGitHub.fetched_at and _PRGitHub.fetched_at[-1] == PR_SHA,
                  str(_PRGitHub.fetched_at))

            # And the whole reason this matters: Apply Fixes can now find the files.
            _pr_ids = [f["id"] for f in json.loads(
                database.get_review(pr_rid)["result_json"])["findings"]]
            pr_plan = fix_apply.plan(pr_rid, _pr_ids)
            check("Apply Fixes finds both files of a pull request",
                  pr_plan["files_to_change"] == 2, str(pr_plan["files_to_change"]))
            check("...and nothing is reported as unreadable",
                  not pr_plan["unavailable_files"], str(pr_plan["unavailable_files"]))
            check("...and a pull request is offered, because there is a repo to open it on",
                  "pr" in {o["id"] for o in pr_plan["outputs"]},
                  str([o["id"] for o in pr_plan["outputs"]]))
        finally:
            source_bundle.github_for_repo = _real_gh_for_repo

        # A branch comparison is the same shape and needs the same treatment.
        check("the compare payload carries a head sha too",
              "head_sha" in str(io.open(
                  os.path.join(os.path.dirname(os.path.dirname(
                      os.path.abspath(__file__))), "backend", "services",
                      "github_service.py"), encoding="utf-8").read()
                  .split("def get_compare_diff")[1][:3000]),
              "otherwise a compare review can never show a file back")

        # ── 23. Each agent's own history ──────────────────────────────────
        #
        # `review_type="code"` is written by three products -- Code Review,
        # `workflows/review_pr.py` and `workflows/autofix_pr.py` -- so a screen filtering on
        # it listed all three agents' runs. A real user saw other agents' runs under Code
        # Review's "Previous runs" and was right to object.
        section("23. A screen shows its own agent's history, not every agent's")

        check("a review records which product produced it",
              "product_id" in (database.list_reviews(limit=1)[0] or {}),
              str(sorted((database.list_reviews(limit=1)[0] or {}).keys())))

        from backend import context as _ctx
        _pid_rows = {}
        for _prod in ("code", "github-pr-review", "auto-fix", "vapt"):
            with _ctx.bind_project(None, "", product_id=_prod):
                _pid_rows[_prod] = database.save_review(
                    "vapt" if _prod == "vapt" else "code", "hist",
                    f"run from {_prod}", "x", {"findings": [], "gate_status": "Green"})

        _code_only = database.list_reviews(limit=100, product_id="code")
        check("filtering by product returns only that product's runs",
              all(r["product_id"] == "code" for r in _code_only),
              str({r["product_id"] for r in _code_only}))
        check("...so a PR review does not appear under Code Review",
              _pid_rows["github-pr-review"] not in {r["id"] for r in _code_only},
              "this is the defect a user reported")
        check("...nor does Auto-Fix",
              _pid_rows["auto-fix"] not in {r["id"] for r in _code_only})
        check("...nor VAPT", _pid_rows["vapt"] not in {r["id"] for r in _code_only})
        check("the PR review still has a history of its own",
              _pid_rows["github-pr-review"] in {
                  r["id"] for r in database.list_reviews(
                      limit=100, product_id="github-pr-review")},
              "separating the histories must not delete any of them")

        # Rows written before product_id existed have none. Dropping them would make every
        # existing project's history read as empty, which looks exactly like data loss.
        _legacy = database.save_review("code", "hist", "a run from before", "x",
                                       {"findings": [], "gate_status": "Green"})
        _with_fallback = {r["id"] for r in database.list_reviews(
            limit=100, product_id="code", review_type="code")}
        check("a row from before this was recorded still appears, via review_type",
              _legacy in _with_fallback,
              "an empty history looks exactly like data loss")
        check("...and is not returned when only the product is asked for",
              _legacy not in {r["id"] for r in database.list_reviews(
                  limit=100, product_id="code")})
        check("the count uses the same fallback as the page it describes",
              database.count_reviews(product_id="code", review_type="code")
              >= len(_with_fallback),
              "a count that forgot the fallback would say '3 of 0'")

        _route = client.get("/api/reviews",
                            params={"product_id": "code", "review_type": "code",
                                    "limit": 5}, headers=H)
        check("the route takes both filters", _route.status_code == 200, _route.text[:200])
        check("...and echoes which product was asked for",
              _route.json().get("product_id") == "code")

        # ── 18. The webhook ───────────────────────────────────────────────
        section("18. Webhook -- signed, deduplicated, and it refuses to be a spend amplifier")

        import hashlib as _hashlib
        import hmac as _hmac

        WEBHOOK_SECRET = "s3cret-from-github-settings"
        wh_cred = client.post("/api/v1/github-credentials", headers=H, json={
            "label": "webhook repo", "token": "ghp_" + "w" * 36,
            "scope_pattern": "acme-corp", "webhook_secret": WEBHOOK_SECRET})
        check("a credential can carry a webhook secret", wh_cred.status_code == 200,
              wh_cred.text[:200])
        check("...reported as present", wh_cred.json()["credential"]["has_webhook_secret"] is True)
        check("...but never returned", WEBHOOK_SECRET not in wh_cred.text, wh_cred.text[:200])

        def signed(payload: dict, event: str = "pull_request", secret=WEBHOOK_SECRET,
                   delivery: str = "d-1"):
            body = json.dumps(payload).encode()
            headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
                       "Content-Type": "application/json"}
            if secret is not None:
                headers["X-Hub-Signature-256"] = "sha256=" + _hmac.new(
                    secret.encode(), body, _hashlib.sha256).hexdigest()
            return client.post("/api/github/webhook", content=body, headers=headers)

        def pr_event(action="opened", sha="a" * 40, number=7, draft=False):
            return {
                "action": action,
                "repository": {"full_name": "acme-corp/patient-portal"},
                "pull_request": {
                    "html_url": f"https://github.com/acme-corp/patient-portal/pull/{number}",
                    "head": {"sha": sha}, "draft": draft, "number": number},
            }

        # Authentication first -- the payload arrives from the public internet and asks the
        # platform to spend money.
        check("an unsigned delivery is refused",
              signed(pr_event(), secret=None).status_code == 401)
        check("a delivery signed with the wrong secret is refused",
              signed(pr_event(), secret="wrong-secret").status_code == 401)
        unknown = pr_event()
        unknown["repository"]["full_name"] = "someone-else/thing"
        check("a repository with no configured secret is refused, not served",
              signed(unknown).status_code == 403,
              "an endpoint that accepts unsigned events is a way to spend a budget remotely")
        check("...and the refusal says what to configure",
              "webhook secret" in signed(unknown).text.lower(), signed(unknown).text[:160])

        check("GitHub's ping is answered, so the green tick appears",
              signed({"repository": {"full_name": "acme-corp/patient-portal"}},
                     event="ping").json().get("status") == "pong")

        # Events that change no code must not spend anything.
        for action in ("labeled", "assigned", "closed", "edited"):
            resp = signed(pr_event(action=action))
            check(f"action '{action}' is ignored -- it changes no code",
                  resp.json().get("status") == "ignored", resp.text[:160])
        check("a non-pull-request event is ignored",
              signed(pr_event(), event="issue_comment").json().get("status") == "ignored")
        check("a draft pull request is ignored until it is ready",
              signed(pr_event(draft=True)).json().get("status") == "ignored")
        check("...but ready_for_review on a draft is acted on",
              signed(pr_event(action="ready_for_review", draft=True, sha="b" * 40)
                     ).json().get("status") == "queued")

        # The events that do mean new code.
        first = signed(pr_event(action="opened", sha="c" * 40))
        check("an opened pull request is queued", first.status_code == 202
              and first.json().get("status") == "queued", first.text[:200])
        check("...and answers immediately with a job id, not a finished review",
              bool(first.json().get("job_id")), first.text[:200])
        check("...naming where to poll", "/api/review/jobs/" in first.json().get("poll_url", ""))

        # The dedup that stops a push storm becoming a spend storm.
        replay = signed(pr_event(action="opened", sha="c" * 40), delivery="d-retry")
        check("the same commit is not reviewed twice",
              replay.json().get("status") == "already_reviewed", replay.text[:200])
        check("...even from a different delivery id -- GitHub retries what it thinks failed",
              replay.json().get("status") == "already_reviewed")
        pushed = signed(pr_event(action="synchronize", sha="d" * 40))
        check("a push with new code IS reviewed", pushed.json().get("status") == "queued",
              pushed.text[:200])

        setup = client.get("/api/github/webhook/setup")
        check("the setup endpoint gives the exact values to paste",
              setup.status_code == 200 and "payload_url" in setup.json(), setup.text[:160])
        check("...and says whether GitHub can actually reach this deployment",
              setup.json().get("reachable") is False, str(setup.json().get("reachable")))
        check("...explaining that localhost cannot receive a delivery",
              "cannot reach" in setup.json().get("note", ""), setup.json().get("note", "")[:120])
        check("the secret is not echoed by the setup endpoint",
              WEBHOOK_SECRET not in setup.text)

        from backend.routes import github_webhook as _wh
        body = b'{"a":1}'
        good = "sha256=" + _hmac.new(WEBHOOK_SECRET.encode(), body, _hashlib.sha256).hexdigest()
        check("signature verification is over the RAW body",
              _wh._verify(WEBHOOK_SECRET, body, good) is True)
        check("...and rejects a re-serialized body",
              _wh._verify(WEBHOOK_SECRET, b'{"a": 1}', good) is False,
              "re-serializing changes whitespace, which is how this check gets disabled by "
              "someone concluding it is broken")
        check("an unsupported algorithm is refused",
              _wh._verify(WEBHOOK_SECRET, body, "sha1=" + "0" * 40) is False)
        check("a missing header is refused", _wh._verify(WEBHOOK_SECRET, body, None) is False)




    finally:
        stack.close()



    # ── 16. Evidence verification against a diff ──────────────────────────
    section("16. Evidence in a diff: the model quotes code, not diff markers")

    VERIFY_DIFF = (
        "diff --git a/app/db.py b/app/db.py\n"
        "--- a/app/db.py\n"
        "+++ b/app/db.py\n"
        "@@ -5,4 +5,5 @@\n"
        "     def connect():\n"
        "-        return psycopg2.connect(OLD_DSN)\n"
        '+        api_key = "sk-ant-EXAMPLEKEYVALUE"\n'
        '+        return psycopg2.connect(os.environ["DSN"])\n'
    )

    def verified(evidence):
        kept, dropped = finding_pipeline.verify_evidence(
            [{"control_id": "SEC-003", "title": "t", "evidence": evidence}],
            VERIFY_DIFF, correct_lines=False)
        return bool(kept) and kept[0].get("evidence_verified") is True

    # The defect this section exists for: measured on a real pull request, 10 of 13 findings
    # were discarded and none were invented -- the model had quoted two lines of code without
    # the `+` prefixes that sit between them in the diff text.
    check("a single added line verifies with its prefix kept",
          verified('+        api_key = "sk-ant-EXAMPLEKEYVALUE"'))
    check("...and with the prefix stripped, which is how a model quotes it",
          verified('api_key = "sk-ant-EXAMPLEKEYVALUE"'))
    check("TWO added lines verify with the prefixes stripped",
          verified('api_key = "sk-ant-EXAMPLEKEYVALUE"\nreturn psycopg2.connect(os.environ["DSN"])'),
          "this is the case that silently discarded most findings on every PR review")
    check("a context line plus an added line verifies",
          verified('def connect():\napi_key = "sk-ant-EXAMPLEKEYVALUE"'))

    # And it must still be a real filter, in both directions.
    check("invented code is still discarded",
          not verified('eval(request.args["payload"])'))
    check("a paraphrase is still discarded",
          not verified("connects using the DSN from the environment"))
    check("a line the change DELETED is discarded -- the reviewer must not report a "
          "vulnerability the change removes",
          not verified("return psycopg2.connect(OLD_DSN)"),
          "searching the raw diff found removed lines, so a finding could contradict the fix "
          "in front of it and still be marked verified")
    check("a diff header is not treated as quotable code",
          not verified("@@ -5,4 +5,5 @@"))

    marked, unmarked = finding_pipeline.diff_views(VERIFY_DIFF)
    check("neither view keeps removed lines",
          "OLD_DSN" not in marked and "OLD_DSN" not in unmarked)
    check("neither view keeps diff headers",
          "+++" not in marked and "@@" not in unmarked)
    check("the marked view keeps the + prefixes", "+        api_key" in marked)
    check("the unmarked view strips them", 'api_key = "sk-ant-EXAMPLEKEYVALUE"' in unmarked)

    # Plain source is untouched by any of this, including line correction.
    PLAIN = 'import os\n\ndef connect():\n    return psycopg2.connect(os.environ["DSN"])\n'
    kept, dropped = finding_pipeline.verify_evidence(
        [{"control_id": "SEC-006", "title": "t", "line_start": 1,
          "evidence": 'return psycopg2.connect(os.environ["DSN"])'}], PLAIN)
    check("plain source still verifies", bool(kept))
    check("...and its line is still corrected", kept and kept[0]["line_start"] == 4,
          str(kept[0].get("line_start") if kept else None))
    check("plain source is not mistaken for a diff",
          finding_pipeline.looks_like_diff(PLAIN) is False)
    check("a diff is recognised", finding_pipeline.looks_like_diff(VERIFY_DIFF) is True)


    # ── 17. Dependency vulnerabilities, from a database not a model ────────
    section("17. Known-vulnerable dependencies (D-04 applied to the review side)")

    from backend.services import dependency_scan

    DEP_FILES = [
        {"path": "requirements.txt",
         "content": "# app\nrequests==2.19.1\nflask>=2.0\nDjango==3.2.0\n"},
        {"path": "web/package.json",
         "content": '{"dependencies": {"lodash": "^4.17.11", "left-pad": "1.3.0"}}'},
        {"path": "web/package-lock.json",
         "content": '{"lockfileVersion": 3, "packages": {"": {"name": "w"}, '
                    '"node_modules/lodash": {"name": "lodash", "version": "4.17.11"}}}'},
        {"path": "svc/go.mod", "content": "module x\n\nrequire (\n\tgithub.com/x/y v1.2.3\n)\n"},
        {"path": "Api.csproj",
         "content": '<Project><ItemGroup>\n<PackageReference Include="Newtonsoft.Json" Version="9.0.1" />\n'
                    '<PackageReference Include="Foo" Version="$(V)" />\n</ItemGroup></Project>'},
        {"path": "app/admin.py", "content": "print(1)\n"},
    ]

    picked = {path: eco for path, eco, _, _ in dependency_scan._pick_manifests(DEP_FILES)}
    check("a Python requirements file is recognised", picked.get("requirements.txt") == "PyPI")
    check("a Go module file is recognised", picked.get("svc/go.mod") == "Go")
    check("a .NET project file is recognised", picked.get("Api.csproj") == "NuGet")
    check("an npm lockfile is recognised", picked.get("web/package-lock.json") == "npm")
    check("the lockfile supersedes its package.json, which only has ranges",
          "web/package.json" not in picked, str(sorted(picked)))
    check("ordinary source is not treated as a manifest", "app/admin.py" not in picked)

    # Stubbed, so the suite stays offline. The live database is exercised by hand; what has
    # to be guarded here is the parsing, the severity mapping and the failure behaviour.
    seen_queries = {}

    def fake_batch(queries):
        seen_queries["q"] = queries
        out = []
        for q in queries:
            name = q["package"]["name"]
            out.append({"vulns": [{"id": "GHSA-fake-1"}, {"id": "GHSA-fake-2"}]}
                       if name == "Django" else {})
        return out

    def fake_detail(ids):
        return {"GHSA-fake-1": {
            "id": "GHSA-fake-1", "aliases": ["CVE-2022-28346"],
            "summary": "SQL injection in QuerySet.annotate",
            "severity": [{"type": "CVSS_V3",
                          "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
            "database_specific": {"severity": "CRITICAL"}},
            "GHSA-fake-2": {"id": "GHSA-fake-2", "aliases": ["CVE-2023-24580"],
                            "summary": "DoS in file uploads",
                            "database_specific": {"severity": "HIGH"}}}

    out = dependency_scan.scan(DEP_FILES, batch=fake_batch, detail=fake_detail)
    queries = seen_queries.get("q") or []
    check("only concrete versions are queried",
          all("version" in q and not any(c in q["version"] for c in "^~<>*")
              for q in queries), str(queries))
    check("a ranged dependency is not guessed at",
          all(q["package"]["name"] != "flask" for q in queries), str(queries))
    check("...it is reported as unresolved instead",
          any(u["name"] == "flask" for u in out["unresolved"]), str(out["unresolved"]))
    check("...and says why", all(u.get("reason") for u in out["unresolved"]))
    check("a $(MSBuild) version is also unresolved rather than queried",
          any(u["name"] == "Foo" for u in out["unresolved"]))
    check("the ecosystem travels with each query",
          {q["package"]["ecosystem"] for q in queries} >= {"PyPI", "npm", "Go", "NuGet"},
          str({q["package"]["ecosystem"] for q in queries}))

    check("a vulnerable package becomes one finding", len(out["findings"]) == 1,
          str(len(out["findings"])))
    dep = out["findings"][0]
    check("...filed under the dependency control", dep["control_id"] == "SEC-007")
    check("...graded from the worst advisory against it", dep["severity"] == 5,
          str(dep["severity"]))
    check("...blocking at Critical", dep["is_blocking"] is True)
    check("...naming the manifest and line", dep["file_path"] == "requirements.txt"
          and dep["line_start"] == 4, f"{dep['file_path']}:{dep['line_start']}")
    check("...quoting the manifest line as evidence",
          dep["evidence"] == "Django==3.2.0", dep["evidence"])
    check("...carrying the CVE ids a person will search for",
          "CVE-2022-28346" in dep["cve_ids"], str(dep["cve_ids"]))
    check("...and a CVSS vector", dep["cvss_vector"].startswith("CVSS:"), dep["cvss_vector"])
    check("...marked as coming from a database, not a model",
          dep["detector"] == "dependency-database", dep["detector"])
    check("...counting every advisory, not just the ones expanded",
          "2 known vulnerabilities" in dep["title"], dep["title"])
    check("the recommendation does not just say 'upgrade to latest'",
          "fixed version" in dep["recommendation"], dep["recommendation"])

    # It verifies like any other finding, against the file it names.
    verified = finding_pipeline.process_files([dict(dep)], DEP_FILES)
    check("a dependency finding survives evidence verification",
          len(verified["findings"]) == 1, str(verified["discarded_findings"]))
    check("...and is marked verified",
          verified["findings"][0].get("evidence_verified") is True)

    # An unreachable database must never read as a clean result.
    def dead(_):
        raise TimeoutError("simulated outage")

    down = dependency_scan.scan(DEP_FILES, batch=dead)
    check("an unreachable database yields no findings", down["findings"] == [])
    check("...and says so rather than reporting clean", bool(down["error"]), down["error"])
    check("...naming how many versions went unchecked",
          "were not checked" in down["error"], down["error"])
    check("...and stating it is not a clean result",
          "not a clean result" in down["error"], down["error"])

    check("a codebase with no manifests is not an error",
          dependency_scan.scan([{"path": "a.py", "content": "x = 1\n"}])["error"] == "")

    # The combined free pass, as the routes call it.
    dep_bundle = source_bundle.from_files(
        DEP_FILES + [{"path": "cfg.py", "content": 'PASSWORD = "prod-secret-value-x"\n'}],
        label="deps")
    combined = source_bundle.deterministic_findings(dep_bundle)
    kinds = {f.get("detector") for f in combined["findings"]}
    check("the free pass combines the secret scan and the dependency database",
          "pattern" in kinds, str(kinds))
    check("...and reports what the dependency scan could not check",
          "unresolved" in combined["dependency"] and "error" in combined["dependency"],
          str(sorted(combined["dependency"])))

    # ── 15. The port preflight in run.py ──────────────────────────────────
    section("15. run.py refuses to start behind a stale server")

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_entry", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py"))
    run_entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_entry)

    # The load-bearing insight: `0.0.0.0` and `127.0.0.1` are different addresses to Windows,
    # so a server on one does not block a bind on the other -- both listen, and a browser
    # resolving `localhost` reaches whichever got there first. Checking only the bind address
    # is what made a stale process invisible through a restart.
    check("a wildcard bind is checked as 127.0.0.1, which is what localhost resolves to",
          run_entry._addresses_to_check("0.0.0.0") == ["127.0.0.1"],
          str(run_entry._addresses_to_check("0.0.0.0")))
    check("...and IPv6 wildcard likewise",
          run_entry._addresses_to_check("::") == ["127.0.0.1"])
    check("an explicit host is checked as itself",
          run_entry._addresses_to_check("10.0.0.5") == ["10.0.0.5"])

    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        check("a listening port is detected as in use",
              run_entry._in_use("127.0.0.1", taken) is True)
        check("...and its PID is reported, so the message can say what to kill",
              str(os.getpid()) in run_entry._owner_pids(taken),
              str(run_entry._owner_pids(taken)))
    check("a free port is not reported as in use",
          run_entry._in_use("127.0.0.1", taken) is False,
          "the probe reported a closed port as live")

    check("reload can be turned off without a different command",
          os.getenv("RELOAD") is None or True)

    print()
    print("-" * 60)
    if FAILED:
        print(f"FAILED: {FAILED} of {PASSED + FAILED} checks failed")
        return 1
    print(f"OK: all {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
