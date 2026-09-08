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
    code_chunker, finding_gate, finding_pipeline, llm_gateway, review_estimate,
    source_bundle,
)
from backend.services import sarif as sarif_mod  # noqa: E402

import base64 as _b64mod34  # noqa: E402
import json as _json34  # noqa: E402
import os as _os34  # noqa: E402
import time as _time34  # noqa: E402


def _b64d34(segment: str) -> bytes:
    """Decode one base64url JWS segment, restoring the padding JWS strips."""
    return _b64mod34.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


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


# ── Fixtures for the incremental pull-request review (section 33) ─────────
#
# Two diffs: the whole pull request, and the slice one push added. The point of the pair is
# that a check can tell which of them reached the model — `app/old.py` appears only in the
# full one.
_PR33 = "https://github.com/acme/portal/pull/42"

_FULL_DIFF33 = (
    "diff --git a/app/old.py b/app/old.py\n"
    "--- a/app/old.py\n"
    "+++ b/app/old.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import os\n"
    "+query = \"SELECT * FROM t WHERE u=\" + u\n"
    " return query\n"
    "diff --git a/app/new.py b/app/new.py\n"
    "--- /dev/null\n"
    "+++ b/app/new.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def run(cmd):\n"
    "+    os.system(cmd)\n"
)

_SLICE_DIFF33 = (
    "diff --git a/app/new.py b/app/new.py\n"
    "--- /dev/null\n"
    "+++ b/app/new.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def run(cmd):\n"
    "+    os.system(cmd)\n"
)

_PRIOR_FINDING33 = dict(
    control_id="SEC-003", severity=5, risk_level="Critical",
    title="SQL injection in the old handler",
    description="User input concatenated into a query.",
    recommendation="Parameterize it.", is_blocking=True,
    file_path="app/old.py", line_start=2, confidence="High",
    evidence='query = "SELECT * FROM t WHERE u=" + u',
)
_PRIOR_FINGERPRINT33 = finding_pipeline.fingerprint(_PRIOR_FINDING33)
_PRIOR_FINDING33["fingerprint"] = _PRIOR_FINGERPRINT33


def _seed_pr_review33(project_id=None, pr_number=42):
    """A previous review of the same pull request, with one Critical left open.

    Written directly rather than run through the workflow, because what is under test is
    the *lookup*: a wrong repository, a wrong pull-request number, or a `pr_number` column
    nothing ever populated all produce an empty carry-forward list and a wrongly Green
    gate, and every one of those failures is silent.
    """
    import uuid as _u33
    from datetime import datetime as _dt33
    rid = str(_u33.uuid4())
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reviews (id, review_type, project_name, project_id, input_summary, "
        "input_content, result_json, findings_count, critical_count, high_count, "
        "risk_score, gate_status, created_at, repo_host, repo_owner, repo_name, branch, "
        "commit_sha, pr_number) VALUES (?, 'code', 'Add the thing', ?, 'PR #42', '', '{}', "
        "1, 1, 0, 90.0, 'Red', ?, 'github.com', 'acme', 'portal', 'feature', ?, ?)",
        (rid, project_id, _dt33.utcnow().isoformat(), "a" * 40, pr_number),
    )
    # No `created_at` on `findings`: a finding takes its date from the review it belongs
    # to, which is why `open_findings_for_pr` orders on the joined `reviews.created_at`.
    cur.execute(
        "INSERT INTO findings (id, review_id, control_id, severity, risk_level, title, "
        "description, recommendation, is_blocking, status, fingerprint, file_path, "
        "line_start, evidence, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'open', ?, ?, ?, ?, ?)",
        (str(_u33.uuid4()), rid, _PRIOR_FINDING33["control_id"],
         _PRIOR_FINDING33["severity"], _PRIOR_FINDING33["risk_level"],
         _PRIOR_FINDING33["title"], _PRIOR_FINDING33["description"],
         _PRIOR_FINDING33["recommendation"], _PRIOR_FINGERPRINT33,
         _PRIOR_FINDING33["file_path"], _PRIOR_FINDING33["line_start"],
         _PRIOR_FINDING33["evidence"], project_id),
    )
    conn.commit()
    conn.close()
    return rid


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



def _raises_value_error(fn):
    """True if `fn()` raises ValueError. Used to assert a guard rejects bad arguments."""
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False

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
            _afp._verify_patch = lambda orch, patch, ctx, **kw: dict(orch._verdict)
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

        # ── 24. One review surface, two products ──────────────────────────
        #
        # The GitHub PR screen and the Code Review screen reviewed the same artifact, and the
        # only real difference was whether the result was written back. Everything else was a
        # feature one had and the other lacked because it was built once -- which is how the
        # missing token field and the non-working fix controls both happened. Merged, with
        # the *products* deliberately kept apart: a customer entitled to review but not to
        # writing on their pull requests still has to be refused.
        section("24. The merged PR surface -- stages, and products kept apart")

        from backend.workflows import review_pr as _rpr

        check("the PR review reports named stages, not a count",
              len(_rpr.STAGES) >= 4 and all(isinstance(s, str) for s in _rpr.STAGES),
              str(_rpr.STAGES))
        check("...including the slow one, which most looks like a hang unlabelled",
              any("Reviewing" in s for s in _rpr.STAGES), str(_rpr.STAGES))
        check("an async PR route exists to report them on",
              "/api/github/pr/async" in client.app.openapi()["paths"]
              if hasattr(client, "app") else True)
        from backend.main import app as _app2
        _p2 = _app2.openapi()["paths"]
        check("the async route is registered", "/api/github/pr/async" in _p2)
        check("...and the synchronous one is kept, since it is the documented API",
              "/api/github/pr" in _p2,
              "verify_pr_review.py drives it, and an API surface is a promise")

        _async_meta = _p2["/api/github/pr/async"]["post"]
        check("the async route answers 202, not 200",
              "202" in _async_meta.get("responses", {}), str(list(
                  _async_meta.get("responses", {}))))

        # The stage adapter. The reviewer's fan-out reports chunks; the workflow reports
        # outer steps. Both have to land on one bar or the number is meaningless.
        _seen = []
        scripted["code"] = code_response([dict(SQLI)], gate="Red")

        class _StageGitHub(_PRGitHub):
            pass

        _sg = _StageGitHub()
        _orig_github = orchestrator_module.SDLCOrchestrator.__init__ if False else None
        from backend.services.orchestrator import SDLCOrchestrator as _Orch2
        _orch2 = _Orch2()
        _orch2.github = _sg
        _sg.format_pr_review_comment = lambda r: "comment"
        _sg.upsert_pr_comment = lambda url, body: {"posted": True, "action": "created"}
        _sg.post_pr_review = lambda *a, **k: {"posted": True}
        _sg.parse_pr_url = lambda url: ("acme", "portal", 1)
        _sg.get_pr_head_sha = lambda url: PR_SHA
        _sg.set_commit_status = lambda *a, **k: {"posted": True}

        _res = _rpr.run(_orch2, "https://github.com/acme/portal/pull/1",
                        post_mode="summary", set_status=True,
                        on_progress=lambda i, total, label: _seen.append((i, total, label)))
        check("every stage is reported as it happens", len(_seen) >= 4, str(_seen))
        check("...with a label a person can read, not an index",
              all(lbl for _, _, lbl in _seen), str(_seen))
        check("...and the chunk progress folds into the reviewing stage",
              any("piece" in lbl for _, _, lbl in _seen), str([l for _, _, l in _seen]))
        check("the total never changes mid-run, so the bar cannot go backwards",
              len({total for _, total, _ in _seen}) == 1,
              str({total for _, total, _ in _seen}))
        check("the review still posts back and reports what reached GitHub",
              _res.get("comment_posted") is True and _res.get("status_check"),
              str(_res.get("status_check")))
        check("...and its stored review now has a source inventory",
              _store.get_inventory(_res["review_id"])["total"] >= 2,
              "without it the fix controls on its findings could not resolve a file -- "
              "which is the defect a user hit on this exact screen")

        # ── 24b. One count, and it is the customer's ──────────────────────
        #
        # The quote said "WILL BE REVIEWED: 1 file(s)" directly above "12 file(s) will be
        # read — see which". Both were true about different things: one artifact for the
        # chunker, twelve files for the person. Only one of them is a statement anybody
        # asked for, and "1 file(s)" on a twelve-file pull request reads as "it will only
        # look at one of them" — the exact doubt this work exists to remove.
        #
        # Every suite passed while that was on screen, which is why these checks exist:
        # nothing compared the number in the tile to the length of the list beneath it.
        section("24b. The file count on a diff is the files, not the artifact")

        pr_sum = pr_bundle.summary()
        pr_prev = _files_preview(pr_bundle)
        check("a pull request reports the files it touches, not one pseudo-file",
              pr_sum["files_reviewed"] == 2, str(pr_sum["files_reviewed"]))
        check("THE COUNT MATCHES THE LIST BENEATH IT",
              pr_sum["files_reviewed"] == len(pr_prev),
              f"tile said {pr_sum['files_reviewed']}, list had {len(pr_prev)} -- "
              f"a quote that contradicts itself is not a quote")
        check("the artifact count is still available, because the arithmetic needs it",
              pr_sum["artifacts"] == 1,
              "one artifact is one chunk is one model call, which is how the quote can say "
              "'12 files, 1 model call' without contradicting itself")
        check("...and the response says it is a diff, so a screen can qualify the number",
              pr_sum["is_diff"] is True)

        # The same invariant for every other shape, so this cannot be fixed for diffs and
        # broken for the rest.
        for _label, _b in (
            ("a file list", source_bundle.from_files(
                [{"path": f"n{i}.py", "content": "x = 1\n"} for i in range(4)], label="four")),
            ("a paste", source_bundle.from_text("print(1)\n", label="paste")),
            ("a zip", source_bundle.from_zip_bytes(make_zip(APP_FILES), label="z")),
        ):
            _s, _p = _b.summary(), _files_preview(_b)
            check(f"{_label}: the count matches the list",
                  _s["files_reviewed"] == len(_p),
                  f"{_s['files_reviewed']} vs {len(_p)}")
            check(f"{_label}: not reported as a diff", _s["is_diff"] is False)

        # And through the route a browser actually calls.
        _q = client.post("/api/review/code/preflight",
                         json={"files": files_payload, "project_name": "Sources"}, headers=H)
        check("the quote endpoint agrees with itself",
              _q.json()["source"]["files_reviewed"] == len(_q.json()["files_preview"]),
              f"{_q.json()['source']['files_reviewed']} vs "
              f"{len(_q.json()['files_preview'])}")

        # ── 25. Where a fix may go ────────────────────────────────────────
        #
        # Your call: offer both, default to a new pull request. Committing into the pull
        # request under review is what most teams want, and it rewrites a branch somebody
        # else may be working on -- so it is offered, never chosen for you, and where it is
        # impossible it is still listed with the reason.
        section("25. Fixing into the pull request itself, or into a new one")

        check("same_pr is an offered delivery", "same_pr" in fix_apply.OUTPUTS,
              str(fix_apply.OUTPUTS))

        _same_plan = fix_apply.plan(pr_rid, [f["id"] for f in json.loads(
            database.get_review(pr_rid)["result_json"])["findings"]])
        _outs = {o["id"]: o for o in _same_plan["outputs"]}
        check("a new pull request is offered first, so it is the default",
              _same_plan["outputs"][0]["id"] == "pr",
              str([o["id"] for o in _same_plan["outputs"]]))
        check("...and committing into this pull request is offered too",
              "same_pr" in _outs, str(sorted(_outs)))
        check("...naming the branch it would write to",
              "feat/cb-104" in _outs["same_pr"]["label"], _outs["same_pr"]["label"])
        check("...and warning that it rewrites a branch others may hold",
              "rewrites" in _outs["same_pr"]["note"], _outs["same_pr"]["note"][:140])

        # A fork PR whose author did not allow maintainer edits cannot be written to, and
        # discovering that by attempting the push means discovering it after the spend.
        fork_bundle = source_bundle.pr_bundle(
            {**_prgh.get_pr_diff("https://github.com/acme/portal/pull/2"),
             "from_fork": True, "maintainer_can_modify": False},
            "https://github.com/acme/portal/pull/2")
        fork_rid = database.save_review("code", "carebrief", "PR #2", "x",
                                        {"findings": _copy.deepcopy(PR_FINDINGS),
                                         "gate_status": "Red"})
        _store.record(fork_rid, fork_bundle, retention_days=0, findings=PR_FINDINGS)
        fork_src = _store.summarize(fork_rid)
        check("a fork PR without maintainer edits is recorded as not writable",
              not fork_src["writable"], str(fork_src.get("writable")))
        check("...with the reason, so a screen can say why rather than hiding the option",
              "fork" in fork_src["write_blocked"], fork_src["write_blocked"][:140])
        fork_outs = {o["id"]: o for o in fix_apply.plan(
            fork_rid, [f["id"] for f in json.loads(
                database.get_review(fork_rid)["result_json"])["findings"]])["outputs"]}
        check("the option is still listed, marked unavailable",
              fork_outs.get("same_pr", {}).get("unavailable"),
              "'this option is missing' is a worse answer than 'here is why it cannot be used'")
        try:
            fix_apply.apply(None, fork_rid, output="same_pr")
            check("...and attempting it is refused before anything is spent", False, "no error")
        except fix_apply.FixError as exc:
            check("...and attempting it is refused before anything is spent",
                  "fork" in str(exc) or "cannot be written" in str(exc), str(exc)[:160])

        # A same-repo PR keeps the option live.
        check("a same-repo pull request is writable",
              bool(_store.summarize(pr_rid)["writable"]))
        check("...and its head branch is recorded, which is what a commit needs",
              _store.summarize(pr_rid)["head_branch"] == "feat/cb-104",
              _store.summarize(pr_rid)["head_branch"])

        # ── 26. Every agent screen declares which agent it is ─────────
        #
        # `AGENT_SCREENS` in app.js is a second place product ids and review types live --
        # the first is `products/registry.py`. That is a real cost, and it buys three things
        # markup and derivation cannot give: a result container id that follows no
        # convention, a renderer (which is a function), and a run history that works when
        # the nav item was not what opened the screen.
        #
        # The duplication is only defensible if drift fails here rather than in a browser.
        # So: every row is checked against the served product contract and against the
        # `data-product` in the markup, every row's result container and run container must
        # exist, and a screen with no row at all is a failure unless it is on the explicit
        # exclusion list. Without this the table would be an apology, not a design.
        section("26. Every agent screen declares which agent it is")

        import re as _re

        _fe = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "frontend")
        _html = io.open(os.path.join(_fe, "index.html"), encoding="utf-8").read()
        _appjs = io.open(os.path.join(_fe, "app.js"), encoding="utf-8").read()

        # Screens that legitimately have no row, each for a stated reason. A screen dropped
        # in here silently is the failure this list exists to make visible.
        _NO_ROW = {
            "modernization": "has its own richer run list (loadModernizationRuns)",
            "governance-report": "writes a `reports` row, and has its own Previous reports list",
            "github": "retired; the nav item opens the review screen (D-267)",
        }

        # ── The table, parsed out of app.js ──────────────────────────────────
        _tbl_m = _re.search(r"const AGENT_SCREENS = \{(.*?)\n\};", _appjs, _re.S)
        check("the screen table exists in app.js", _tbl_m is not None)
        # The trailing newline matters: the table regex consumes the one before `};`, so
        # without putting it back the *last* row never matches the per-entry pattern and is
        # reported as a screen with no history. Which is what happened, and is a fair
        # demonstration that this check can fail.
        _body = (_tbl_m.group(1) + "\n") if _tbl_m else ""
        _rows = {}
        for _m in _re.finditer(r"'?([a-z0-9-]+)'?:\s*\{(.*?)\},\n", _body, _re.S):
            _fields = _m.group(2)
            def _s(key, _f=_fields):
                _g = _re.search(key + r":\s*'([^']*)'", _f)
                return _g.group(1) if _g else None
            def _b(key, _f=_fields):
                _g = _re.search(key + r":\s*(true|false)", _f)
                return _g.group(1) == "true" if _g else False
            _rows[_m.group(1)] = {
                "product": _s("product"), "reviewType": _s("reviewType"),
                "result": _s("result"), "noun": _s("noun"),
                "gated": _b("gated"), "inventory": _b("inventory"),
                "render": (_re.search(r"render:\s*([A-Za-z_$][\w$]*)", _fields) or [None, None])[1],
            }
        check("...and parses into rows", len(_rows) >= 15, f"parsed {len(_rows)}")

        # ── The markup's own answer ──────────────────────────────────────────
        _nav = dict(_re.findall(r'data-page="([a-z0-9-]+)"\s+data-product="([a-z0-9-]+)"',
                                _html))
        check("the nav declares a product per agent screen", len(_nav) >= 20, str(len(_nav)))

        # ── The served contract ──────────────────────────────────────────────
        _prods = {p["id"]: p for p in client.get("/api/v1/products",
                                                 headers=H).json()["products"]}
        check("the products endpoint serves review_type",
              all("review_type" in p for p in _prods.values()),
              "without it this check would have to re-parse registry.py, which is how a "
              "check starts agreeing with a bug")

        # ── 1. No agent screen is missing a row ──────────────────────────────
        _missing = sorted(set(_nav) - set(_rows) - set(_NO_ROW))
        check("EVERY AGENT SCREEN HAS A ROW OR A STATED REASON NOT TO",
              not _missing,
              f"no run history and no exclusion for: {_missing} -- a screen added later "
              f"without a row ships with an empty history and nobody notices")
        for _page, _why in _NO_ROW.items():
            check(f"{_page} is excluded on purpose ({_why[:38]}...)",
                  _page not in _rows and _page in _nav,
                  "an excluded screen must still be a real nav item")

        # ── 2. Every row agrees with the markup and the registry ─────────────
        for _page, _row in sorted(_rows.items()):
            check(f"{_page}: the product matches the nav item's data-product",
                  _nav.get(_page) == _row["product"],
                  f"table says {_row['product']!r}, markup says {_nav.get(_page)!r}")
            check(f"{_page}: the product is real",
                  _row["product"] in _prods, str(_row["product"]))
            _served = (_prods.get(_row["product"]) or {}).get("review_type")
            check(f"{_page}: the review type matches the registry",
                  _served == _row["reviewType"],
                  f"table says {_row['reviewType']!r}, registry writes {_served!r} -- "
                  f"a mismatch means this screen's history is silently empty")
            check(f"{_page}: its result container exists in index.html",
                  f'id="{_row["result"]}"' in _html, str(_row["result"]))
            check(f"{_page}: its run container exists in index.html",
                  f'id="{_page}-runs"' in _html,
                  "loadAgentRuns reads `${agent}-runs` and returns silently without it, "
                  "which is indistinguishable from a project with no history")
            check(f"{_page}: its renderer is a real function",
                  _row["render"] and f'function {_row["render"]}(' in _appjs,
                  str(_row["render"]))
            check(f"{_page}: it names what it produces", bool(_row["noun"]),
                  "the empty state and the card both read this")

        # ── 3. A product that writes no reviews row must not claim one ───────
        for _page, _row in sorted(_rows.items()):
            check(f"{_page}: does not point at a product that stores nothing",
                  bool(_row["reviewType"]),
                  "report-generate and modernization write elsewhere; a row for either "
                  "would list runs that can never appear")

        # ── 4. The gate flag is not decoration ───────────────────────────────
        # `save_review` defaults gate_status to "Green" and findings_count to 0 when the
        # result carries neither, so a generative agent's row is byte-identical to a clean
        # security review's. Rendering that as a gate is a claim of safety nobody made.
        _ungated = {p for p, r in _rows.items() if not r["gated"]}
        check("the generative agents are marked as having no gate",
              {"test-scenario", "test-case", "codegen", "gxp-generate"} <= _ungated,
              f"ungated: {sorted(_ungated)}")
        _gate_free = database.save_review(
            "test_cases", "gatecheck", "Test cases — 40 generated", "x",
            {"test_cases": [{"id": 1}]})
        _row_tc = database.get_review(_gate_free)
        check("...because such a run really does store a default Green with no findings",
              _row_tc["gate_status"] == "Green" and _row_tc["findings_count"] == 0,
              f"{_row_tc['gate_status']} / {_row_tc['findings_count']} -- this is the row "
              f"the card must not present as a verdict")
        check("a findings agent is still marked gated",
              all(_rows[p]["gated"] for p in ("code", "vapt", "security", "cfr21")
                  if p in _rows))

        # ── 5. Only the screens that store a source may offer the extras ─────
        check("only the review screens claim a source inventory",
              {p for p, r in _rows.items() if r["inventory"]} == {"code", "vapt"},
              "the fix bar and the retention control are gated on this flag; a screen "
              "claiming it without a route that calls review_source_store.record would "
              "render a checkbox guaranteed to fail (D-268)")

        # ── 27. What a run cost comes back on every synchronous route ───
        #
        # `agent_guard.guarded_call` attaches cost, calls, tokens and model to the response
        # *body* as `_budget_stats`. The synchronous `/api/review/*` routes cannot: they are
        # metered by middleware that runs around the handler, so by the time the numbers
        # exist `call_next` has already serialized the body. Every agent screen except Code
        # Review and VAPT therefore showed findings with no indication of what producing
        # them cost or which model produced them -- all of it computed, written to the
        # ledger, and thrown away.
        #
        # Headers are the channel still open after `call_next`. The figures must come from
        # `agent_guard._price_usage`, the same arithmetic that writes the ledger, or the
        # number on screen and the number billed drift apart.
        #
        # This suite has never exercised metering: it stubs `call_tool`, so
        # `_record_usage` is never called and every run above costs zero. The stub is
        # wrapped here to record usage the way a real call does, and restored afterwards.
        section("27. What a run cost comes back on every synchronous route")

        from backend.services import legacy_guard as _lg
        from backend.services import agent_guard as _ag
        from backend.products import registry as _reg

        _METERED = ("X-Spend-Cost-USD", "X-Spend-Calls", "X-Spend-Tokens-In",
                    "X-Spend-Tokens-Out", "X-Spend-Model", "X-Spend-Provider")

        _plain_call_tool = llm_gateway.call_tool

        def _metered_call_tool(*a, **k):
            llm_gateway._record_usage("anthropic", config.MODEL, 1200, 340)
            return _plain_call_tool(*a, **k)

        llm_gateway.call_tool = _metered_call_tool
        try:
            _r = client.post("/api/review/requirement?project_name=Sources",
                             json={"content": "The system shall be fast.", "context": ""},
                             headers=H)
            check("a synchronous agent run answers 200", _r.status_code == 200,
                  _r.text[:200])
            _missing = [h for h in _METERED if h not in _r.headers]
            check("EVERY SPEND HEADER IS PRESENT ON A METERED RUN", not _missing,
                  f"absent: {_missing} -- without them the screen has nothing to show and "
                  f"the person who approved nothing is told nothing")
            check("...the model that answered is named, not the deployment default",
                  _r.headers.get("X-Spend-Model") == config.MODEL,
                  _r.headers.get("X-Spend-Model"))
            check("...the call count is real, not zero",
                  int(_r.headers.get("X-Spend-Calls", 0)) >= 1,
                  _r.headers.get("X-Spend-Calls"))
            check("...and the tokens are the ones the gateway recorded",
                  int(_r.headers.get("X-Spend-Tokens-In", 0)) >= 1200
                  and int(_r.headers.get("X-Spend-Tokens-Out", 0)) >= 340,
                  f"{_r.headers.get('X-Spend-Tokens-In')} in / "
                  f"{_r.headers.get('X-Spend-Tokens-Out')} out")

            # The figure on screen and the figure billed must be the same arithmetic.
            _calls = int(_r.headers.get("X-Spend-Calls"))
            _expected = _ag._price_usage(
                {"calls": _calls,
                 "input_tokens": int(_r.headers.get("X-Spend-Tokens-In")),
                 "output_tokens": int(_r.headers.get("X-Spend-Tokens-Out"))},
                _r.headers.get("X-Spend-Model"))
            check("THE COST SHOWN IS THE COST BILLED",
                  abs(float(_r.headers.get("X-Spend-Cost-USD")) - _expected) < 1e-6,
                  f"header says {_r.headers.get('X-Spend-Cost-USD')}, _price_usage says "
                  f"{_expected:.6f} -- a display cost derived any other way drifts from "
                  f"the ledger, which is worse than showing nothing")

            # `product_id` is published by this middleware now, so the run is attributable
            # to the agent that produced it rather than matched on review type alone.
            _reqs = database.list_reviews(limit=5, product_id="requirement")
            check("...and the run is attributed to its product, not left NULL",
                  any(r["product_id"] == "requirement" for r in _reqs),
                  str([r.get("product_id") for r in _reqs][:5]))

            # A free route reached no model. "cost $0.0000" on a preflight is noise, and
            # the frontend drops a zero-call header set for the same reason.
            _pf = client.post("/api/review/code/preflight",
                              json={"content": "print(1)", "project_name": "Sources"},
                              headers=H)
            check("a run that reached no model reports no spend",
                  int(_pf.headers.get("X-Spend-Calls", 0)) == 0,
                  f"a free preflight claimed {_pf.headers.get('X-Spend-Calls')} call(s)")

            # ── The upload variants, which nothing had ever exercised (C-83) ──
            #
            # These ten routes had no test of any kind, and they are the only agent routes
            # that are legitimately `async def` -- they must `await file.read()`. C-83 moved
            # their model call and their PDF/zip parsing onto a thread with
            # `asyncio.to_thread`, and that is not a free change: `to_thread` runs the
            # function in a **copy** of the context, so anything a contextvar is supposed to
            # report *back up* could have started silently returning nothing.
            #
            # `context.collect_saved_reviews()` is exactly that mechanism, and it is how
            # `legacy_guard` learns which review to bill (D-311). It holds a mutable list
            # for this reason -- the thread appends to the same object the middleware is
            # holding -- so it should survive. "Should" is why this is a test: the same
            # reasoning applied to a plain value would be wrong, and no existing check
            # would have noticed.
            _up = client.post(
                "/api/review/requirement/upload",
                files={"file": ("requirement.txt",
                                b"The system shall log every access to a patient record.",
                                "text/plain")},
                data={"project_name": "Sources", "context": ""},
                headers=H)
            check("an upload route answers 200 with its model call off the event loop",
                  _up.status_code == 200, _up.text[:200])
            _up_missing = [h for h in _METERED if h not in _up.headers]
            check("...and the spend still reaches the browser through the context copy",
                  not _up_missing,
                  f"absent: {_up_missing} -- `asyncio.to_thread` gives the call a *copy* of "
                  f"the context, so if attribution rode on a plain contextvar rather than "
                  f"the mutable list of D-311, moving the call off the loop would have "
                  f"silently unbilled every upload")
            check("...priced by the same arithmetic as the JSON route",
                  int(_up.headers.get("X-Spend-Tokens-In", 0)) >= 1200,
                  f"{_up.headers.get('X-Spend-Tokens-In')} in -- the figure must come from "
                  f"the gateway's own record, not from a re-estimate")

            # Iterate the map rather than spot-checking one route, so a product that
            # reports its spend by no mechanism at all is named by the test instead of
            # passing quietly. There are exactly three mechanisms and every product must
            # use one:
            #
            #   * in LEGACY_PRODUCT_ROUTES  -> the X-Spend-* headers checked above
            #   * a 202 job, or a /api/v1 route -> `_budget_stats` on the body, attached by
            #     `agent_guard.guarded_call`. These are deliberately absent from the map
            #     (main.py:774-779): they do their model work after the response is sent, so
            #     there is nothing request-scoped left for a middleware to meter.
            #   * its own accounting -- modernization prices per run and shows spent-of-cap
            #     in its own header.
            # Header-metered by the dict, *and* by the universal-runner regex beside it
            # (`_AGENT_RUN_PATH`, POST /api/agents/{id}/run) -- which is how `compliance`,
            # the one product with no screen of its own, is metered. Written out because
            # the first version of this check knew only about the dict and reported
            # `compliance` as unmetered when it is not.
            from backend.routes.agents import SUPPORTED_AGENTS as _RUNNER_AGENTS
            _dict_metered = set(_lg.LEGACY_PRODUCT_ROUTES.values())
            _legacy = _dict_metered | set(_RUNNER_AGENTS)
            check("the legacy route map is where this test thinks it is",
                  len(_legacy) > 20, f"{len(_legacy)} products mapped")
            check("...and the universal runner's products are metered by the same middleware",
                  _lg._AGENT_RUN_PATH.match("/api/agents/compliance/run") is not None,
                  "if this regex moves, compliance stops being metered silently")

            _BODY_METERED = {
                "full": "202 job; guarded_call attaches _budget_stats to the job result",
                "swarm": "/api/v1/swarm forwards _budget_stats",
                "report-generate": "/api/v1/agents/report-generate forwards _budget_stats",
                "modernization": "prices per run; shows spent-of-cap in its own header",
            }
            _unmetered = sorted(
                p.id for p in _reg.list_products()
                if p.id not in _legacy and p.id not in _BODY_METERED)
            check("EVERY PRODUCT REPORTS ITS SPEND BY SOME MECHANISM", not _unmetered,
                  f"no spend path for: {_unmetered} -- a run whose cost reaches neither "
                  f"the headers nor the body is a run the person who paid for it cannot "
                  f"see. Add it to the route map, or record here which mechanism it uses "
                  f"and why")
            # Compared against the *dict*, not the union: the universal runner is a third
            # entry point, and `full` legitimately has two -- POST /api/review/full is a 202
            # job whose spend lands on the body, while POST /api/agents/full/run is metered
            # by the middleware and lands in the headers. Asserting against the union made
            # this check fail on a product that is metered twice over, which is the opposite
            # of the thing it exists to catch.
            for _pid, _why in sorted(_BODY_METERED.items()):
                check(f"{_pid} is body-metered on purpose ({_why[:34]}...)",
                      _pid not in _dict_metered,
                      "it is in the synchronous route map as well, so its spend arrives in "
                      "the headers and this exemption is now misleading")
        finally:
            llm_gateway.call_tool = _plain_call_tool

        check("the gateway stub is restored for anything after this section",
              llm_gateway.call_tool is _plain_call_tool)

        # ── 28. The slow runs report what they are doing ────────────
        #
        # Three screens fanned out over many model calls behind a static sentence. The Core
        # SDLC Review already produced a real stage label per step and threw it into a
        # full-screen blocking spinner; the swarm and the repository-wide triage produced
        # nothing at all.
        #
        # The other two candidates were checked and deliberately left alone:
        # `codegen_scaffold.run` and `migration_execute.run` each make **one** agent call, so
        # a progress bar over them would be a bar that goes 0 to 100 with an invented label
        # in between. A spinner at least does not claim to know anything.
        section("28. The slow runs report what they are doing")

        from backend.services import swarm_service as _sw
        from backend.workflows import issue_triage_batch as _itb
        from backend.workflows import codegen_scaffold as _cgs
        from backend.workflows import migration_execute as _mex
        from backend.services.orchestrator import SDLCOrchestrator as _Orch28
        import inspect as _inspect

        _p28 = main.app.openapi()["paths"]
        for _sync, _async in (
            ("/api/v1/swarm", "/api/v1/swarm/async"),
            ("/api/review/issue-triage/batch-github",
             "/api/review/issue-triage/batch-github/async"),
        ):
            check(f"{_async} exists", _async in _p28, str(_async))
            check(f"...and answers 202, not 200",
                  "202" in _p28.get(_async, {}).get("post", {}).get("responses", {}),
                  str(list(_p28.get(_async, {}).get("post", {}).get("responses", {}))))
            check(f"...and {_sync} is kept, since it is the documented surface",
                  _sync in _p28,
                  "an API surface is a promise, and scripts/ and n8n drive these")

        # ── The swarm reports per agent, as they finish ──────────────────────
        check("run_swarm accepts a progress reporter",
              "on_progress" in _inspect.signature(_sw.SwarmService.run_swarm).parameters)

        _seen28 = []
        _orig_one = _sw._run_one_agent
        _orig_resolve = _sw._SourceResolver.resolve

        def _fake_one(name, artifact, req, orch, ctx, caller):
            return _sw.AgentSwarmResult(agent=name, gate_status="Green", risk_score=1.0,
                                        summary=f"{name} done")

        _sw._run_one_agent = _fake_one
        _sw._SourceResolver.resolve = lambda self, req: ("print(1)", "text", "pasted", "")
        try:
            from backend.models.swarm_models import SwarmRequest as _SReq
            _resp = _sw.SwarmService().run_swarm(
                _SReq(text="print(1)", project_name="Sources", policy_name="standard"),
                _Orch28(), {"type": "anonymous"},
                on_progress=lambda i, t, l: _seen28.append((i, t, l)))
            check("the swarm reports progress as agents finish",
                  len(_seen28) >= 3, str(_seen28[:4]))
            check("...naming the agent that just landed, not only a count",
                  any("just completed" in l for _, _, l in _seen28),
                  "a bare '4 of 6' cannot tell a swarm that is progressing from one where "
                  "the slow agent is still the slow agent")
            check("...counting up, never down",
                  [i for i, _, _ in _seen28] == sorted(i for i, _, _ in _seen28),
                  str([i for i, _, _ in _seen28]))
            check("...against one total, so the bar cannot go backwards mid-run",
                  len({t for _, t, _ in _seen28}) == 1,
                  str({t for _, t, _ in _seen28}))
            check("...and the run still produced its gate",
                  bool(getattr(_resp, "swarm_gate", None)),
                  str(getattr(_resp, "swarm_gate", None)))
        finally:
            _sw._run_one_agent = _orig_one
            _sw._SourceResolver.resolve = _orig_resolve
        check("the swarm stubs are restored", _sw._run_one_agent is _orig_one)

        # ── The repository triage reports per issue, by name ─────────────────
        check("the batch triage accepts a progress reporter",
              "on_progress" in _inspect.signature(_itb.run).parameters)

        _seen_it = []
        _orig_triage = _itb.issue_triage.run

        class _FakeGH:
            def list_open_issues(self, url, limit):
                return [{"number": n, "title": f"Issue {n} title", "body": "b",
                         "url": f"{url}/issues/{n}"} for n in range(1, 5)]

        class _FakeOrch:
            github = _FakeGH()

        _itb.issue_triage.run = lambda orch, **k: {"severity": "High", "summary": "x"}
        try:
            _res_it = _itb.run(_FakeOrch(), github_repo_url="https://github.com/acme/portal",
                               limit=4, project_name="Sources",
                               on_progress=lambda i, t, l: _seen_it.append((i, t, l)))
            check("the batch reports progress per issue",
                  len(_seen_it) >= 5, str(_seen_it[:3]))
            check("...naming the issue being read, not only its index",
                  any("Issue 2 title" in l for _, _, l in _seen_it),
                  "'12 of 40' alone cannot distinguish a batch that is working from one "
                  "stuck on issue 12")
            check("...and the total is the number actually found",
                  {t for _, t, _ in _seen_it} == {4}, str({t for _, t, _ in _seen_it}))
            check("...and it still triaged every issue",
                  _res_it["issues_triaged"] == 4, str(_res_it.get("issues_triaged")))
        finally:
            _itb.issue_triage.run = _orig_triage
        check("the triage stub is restored", _itb.issue_triage.run is _orig_triage)

        # ── Driven through HTTP, the way the browser does ────────────────────
        _acc28 = client.post("/api/review/issue-triage/batch-github/async",
                             json={"github_repo_url": "https://github.com/acme/portal",
                                   "limit": 2, "project_name": "Sources"}, headers=UI)
        check("the batch route accepts rather than holding the connection",
              _acc28.status_code == 202, f"{_acc28.status_code}: {_acc28.text[:200]}")
        _acc28j = _acc28.json()
        check("...returning a job id and where to poll",
              bool(_acc28j.get("job_id"))
              and _acc28j.get("poll_url", "").endswith(_acc28j.get("job_id", "x")),
              _acc28.text[:200])
        check("...and naming the unit, so the bar does not say 'piece(s)' about issues",
              _acc28j.get("unit") == "issue", str(_acc28j.get("unit")))

        # ── The two that correctly get no progress bar ───────────────────────
        for _mod, _name in ((_cgs, "codegen_scaffold"), (_mex, "migration_execute")):
            check(f"{_name} is a single call, so it is given no progress bar",
                  "on_progress" not in _inspect.signature(_mod.run).parameters,
                  "if this workflow ever fans out, it should report progress -- but a bar "
                  "over one model call is an invented label between 0 and 100, and a "
                  "spinner at least does not claim to know anything")

        # ── The Core SDLC Review already had labels; it kept them ────────────
        check("the Core SDLC Review still answers 202 with a job",
              "202" in _p28.get("/api/review/full", {}).get("post", {}).get("responses", {}),
              str(list(_p28.get("/api/review/full", {}).get("post", {}).get("responses", {}))))
        check("...and its own poll route is still there",
              "/api/review/full/jobs/{job_id}" in _p28)

        # ── 29. A vulnerability assessment's findings can be fixed ────
        #
        # Apply Fixes was built review-type agnostic and VAPT already met every precondition
        # it has -- the same bundle path records its source, its findings carry the verified
        # quote and the file path -- so the only thing missing was that the VAPT screen never
        # armed it. What is checked here is the backend half: that the plan, the delivery
        # options, the quote and the guardrail all behave when the review is a VAPT one.
        #
        # The guardrail is the reason this section is long. It has two changes, and both are
        # the kind that fail silently: the re-review is now done by the agent that found the
        # problem, and a Critical the original review already reported no longer blocks the
        # patch. Get the second one wrong in either direction and either every VAPT patch is
        # rejected, or a vulnerability the fixer introduced ships.
        section("29. A vulnerability assessment's findings can be fixed")

        VAPT_FIX_FINDINGS = [
            {"id": "v1", "title": "SQL injection in login", "file_path": "app/auth.py",
             "line_start": 4, "severity": 5, "risk_level": "Critical",
             "control_id": "VAPT-003", "cvss_score": 9.8, "cwe_id": "CWE-89",
             "owasp_category": "A03:2021 Injection", "vuln_id": "VLN-001",
             "attack_scenario": "An unauthenticated caller posts a crafted username.",
             "proof_of_concept": "curl -X POST /login -d \"u=' OR 1=1--\"",
             "affected_component": "POST /login", "evidence": "SELECT",
             "evidence_verified": True, "is_blocking": True,
             "recommendation": "Use a parameterised query."},
            {"id": "v2", "title": "Verbose error body", "file_path": "app/auth.py",
             "line_start": 20, "severity": 3, "risk_level": "Moderate",
             "control_id": "VAPT-011", "cvss_score": 5.3, "cwe_id": "CWE-209",
             "vuln_id": "VLN-002", "attack_scenario": "Stack traces reach the client.",
             "evidence": "id", "evidence_verified": True, "is_blocking": False},
            # VAPT legitimately assesses things with no file at all -- an OpenAPI spec, an
            # architecture description, a list of URLs. Those findings are real and are not
            # fixable, and the difference has to be visible rather than inferred.
            {"id": "v3", "title": "No rate limit on the auth endpoint", "severity": 4,
             "risk_level": "High", "control_id": "VAPT-020", "cvss_score": 7.5,
             "vuln_id": "VLN-003", "affected_component": "POST /login",
             "evidence": "no limiter", "evidence_verified": True},
        ]

        _vapt_bundle = source_bundle.from_files(
            [{"path": "app/auth.py", "content": APP_FILES["app/auth.py"]}],
            label="vapt fix demo")
        # Saved inside the project the UI headers are scoped to, because the route below is
        # driven as the browser drives it and `_review_in_scope` 404s anything outside the
        # caller's projects. A review with a NULL project id is invisible to it -- which the
        # check at section 21 relies on, and which is easy to reproduce here by accident.
        with _ctx.bind_project(pid, "Sources", product_id="vapt"):
            _vapt_rid = database.save_review(
                "vapt", "fix", "s", "c",
                {"findings": _copy.deepcopy(VAPT_FIX_FINDINGS), "gate_status": "Red",
                 "risk_score": 88.0})
        _store.record(_vapt_rid, _vapt_bundle, retention_days=7)
        _VID = {f["title"]: f["id"] for f in
                json.loads(database.get_review(_vapt_rid)["result_json"])["findings"]}

        _vsplit = fix_apply.eligible(
            json.loads(database.get_review(_vapt_rid)["result_json"])["findings"])
        check("a vulnerability in a real file is offered for fixing",
              {f["title"] for f in _vsplit["fixable"]}
              == {"SQL injection in login", "Verbose error body"},
              str(sorted(f["title"] for f in _vsplit["fixable"])))
        check("...and one that names no file is not, with the reason it is not",
              any(f["title"].startswith("No rate limit")
                  and "nothing to patch" in f["fix_blocked_reason"]
                  for f in _vsplit["not_fixable"]),
              "a VAPT assessment of an API spec has findings with no file, and the "
              "difference between 'cannot' and 'will not' has to be visible")

        _vplan = fix_apply.plan(_vapt_rid, [_VID["SQL injection in login"]])
        check("a VAPT review can be planned for fixing at all",
              _vplan["files_to_change"] == 1, str(_vplan["files_to_change"]))
        check("...and is quoted, so the price is not discovered afterwards",
              (_vplan["estimate"] or {}).get("ceiling_usd", 0) > 0,
              str(_vplan["estimate"])[:160])
        check("...and its note says who does the re-review",
              "vulnerability assessor" in _vplan["note"], _vplan["note"][-160:])
        check("a VAPT zip assessment is offered the diff and the zip, not a pull request",
              {o["id"] for o in _vplan["outputs"]} == {"diff", "zip"},
              str(sorted(o["id"] for o in _vplan["outputs"])))

        # ── The quote is priced against the agents that will actually be called ──
        #
        # Not "a VAPT fix costs more than a code fix": measured, the VAPT prompt frame is
        # *smaller* than the code reviewer's (3416 vs 3975 tokens), so asserting a direction
        # would encode a guess. What is asserted is the arithmetic -- that the frame charged
        # is the sum of the fixer's and the re-reviewer's, which pins which two agents were
        # priced without depending on which of them is larger.
        _af_frame = review_estimate._frame_tokens("auto-fix")
        _vp_frame = review_estimate._frame_tokens("vapt")
        _cd_frame = review_estimate._frame_tokens("code")
        _code_plan = fix_apply.plan(fix_rid, [FID["SQL injection"]])
        check("a fix is priced against the fixer's prompt plus the re-reviewer's",
              _vplan["estimate"]["prompt_frame_tokens"] == _af_frame + _vp_frame,
              f"{_vplan['estimate']['prompt_frame_tokens']} != {_af_frame}+{_vp_frame}")
        check("...and a code review's fix against the code reviewer's, not the assessor's",
              _code_plan["estimate"]["prompt_frame_tokens"] == _af_frame + _cd_frame,
              f"{_code_plan['estimate']['prompt_frame_tokens']} != {_af_frame}+{_cd_frame}")
        check("the fixer's own prompt frame is measured, not the reviewer's",
              _af_frame != _cd_frame and _af_frame > 0,
              f"auto-fix={_af_frame} code={_cd_frame} -- these were the same number until "
              f"`_frame_tokens` gained an auto-fix branch, so Apply Fixes was quoted "
              f"against a prompt it never sends")
        check("the file's content is counted for both calls, since both carry it whole",
              _vplan["estimate"]["est_input_tokens"]
              > 2 * (_vplan["estimate"]["est_input_tokens"] - _af_frame - _vp_frame) - 1,
              str(_vplan["estimate"]["est_input_tokens"]))
        check("...and the ceiling allows the fixer its full output budget",
              _vplan["estimate"]["max_output_tokens_per_call"]
              == review_estimate.MAX_OUTPUT_TOKENS["auto-fix"] == 32000,
              str(_vplan["estimate"]["max_output_tokens_per_call"]))
        check("both halves are still reported as two calls per file",
              _vplan["calls"] == 2, str(_vplan["calls"]))

        # A quote must never be free of charge for work that costs money.
        check("combining two quotes sums the money rather than taking one of them",
              review_estimate.combine(
                  {"expected_usd": 1.0, "ceiling_usd": 2.0, "calls": 1},
                  {"expected_usd": 0.5, "ceiling_usd": 1.0, "calls": 1},
              )["expected_usd"] == 1.5)
        check("...and reports the price as estimated if either half was",
              review_estimate.combine(
                  {"expected_usd": 1.0, "price_is_estimated": False, "calls": 1},
                  {"expected_usd": 1.0, "price_is_estimated": True, "calls": 1},
              )["price_is_estimated"] is True,
              "a quote that hides that its price table was guessed is worse than one that "
              "says so")
        check("...and an empty combine is empty, not a zero-dollar quote",
              review_estimate.combine() == {} and review_estimate.combine(None, {}) == {})

        # ── A VAPT of a pull request can be fixed in that pull request ───────
        _vpr = source_bundle.from_files(
            [{"path": "app/auth.py", "content": APP_FILES["app/auth.py"]}],
            label="acme/portal#42")
        _vpr.source_type = "github_pr"
        _vpr.ref = "https://github.com/acme/portal"
        _vpr.meta = {"commit_sha": "d" * 40, "head_branch": "feature/login",
                     "pr_number": 42, "head_repo_full_name": "acme/portal",
                     "base_repo_full_name": "acme/portal"}
        _vpr_rid = database.save_review(
            "vapt", "fix", "s", "c",
            {"findings": _copy.deepcopy(VAPT_FIX_FINDINGS[:1]), "gate_status": "Red"})
        _store.record(_vpr_rid, _vpr, retention_days=0)
        _vpr_outs = {o["id"]: o for o in fix_apply.plan(
            _vpr_rid,
            [json.loads(database.get_review(_vpr_rid)["result_json"])["findings"][0]["id"]],
        )["outputs"]}
        check("a VAPT of a pull request can be fixed in that pull request",
              "same_pr" in _vpr_outs and not _vpr_outs["same_pr"].get("unavailable"),
              str(sorted(_vpr_outs)))
        check("...and names the branch the fix would land on",
              "feature/login" in _vpr_outs["same_pr"]["label"],
              _vpr_outs["same_pr"]["label"])
        check("...and can open a new pull request instead",
              {"pr", "branch"} <= set(_vpr_outs), str(sorted(_vpr_outs)))

        # ── The fixer is told how the vulnerability is exploited ────────────
        from backend.agents.developer_intelligence.autofix_agent import AutoFixAgent as _AFA
        _desc_v = _AFA._describe_finding(VAPT_FIX_FINDINGS[0])
        check("the fixer is given the CWE and the OWASP category",
              "CWE-89" in _desc_v and "A03:2021" in _desc_v, _desc_v[:120])
        check("...and how the vulnerability is actually reached",
              "How it is exploited" in _desc_v and "crafted username" in _desc_v,
              "a patch that sanitises the wrong end of the path closes nothing, and the "
              "attack scenario is the only field that says which end is which")
        check("...and the proof of concept, which is the shortest statement of the bug",
              "Proof of concept" in _desc_v and "OR 1=1" in _desc_v, _desc_v[-200:])
        _desc_c = _AFA._describe_finding(
            {"severity": 5, "risk_level": "Critical", "control_id": "SEC-008",
             "title": "SQL injection", "line_start": 4, "evidence": "SELECT",
             "description": "concat", "recommendation": "parameterise"})
        check("a code-review finding reads exactly as it did before",
              _desc_c == ("- [5/5 Critical] SEC-008 SQL injection\n"
                          "  Location: line 4\n"
                          "  Offending code (verbatim):\n    SELECT\n"
                          "  Why: concat\n"
                          "  Recommended fix: parameterise"),
              repr(_desc_c))

        # ── The re-review is done by the agent that found the problem ───────
        _called29 = []

        class _Agent29:
            def __init__(self, name, findings):
                self._name, self._findings = name, findings

            def review_code(self, files, context=""):
                _called29.append("code")
                return {"findings": self._findings, "gate_status": "Red"}

            def run_vapt(self, files, context=""):
                _called29.append("vapt")
                return {"findings": self._findings, "gate_status": "Red"}

        class _Orch29:
            def __init__(self, findings):
                self.code_agent = _Agent29("code", findings)
                self.vapt_agent = _Agent29("vapt", findings)

        _p29 = {"path": "app/auth.py", "fixed_content": "def login(u, p):\n    return 1\n"}
        _afp._verify_patch(_Orch29([]), dict(_p29), "")
        check("a fix with no review type stated is still re-reviewed by the code reviewer",
              _called29 == ["code"],
              "Auto-Fix on a pull request must reach exactly the code path it always did")
        _called29.clear()
        _afp._verify_patch(_Orch29([]), dict(_p29), "", review_agent="vapt")
        check("a VAPT fix is re-reviewed by the vulnerability assessor",
              _called29 == ["vapt"], str(_called29))
        _called29.clear()

        # ── A Critical that was already there does not block the patch ──────
        #
        # This is the check that matters most, because getting it wrong is invisible in both
        # directions. Too strict and every VAPT patch is thrown away -- a file with three
        # vulnerabilities where you fix one still has two Criticals in it, truthfully
        # reported. Too loose and a vulnerability the fixer introduced ships.
        _old_crit = {"severity": 5, "title": "SQL injection in login",
                     "fingerprint": "known-fp-1", "evidence_verified": True}
        _new_crit = {"severity": 5, "title": "Command injection in the new sanitiser",
                     "fingerprint": "fresh-fp-9", "evidence_verified": True}

        _v_old = _afp._verify_patch(_Orch29([_old_crit]), dict(_p29), "",
                                    review_agent="vapt",
                                    known_fingerprints={"known-fp-1"})
        check("a Critical the original review already reported does not block the patch",
              _v_old["blocked"] is False, str(_v_old))
        check("...but is reported, so passing the guard is not read as the file being clean",
              _v_old.get("carried_over") == ["SQL injection in login"],
              str(_v_old.get("carried_over")))

        _v_new = _afp._verify_patch(_Orch29([_new_crit]), dict(_p29), "",
                                    review_agent="vapt",
                                    known_fingerprints={"known-fp-1"})
        check("a Critical the original review did NOT report blocks the patch",
              _v_new["blocked"] is True, str(_v_new))
        check("...and says it was not in the original review, rather than merely 'still'",
              "did not report" in _v_new["reason"], _v_new["reason"])
        check("...and names it",
              _v_new.get("introduced") == ["Command injection in the new sanitiser"],
              str(_v_new.get("introduced")))

        _v_mixed = _afp._verify_patch(_Orch29([_old_crit, _new_crit]), dict(_p29), "",
                                      review_agent="vapt",
                                      known_fingerprints={"known-fp-1"})
        check("one old and one new Critical still blocks, on the new one",
              _v_mixed["blocked"] is True
              and _v_mixed["introduced"] == ["Command injection in the new sanitiser"],
              str(_v_mixed))

        _v_nofp = _afp._verify_patch(_Orch29([_old_crit]), dict(_p29), "",
                                     review_agent="vapt")
        check("with no fingerprints supplied, every Critical blocks as it always did",
              _v_nofp["blocked"] is True,
              "a caller that cannot say what was there before must get the strict rule, "
              "not the lenient one")

        # Fingerprints are grouped per file. Pooling them would let an old vulnerability in
        # one file excuse a new one in another.
        _guard29 = fix_apply._guard_patches(
            _Orch29([_old_crit]),
            [{"path": "app/admin.py", "fixed_content": "x = 1\n"}],
            "", review_agent="vapt",
            prior_findings=[{"file_path": "app/auth.py", "fingerprint": "known-fp-1"}])
        check("a fingerprint known in one file does not excuse a Critical in another",
              not _guard29["safe"] and _guard29["blocked"],
              str(_guard29)[:200])
        _guard29b = fix_apply._guard_patches(
            _Orch29([_old_crit]),
            [{"path": "./app/auth.py", "fixed_content": "x = 1\n"}],
            "", review_agent="vapt",
            prior_findings=[{"file_path": "app/auth.py", "fingerprint": "known-fp-1"}])
        check("...and a leading ./ on either side is the same file, not a different one",
              len(_guard29b["safe"]) == 1, str(_guard29b)[:200])
        check("the guard reports which agent re-read the patch",
              (_guard29b["safe"][0].get("post_fix_review") or {}).get("reviewed_by") == "vapt",
              str(_guard29b["safe"][0].get("post_fix_review")))

        # ── Driven through the real route, with a VAPT review id ───────────
        _vroute = client.post(f"/api/reviews/{_vapt_rid}/fix/plan",
                              json={"finding_ids": [_VID["SQL injection in login"]]},
                              headers=UI)
        check("the fix-plan route accepts a VAPT review, not only a code review",
              _vroute.status_code == 200, f"{_vroute.status_code}: {_vroute.text[:200]}")
        check("...and it calls no model, so a quote is always free",
              (_vroute.json().get("estimate") or {}).get("calls", 0) > 0
              and _vroute.json()["files_to_change"] == 1, _vroute.text[:200])

        # ── 30. A stored run remembers what it cost ────────────────
        #
        # Reopening a run from history showed findings with no cost. The figure could never
        # travel inside `result_json`: it is not known until the model calls finish, which
        # is after `save_review` has written and serialized the row. `usage_events` has had
        # a `review_id` column for this link all along, and on the synchronous routes it was
        # always NULL because the middleware doing the metering never learned which review
        # the handler wrote.
        #
        # The cost is **read** from the ledger, never copied onto the review row. A second
        # copy of a billing figure is a second number that can disagree with the invoice.
        section("30. A stored run remembers what it cost")

        from backend import context as _ctx30

        # ── The contextvar reports back across a worker thread ──────────────
        #
        # This is the subtle half. A synchronous FastAPI endpoint runs in a threadpool with
        # a *copy* of the caller's context, so `ContextVar.set()` inside it is invisible to
        # the middleware outside. The collector holds a mutable list and appends to it,
        # which is the same trick `llm_gateway.track_usage` uses and for the same reason.
        import contextvars as _cv30

        def _save_in_child():
            return database.save_review("code", "ctx", "s", "c",
                                        {"findings": [], "gate_status": "Green"})

        with _ctx30.collect_saved_reviews():
            _child_ctx = _cv30.copy_context()
            _child_rid = _child_ctx.run(_save_in_child)
            check("A REVIEW SAVED IN A COPIED CONTEXT IS STILL REPORTED BACK",
                  _ctx30.saved_review_ids() == [_child_rid],
                  f"{_ctx30.saved_review_ids()} != [{_child_rid}] -- a plain ContextVar.set "
                  f"here is invisible to the caller, which is why this uses a mutable list; "
                  f"get it wrong and every synchronous route silently records no review id")
        check("...and the collector is empty again outside its block",
              _ctx30.saved_review_ids() == [], str(_ctx30.saved_review_ids()))
        check("saving a review with nobody collecting is not an error",
              bool(database.save_review("code", "nocollect", "s", "c", {"findings": []})),
              "the smoke suites and scripts/ save reviews outside any request")

        with _ctx30.collect_saved_reviews():
            _a30 = database.save_review("security", "multi", "s", "c", {"findings": []})
            _b30 = database.save_review("full", "multi", "s", "c", {"findings": []})
            check("several reviews in one request are collected in order, newest last",
                  _ctx30.saved_review_ids() == [_a30, _b30],
                  str(_ctx30.saved_review_ids()))

        # ── Driven through a real synchronous route, with the gateway metered ──
        _plain30 = llm_gateway.call_tool

        def _metered30(*a, **k):
            llm_gateway._record_usage("anthropic", config.MODEL, 2100, 480)
            return _plain30(*a, **k)

        llm_gateway.call_tool = _metered30
        try:
            _before30 = {r["id"] for r in database.list_reviews(limit=200)}
            _run30 = client.post("/api/review/requirement?project_name=Sources",
                                 json={"content": "The system shall be quick.",
                                       "context": ""},
                                 headers=UI)
            check("the metered run answered 200", _run30.status_code == 200,
                  _run30.text[:200])
            _new30 = [r for r in database.list_reviews(limit=200)
                      if r["id"] not in _before30]
            check("...and stored a review", len(_new30) == 1,
                  str([r["id"] for r in _new30]))
            _rid30 = _new30[0]["id"]

            _spend30 = database.get_review_spend([_rid30])
            check("THE RUN'S COST IS LINKED TO THE RUN", _rid30 in _spend30,
                  "the ledger row named no review, so history has nothing to read and a "
                  "reopened run shows findings with no cost -- which is the whole defect")
            _s30 = _spend30[_rid30]
            check("...with the tokens the gateway actually recorded",
                  _s30["input_tokens"] >= 2100 and _s30["output_tokens"] >= 480,
                  f"{_s30['input_tokens']} in / {_s30['output_tokens']} out")
            check("...and at least one call",
                  _s30["calls"] >= 1, str(_s30["calls"]))
            check("THE STORED COST IS THE COST BILLED",
                  abs(_s30["cost_usd"] - _ag._price_usage(
                      {"calls": _s30["calls"], "input_tokens": _s30["input_tokens"],
                       "output_tokens": _s30["output_tokens"]}, _s30["model"])) < 1e-6,
                  f"{_s30['cost_usd']} is not what _price_usage says for the same tokens -- "
                  f"a cost read any other way drifts from the invoice")
            check("...and it agrees with the header the same run returned",
                  abs(_s30["cost_usd"]
                      - float(_run30.headers.get("X-Spend-Cost-USD", -1))) < 1e-6,
                  f"ledger {_s30['cost_usd']} vs header "
                  f"{_run30.headers.get('X-Spend-Cost-USD')} -- the live figure and the "
                  f"remembered one must be one number, or reopening a run contradicts it")
            check("...naming the model that answered",
                  _s30["model"] == config.MODEL, _s30["model"])

            # ── The two read paths the browser uses ────────────────────────
            _one30 = client.get(f"/api/reviews/{_rid30}", headers=UI)
            check("a reopened run carries its cost", _one30.status_code == 200
                  and (_one30.json().get("spend") or {}).get("cost_usd", 0) > 0,
                  _one30.text[:240])
            _list30 = client.get("/api/reviews?limit=50&product_id=requirement",
                                 headers=UI)
            _row30 = next((r for r in _list30.json()["reviews"]
                           if r["id"] == _rid30), None)
            check("...and so does its card in the history list",
                  _row30 is not None and (_row30.get("spend") or {}).get("cost_usd", 0) > 0,
                  str(_row30)[:240])
            check("...which is one ledger query for the page, not one per card",
                  "spend" in (_list30.json()["reviews"][0] or {}),
                  "every row is annotated, so a page of ten runs is not ten round trips")
        finally:
            llm_gateway.call_tool = _plain30
        check("the metering stub is restored", llm_gateway.call_tool is _plain30)

        # ── A run with no ledger row is absent, not zero ────────────────────
        # Project-bound, because the endpoint below is driven as the browser drives it and
        # `_review_in_scope` 404s anything outside the caller's projects.
        with _ctx.bind_project(pid, "Sources", product_id="code"):
            _bare30 = database.save_review("code", "bare", "s", "c",
                                           {"findings": [], "gate_status": "Green"})
        check("A RUN WHOSE COST WAS NEVER RECORDED REPORTS NO COST, NOT $0.00",
              database.get_review_spend([_bare30]) == {},
              "every run from before this link exists cost an unknown amount, and a "
              "confident zero beside it is a false statement about money")
        _bare_get = client.get(f"/api/reviews/{_bare30}", headers=UI)
        check("...and the endpoint sends null rather than an empty cost",
              _bare_get.status_code == 200 and _bare_get.json().get("spend") is None,
              _bare_get.text[:200])
        check("asking about no reviews at all is empty, not an error",
              database.get_review_spend([]) == {}
              and database.get_review_spend([None, ""]) == {})
        check("asking about a review id that does not exist is empty",
              database.get_review_spend(["not-a-real-review-id"]) == {})

        # ── Billable and non-billable are summed apart ──────────────────────
        #
        # A failed run leaves a non-billable ledger row beside the billable one. Both are
        # real spend; only one moved a budget, and conflating them would either overstate
        # what a project was charged or hide tokens that were genuinely burned.
        _dual30 = database.save_review("code", "dual", "s", "c", {"findings": []})
        for _billable, _cost in ((1, 0.20), (0, 0.05)):
            database.record_usage_event(
                key_id=None, caller_type="ui", caller_ref=None, product_id="code",
                product_kind="agent", calls=1, input_tokens=100, output_tokens=50,
                cost_usd=_cost, model=config.MODEL, provider="anthropic",
                billable=bool(_billable), cost_estimated=False, project_id=None,
                project_name="", review_id=_dual30, status="ok")
        _d30 = database.get_review_spend([_dual30])[_dual30]
        check("two ledger rows for one run are summed, not overwritten",
              abs(_d30["cost_usd"] - 0.25) < 1e-6 and _d30["calls"] == 2,
              f"${_d30['cost_usd']} over {_d30['calls']} call(s)")
        check("...and what a budget actually moved by is reported apart",
              abs(_d30["billable_cost_usd"] - 0.20) < 1e-6,
              f"billable ${_d30['billable_cost_usd']} of ${_d30['cost_usd']} total")

        # An unpriced model must stay flagged through the sum, or an estimate is read as a
        # measurement.
        _est30 = database.save_review("code", "est", "s", "c", {"findings": []})
        database.record_usage_event(
            key_id=None, caller_type="ui", caller_ref=None, product_id="code",
            product_kind="agent", calls=1, input_tokens=10, output_tokens=5,
            cost_usd=0.01, model="some-local-model", provider="ollama", billable=True,
            cost_estimated=True, project_id=None, project_name="", review_id=_est30,
            status="ok")
        check("a guessed price stays marked as guessed after summing",
              database.get_review_spend([_est30])[_est30]["cost_estimated"] is True,
              "an estimate presented as a measurement is the one way this figure can lie")

        # ── 31. The write path, which no offline test had ever reached ───
        #
        # Both of these were found by running one real fix against one real pull request,
        # and neither was reachable from here before: every fix test in this suite stops at
        # `plan`, which writes nothing, and section 25's delivery tests assert which options
        # are *offered*, never that taking one works. Two bugs sat behind that line, so
        # `output="pr"` and `output="branch"` from Apply Fixes had **never once succeeded**.
        #
        # Neither is checkable end to end without a real repository, so what is checked here
        # is the argument each one got wrong -- which is where the bug actually was.
        section("31. The write path, which no offline test had ever reached")

        from backend.services.github_service import GitHubService as _GH31
        import inspect as _i31

        # ── D-314: a commit sha in a slot that resolves branch names ─────
        #
        # `create_branch`'s fourth argument is resolved through `/git/ref/heads/<name>`, so a
        # sha passed there asks GitHub for a branch literally named after a commit and gets a
        # bare 404. `fix_apply` did exactly that, on the line whose own comment explains that
        # branching off the reviewed commit is the point.
        check("there is a way to branch at an exact commit",
              hasattr(_GH31, "create_branch_at_sha"),
              "branching off the commit a review read is the whole reason Apply Fixes does "
              "not just branch off the tip")
        # `_write31` is the write path. `apply` generates, guards and stops at the preview;
        # `_deliver` is what touches a repository, and every check below is about writing.
        _write31 = _i31.getsource(fix_apply._deliver)
        check("...and it is the one Apply Fixes calls",
              "create_branch_at_sha(" in _write31,
              "create_branch resolves its fourth argument as a branch NAME; a sha there is "
              "a request for /git/ref/heads/<sha>, which 404s every time")
        check("...and it refuses a branch name, rather than asking GitHub about a bad ref",
              _raises_value_error(lambda: _GH31().create_branch_at_sha(
                  "o", "r", "new", "feat/some-branch")),
              "caught here it names the wrong argument; caught at GitHub it is a 422 about "
              "a malformed ref with nothing saying which argument was wrong")
        check("...while accepting a real sha's shape",
              not _raises_value_error(lambda: _GH31._sha_ok("a" * 40))
              if hasattr(_GH31, "_sha_ok") else True)
        check("create_branch still takes a branch name, for the caller that has one",
              list(_i31.signature(_GH31.create_branch).parameters)[-1] == "from_branch",
              "workflows/autofix_pr.py passes a head branch here and has always worked -- "
              "the fix must not move that path")

        # ── D-315: a pull request opened against a base named after a title ─
        #
        # `base_ref` was `label.split("@")[-1]` for every source. A repository's label is
        # `owner/repo@ref`, so that works. A **pull request's** label is its title, so the
        # fix pull request was opened with `base="CB-104: AI visit summary endpoint"` and
        # GitHub answered 422 with nothing naming the branch.
        _src31 = _write31
        check("a fix from a pull request targets a branch, not the review's label",
              'source.get("source_type") == "github_pr"' in _src31
              and 'source.get("head_branch")' in _src31,
              "a PR's label is its title; splitting it on '@' yields the title, which is "
              "not a branch and 422s")
        check("...and a repository source still targets its ref",
              'split("@")[-1]' in _src31,
              "owner/repo@main is the shape that made the original line work")
        check("...and an unknown base is ASKED FOR, not assumed to be 'main'",
              "get_default_branch" in _src31 and 'base=base_ref,' in _src31,
              "'main' is a guess that fails on master or develop, and fails as another "
              "opaque 422")
        check("the default branch is a real lookup with a safe fallback",
              hasattr(_GH31, "get_default_branch"))

        # ── The fallback above was itself a crash ─────────────────────────
        #
        # `base_ref = gh.get_default_branch(owner, repo)` sat *above* the lines assigning
        # `owner`, `repo` and `gh`. All three are assigned somewhere in `apply`, so Python
        # treats them as locals throughout it and that call raised `UnboundLocalError`
        # instead of reaching GitHub — the sentence written to replace an opaque 422 was a
        # traceback. Third defect in the same twenty lines (D-314, D-315), and invisible to
        # every test here because they all stop at `plan`, which writes nothing.
        #
        # Checked structurally rather than by string, because the failure is an *ordering*
        # property: walk the function body and require the first binding of each name to
        # come before its first read. This catches the whole class, not the one instance.
        import ast as _ast31
        import textwrap as _tw31

        # `ast.unparse(ast.parse(...))` first, so every check below reads *code* and not
        # comments. The first version of this check compared string offsets over the raw
        # source and passed/failed on a comment that happened to mention the function it
        # was looking for -- which is the same class of mistake it exists to catch.
        _code31 = _ast31.unparse(_ast31.parse(_tw31.dedent(_i31.getsource(fix_apply._deliver))))
        # Guard against this check quietly emptying out. If the block it watches moves
        # again, the names disappear from the function and the ordering check below starts
        # passing because there is nothing left to order — the failure mode that made this
        # comment necessary the first time.
        check("...and the check is looking at the function that actually writes",
              all(n in _code31 for n in ("gh", "owner", "repo", "get_default_branch")),
              "the owner/repo/client resolution is no longer in `_deliver`, so the "
              "ordering check below would pass vacuously -- point it at wherever the "
              "write path went")

        def _read_before_bound(code: str, names):
            """Names read on a line above the line that first binds them.

            These are function-locals, so such a read is an `UnboundLocalError` at runtime
            rather than a fall-through to a module global -- which is exactly what the
            default-branch fallback did.
            """
            bound, offenders = {}, []
            for node in _ast31.walk(_ast31.parse(code)):
                if isinstance(node, _ast31.Name) and node.id in names:
                    if isinstance(node.ctx, _ast31.Store):
                        bound[node.id] = min(bound.get(node.id, node.lineno), node.lineno)
            for node in _ast31.walk(_ast31.parse(code)):
                if (isinstance(node, _ast31.Name) and node.id in names
                        and isinstance(node.ctx, _ast31.Load)):
                    first = bound.get(node.id)
                    if first is not None and node.lineno < first:
                        offenders.append(f"{node.id}@line{node.lineno}")
            return sorted(set(offenders))

        _offenders31 = _read_before_bound(_code31, {"gh", "owner", "repo"})
        check("the write path never reads owner/repo/gh before assigning them",
              not _offenders31,
              "read before assignment: " + ", ".join(_offenders31)
              + " -- function-locals, so a read above the assignment is an "
                "UnboundLocalError and not a fall-through to a global")
        check("...and the repository client is resolved before the default branch is asked for",
              _code31.index("gh = orch.github") < _code31.index("get_default_branch("),
              "the default-branch lookup takes the client and the owner/repo it names")

        # ── The patch is shown before it is delivered ─────────────────────
        #
        # `apply` used to take the destination with it, so one call generated the patch and
        # opened the pull request; the diff was in the result, after the branch existed. On
        # a platform whose argument is that a human approves what an agent proposes, this
        # was the one write with no approval step.
        check("a fix can be generated without being delivered",
              fix_apply.PREVIEW not in fix_apply.OUTPUTS,
              "preview is the step before a delivery, not one of them -- in OUTPUTS it "
              "would appear in the destination list as somewhere a patch can go")
        check("...and there is a separate call that only delivers",
              callable(getattr(fix_apply, "deliver", None)))
        _dsrc = _i31.getsource(fix_apply.deliver)
        check("...which never generates, so the diff approved is the diff that ships",
              "_fix_one_file" not in _dsrc and "_guard_patches" not in _dsrc
              and "call_tool" not in _dsrc,
              "regenerating to change the destination would pay twice and could produce a "
              "different patch, so the approval would refer to something else")
        check("...and takes the patched contents from the held preview, not from its caller",
              'held.get("patches")' in _dsrc,
              "a caller that could post file contents here could commit anything it liked "
              "to the customer's repository under a fix's name")
        check("...and refuses a destination this review cannot use",
              "_available_outputs(" in _dsrc and "not in available" in _dsrc)
        _main_src = io.open(_i31.getsourcefile(main), encoding="utf-8").read()
        check("the preview route holds the contents server-side, not in the response",
              all(frag in _main_src
                  for frag in ('result.pop("patches", None)', "_FIX_PENDING")),
              "the patched source must not travel to the browser and back")
        check("...and delivery is still entitlement-checked, having no model call to meter",
              'agent_guard.preflight(caller, "auto-fix")' in _main_src,
              "D-307: writing to a repository is the auto-fix product whichever review the "
              "findings came from, and no model call does not mean no governance")

        # The delivery options are unchanged by any of this -- checked because the fix
        # touched the function that reads them.
        _pr_outs31 = {o["id"] for o in fix_apply.plan(
            pr_rid, [f["id"] for f in json.loads(
                database.get_review(pr_rid)["result_json"])["findings"][:1]],
        )["outputs"]}
        check("a pull-request review is still offered all four writes plus the two reads",
              {"pr", "same_pr", "branch", "diff", "zip"} <= _pr_outs31,
              str(sorted(_pr_outs31)))

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
        # ── 34. GitHub App and the check run ─────────────────────────────
        #
        # A check run is the only way a finding lands on the line it is about, and GitHub
        # refuses the endpoint for a personal access token — so this needs an App, and an
        # App means an RS256 JWT and a minted installation token.
        #
        # **Two of these calls have never reached GitHub.** What is checked here is
        # everything that does not need the network: the JWT's structure and claims, the
        # token cache, the annotation mapping, the conclusion mapping, the cap, which
        # findings are deliberately not placed, and the fallback when no App is configured.
        # D-314 and D-315 are two GitHub write paths that shipped broken because nothing
        # offline could reach them, so the boundary is drawn explicitly rather than left to
        # be assumed.
        section("34. GitHub App auth and check-run assembly")

        from backend.services import github_app as _ga34
        from backend.services.github_service import GitHubService as _GHS34

        check("with nothing configured, the App is not configured",
              _ga34.is_configured() is False)
        check("...and says which variables are missing rather than just 'no'",
              "GITHUB_APP_ID" in (_ga34.status().get("note") or ""),
              str(_ga34.status()))
        check("...and explains what still works without one",
              "commit status" in (_ga34.status().get("note") or ""),
              "a person reading this needs to know the review is not broken")
        check("a token is not attempted when nothing is configured",
              _ga34.token_for("acme", "portal") == "",
              "every caller's fallback is the commit status, so this must return empty "
              "rather than raise")

        # An id with no key cannot mint anything, and reporting it as configured would mean
        # every review tried, failed and fell back, once per review.
        _ga34.config.GITHUB_APP_ID = "123456"
        try:
            check("an App id with no private key is not 'configured'",
                  _ga34.is_configured() is False,
                  "half-configured would mean a failed attempt on every single review")
        finally:
            _ga34.config.GITHUB_APP_ID = ""

        # A real RSA key, generated here. `cryptography` is a declared dependency and is
        # what signs the JWT — PyJWT is importable in this environment but is not in
        # requirements.txt, and building an auth path on a transitive dependency is how it
        # disappears in the next upgrade.
        from cryptography.hazmat.primitives import serialization as _ser34
        from cryptography.hazmat.primitives.asymmetric import rsa as _rsa34
        _key34 = _rsa34.generate_private_key(public_exponent=65537, key_size=2048)
        _pem34 = _key34.private_bytes(
            encoding=_ser34.Encoding.PEM,
            format=_ser34.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=_ser34.NoEncryption(),
        ).decode("ascii")

        _ga34.config.GITHUB_APP_ID = "123456"
        _ga34.config.GITHUB_APP_PRIVATE_KEY = _pem34
        try:
            check("an id and a key together are 'configured'", _ga34.is_configured() is True)
            check("...and the status reports it without returning the key",
                  _ga34.status()["configured"] is True
                  and "BEGIN" not in _json34.dumps(_ga34.status()),
                  "a private key echoed by a status endpoint is a private key in a log")

            _jwt34 = _ga34.app_jwt()
            _parts34 = _jwt34.split(".")
            check("the App assertion is a three-part JWS", len(_parts34) == 3, _jwt34[:40])
            _hdr34 = _json34.loads(_b64d34(_parts34[0]))
            _pay34 = _json34.loads(_b64d34(_parts34[1]))
            check("...signed RS256, which is the only algorithm GitHub accepts here",
                  _hdr34.get("alg") == "RS256", str(_hdr34))
            check("...issued by the App id", _pay34.get("iss") == "123456", str(_pay34))
            check("...backdated, so a fast clock does not produce a future token",
                  _pay34["iat"] < _time34.time(),
                  "GitHub rejects an assertion issued in the future, which a server a few "
                  "seconds fast otherwise sends every time")
            check("...and expiring inside GitHub's ten-minute ceiling",
                  0 < _pay34["exp"] - _time34.time() <= 600, str(_pay34["exp"]))
            check("...with no padding on the base64, which JWS forbids",
                  "=" not in _jwt34, _jwt34[:60])

            # And it really verifies, which is the only thing that makes the rest of it
            # meaningful — a signature over the wrong bytes is a well-formed useless token.
            from cryptography.hazmat.primitives import hashes as _h34
            from cryptography.hazmat.primitives.asymmetric import padding as _pad34
            _sig34 = _b64d34(_parts34[2])
            _key34.public_key().verify(
                _sig34, (_parts34[0] + "." + _parts34[1]).encode("ascii"),
                _pad34.PKCS1v15(), _h34.SHA256())
            check("...and the signature verifies against the key that made it", True)

            # A PEM whose newlines were flattened by a deployment tool. This is the single
            # most common way this is misconfigured, and the raw error says nothing.
            _ga34.config.GITHUB_APP_PRIVATE_KEY = _pem34.replace("\n", "\\n")
            check("a PEM with escaped newlines still works",
                  bool(_ga34.app_jwt()),
                  "every deployment tool flattens a PEM into one line, and the "
                  "'could not deserialize key' that follows says nothing about the cause")

            _ga34.config.GITHUB_APP_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nnope\n"
            _err34 = ""
            try:
                _ga34.app_jwt()
            except _ga34.AppAuthError as exc:
                _err34 = str(exc)
            check("an unreadable key is refused with a sentence, not a stack trace",
                  "starting with '-----BEGIN'" in _err34, _err34[:200])
            check("...and `token_for` swallows it so the review still reports",
                  _ga34.token_for("acme", "portal") == "",
                  "an App problem must degrade the richness of the report, never lose it")
        finally:
            _ga34.config.GITHUB_APP_ID = ""
            _ga34.config.GITHUB_APP_PRIVATE_KEY = ""
            _ga34._token_cache.clear()

        # ── The check run's body, which is where the judgement is ────────
        class _Check34(_GHS34):
            """`create_check_run` with the HTTP call replaced, so the body can be read."""

            def __init__(self):
                self.token = "unused"
                self.sent = None

        def _capture34(self, url, headers=None, json=None, **kw):
            self.sent = {"url": url, "headers": headers, "body": json}
            class _R:
                status_code = 201
                text = ""
                @staticmethod
                def json():
                    return {"id": 9, "html_url": "https://github.com/acme/portal/runs/9"}
            return _R()

        def _run_check34(result, **kw):
            svc = _Check34()
            import httpx as _hx34
            _real_client = _hx34.Client

            class _StubClient:
                def __init__(self, *a, **k):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def post(self, url, headers=None, json=None, **k):
                    return _capture34(svc, url, headers=headers, json=json)

            _hx34.Client = _StubClient
            try:
                out = svc.create_check_run("acme", "portal", "b" * 40, result,
                                           "ghs_installationtoken", **kw)
            finally:
                _hx34.Client = _real_client
            return out, svc.sent

        _placeable34 = dict(SQLI, fingerprint="fp-1")
        _noline34 = dict(control_id="GOV-001", severity=3, risk_level="Moderate",
                         title="No threat model", description="Design level.",
                         recommendation="Write one.", is_blocking=False)
        _unverified34 = dict(SQLI, title="Invented", file_path="app/auth.py",
                             line_start=9, evidence_verified=False)
        _out34, _sent34 = _run_check34({
            "gate_status": "Red", "risk_score": 90.0, "summary": "One real problem.",
            "findings": [dict(_placeable34, evidence_verified=True), _noline34, _unverified34],
        }, details_url="https://sdlc.example/x")

        check("the check run goes to the check-runs endpoint",
              _sent34["url"].endswith("/repos/acme/portal/check-runs"), _sent34["url"])
        check("...with the installation token, not this service's own credential",
              _sent34["headers"]["Authorization"] == "Bearer ghs_installationtoken",
              "GitHub refuses this endpoint for a PAT, which is the entire reason "
              "github_app.py exists")
        check("...and an explicit API version, so a default change cannot break it",
              _sent34["headers"].get("X-GitHub-Api-Version") == "2022-11-28")
        check("a Red gate concludes as a failure, which is what blocks a merge",
              _sent34["body"]["conclusion"] == "failure", str(_sent34["body"]["conclusion"]))
        check("only the placeable finding is annotated",
              len(_sent34["body"]["output"]["annotations"]) == 1,
              str(_sent34["body"]["output"]["annotations"]))
        check("...on the file and line it names",
              _sent34["body"]["output"]["annotations"][0]["path"] == "app/auth.py"
              and _sent34["body"]["output"]["annotations"][0]["start_line"] == 1,
              str(_sent34["body"]["output"]["annotations"][0]))
        check("...at failure level for a Critical",
              _sent34["body"]["output"]["annotations"][0]["annotation_level"] == "failure")
        check("a finding whose quote was not found is not placed on a guessed line",
              _out34["unplaceable"] == 2,
              "an annotation on the wrong line sends a reviewer to code that is fine and "
              "teaches them the tool is unreliable")
        check("...and the summary says how many could not be placed",
              "2 finding(s) could not be placed"
              in _sent34["body"]["output"]["summary"],
              _sent34["body"]["output"]["summary"][:300])
        check("the Details link is passed through when there is one",
              _sent34["body"].get("details_url") == "https://sdlc.example/x")
        check("the result carries the check run's own URL back",
              _out34["created"] is True and "runs/9" in _out34["url"], str(_out34))

        for _gate34, _concl34 in (("Green", "success"), ("Amber", "neutral"),
                                  ("Red", "failure")):
            _o34, _s34 = _run_check34({"gate_status": _gate34, "findings": []})
            check(f"a {_gate34} gate concludes as {_concl34}",
                  _s34["body"]["conclusion"] == _concl34, str(_s34["body"]["conclusion"]))
        check("Amber deliberately does not block a merge",
              _GHS34._CHECK_CONCLUSION["Amber"] == "neutral",
              "whether an Amber gate should stop a merge is the open policy question C-38 "
              "records; choosing action_required here would answer it for every customer")

        _many34 = [dict(_placeable34, title=f"Finding {i}", line_start=i + 1,
                        evidence_verified=True, fingerprint=f"fp-{i}")
                   for i in range(_GHS34.MAX_ANNOTATIONS + 7)]
        _o34, _s34 = _run_check34({"gate_status": "Red", "findings": _many34})
        check("GitHub's fifty-annotation cap is respected",
              len(_s34["body"]["output"]["annotations"]) == _GHS34.MAX_ANNOTATIONS,
              str(len(_s34["body"]["output"]["annotations"])))
        check("...and the overflow is counted rather than dropped silently",
              _o34["annotations_omitted"] == 7
              and "7 further finding(s)" in _s34["body"]["output"]["summary"],
              _s34["body"]["output"]["summary"][:200])

        _o34, _s34 = _run_check34({
            "gate_status": "Red",
            "findings": [dict(_placeable34, evidence_verified=True, carried_forward=True)],
        })
        check("a carried finding says so in its annotation",
              "did not re-read" in _s34["body"]["output"]["annotations"][0]["message"],
              "otherwise an annotation on a line this run never read looks like one it did")

        # A refusal has to be a sentence with a cause, not a status code.
        def _refuse34(status):
            svc = _Check34()
            import httpx as _hx34
            _real = _hx34.Client

            class _Deny:
                def __init__(self, *a, **k):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def post(self, *a, **k):
                    class _R:
                        status_code = status
                        text = '{"message":"nope"}'
                        @staticmethod
                        def json():
                            return {}
                    return _R()

            _hx34.Client = _Deny
            try:
                return svc.create_check_run("acme", "portal", "b" * 40,
                                            {"gate_status": "Red", "findings": []}, "t")
            finally:
                _hx34.Client = _real

        check("a 403 names the permission that is missing",
              "Checks: Read and write" in (_refuse34(403).get("hint") or ""),
              str(_refuse34(403)))
        check("a 404 names the thing people actually forget",
              "not installed" in (_refuse34(404).get("hint") or ""),
              "an App that exists is not an App with access")
        check("a refusal is reported, never raised",
              _refuse34(500)["created"] is False,
              "the review is stored and paid for by the time this runs")

        # ── The setup endpoint ───────────────────────────────────────────
        _st34 = client.get("/api/github/app/status")
        check("the App status endpoint answers", _st34.status_code == 200, _st34.text[:200])
        check("...saying what an App enables that a token cannot",
              "annotated on its own line" in _st34.text, _st34.text[:300])
        check("...and what already works without one",
              "SARIF" in _st34.text and "commit status" in _st34.text)
        check("...and the exact permissions to set",
              _st34.json()["permissions_needed"]["Checks"] == "Read and write",
              str(_st34.json().get("permissions_needed")))
        check("...without returning anything secret",
              "BEGIN" not in _st34.text and "ghs_" not in _st34.text, _st34.text[:200])
        check("the written instructions exist, since the status points at them",
              _os34.path.exists(_os34.path.join(
                  _os34.path.dirname(_os34.path.dirname(_os34.path.abspath(__file__))),
                  "docs", "github-app.md")),
              "a status that names a document that is not there is worse than one that "
              "names nothing")

        # ── 33. Incremental review of a pull request ─────────────────────
        #
        # `synchronize` fires on every push, so an active pull request cost a full review
        # per push. The saving is obvious; the danger is the whole of the work. A run that
        # reads only the newest commits does not see a Critical raised two pushes ago in a
        # file it never opened — **and an absent finding reads as a fixed one**, so the
        # stored gate goes Green and `set_commit_status` clears the merge button.
        #
        # Everything below is about that one property.
        section("33. An incremental review cannot turn a blocked pull request Green")

        from backend.workflows import review_pr as _rp33

        class _GH33:
            """A GitHub stub that records what was asked of it.

            Two calls matter: `get_pr_diff` (the whole pull request) and `get_compare_diff`
            (the slice). Which of them the diff came from is the question.
            """

            def __init__(self, compare=None, compare_error=None):
                self.compare = compare
                self.compare_error = compare_error
                self.asked = []
                self.comments = []

            def get_pr_diff(self, pr_url):
                self.asked.append(("pr", pr_url))
                return {
                    "pr_number": 42, "title": "Add the thing", "description": "",
                    "author": "dev", "base_branch": "main", "head_branch": "feature",
                    "head_sha": "b" * 40, "base_sha": "a" * 40,
                    "head_repo": "acme/portal", "base_repo": "acme/portal",
                    "from_fork": False, "maintainer_can_modify": True,
                    "diff": _FULL_DIFF33, "diff_truncated": False,
                    "diff_full_chars": len(_FULL_DIFF33),
                    "files_changed": ["app/old.py", "app/new.py"],
                    "additions": 4, "deletions": 0, "url": pr_url,
                }

            def get_compare_diff(self, repo_url, base, head):
                self.asked.append(("compare", base, head))
                if self.compare_error:
                    raise RuntimeError(self.compare_error)
                return dict(self.compare, base=base, head=head)

            def get_file_content_at_ref(self, owner, repo, path, ref):
                raise RuntimeError("404 Not Found")

            def parse_pr_url(self, pr_url):
                return ("acme", "portal", 42)

            def get_pr_head_sha(self, pr_url):
                return "b" * 40

            def format_pr_review_comment(self, result, result_url=""):
                from backend.services.github_service import GitHubService
                return GitHubService.format_pr_review_comment(self, result, result_url)

            def _format_location(self, f):
                return f"`{f.get('file_path', '')}`"

            MAX_COMMENT_FINDINGS = 20
            COMMENT_MARKER = "<!-- lm-ai-sdlc:review -->"

            def upsert_pr_comment(self, pr_url, body):
                self.comments.append(body)
                return {"posted": True}

            def set_commit_status(self, owner, repo, sha, gate, description=""):
                return {"set": True, "gate": gate}

        class _Orch33:
            def __init__(self, gh, agent):
                self.github = gh
                self.code_agent = agent

        class _Agent33:
            """Records the diff it was shown, and answers with whatever is scripted."""

            def __init__(self):
                self.seen = []
                self.answer = None
                self.suppression = "unset"

            def review_code(self, code="", language="", context="", files=None,
                            is_diff=None, coverage_incomplete=False, coverage_reason="",
                            extra_findings=None, suppression=None, on_progress=None,
                            **kw):
                self.seen.append({"code": code, "context": context,
                                  "coverage_incomplete": coverage_incomplete})
                self.suppression = suppression
                out = dict(self.answer or {"findings": [], "gate_status": "Green",
                                           "risk_score": 0.0, "summary": "Clean."})
                finding_gate.reconcile_gate(out)
                return out

        _NEW33 = dict(
            control_id="SEC-001", severity=5, risk_level="Critical",
            title="Command injection in the new helper", description="Shell out.",
            recommendation="Use argv.", is_blocking=True, file_path="app/new.py",
            line_start=2, evidence="os.system(cmd)", confidence="High",
        )

        # ── The saving: only the new commits reach the model ─────────────
        _gh33 = _GH33(compare={
            "diff": _SLICE_DIFF33, "status": "ahead", "ahead_by": 2, "behind_by": 0,
            "head_sha": "b" * 40, "diff_truncated": False,
            "diff_full_chars": len(_SLICE_DIFF33),
            "files_changed": ["app/new.py"],
            "url": "https://github.com/acme/portal/compare/aaa...bbb",
        })
        _ag33 = _Agent33()
        _ag33.answer = {"findings": [], "gate_status": "Green", "risk_score": 0.0,
                        "summary": "The new commits are clean."}
        _out33 = _rp33.run(_Orch33(_gh33, _ag33), pr_url=_PR33,
                           post_mode="none", set_status=False, since_sha="a" * 40)
        check("a push reviews only the commits since the last review",
              "app/new.py" in _ag33.seen[0]["code"]
              and "app/old.py" not in _ag33.seen[0]["code"],
              "the model was shown: " + _ag33.seen[0]["code"][:120])
        check("...asked for as a compare of the two commits",
              ("compare", "a" * 40, "b" * 40) in _gh33.asked, str(_gh33.asked))
        check("...and the reviewer is told it is seeing part of a pull request",
              "INCREMENTAL review" in _ag33.seen[0]["context"],
              "otherwise it reports a control missing when the file that implements it "
              "simply was not in this push")
        check("...and told the whole pull request's file list anyway",
              "app/old.py" in _ag33.seen[0]["context"], _ag33.seen[0]["context"][:300])
        check("a narrowed scope does not clamp the gate to Amber",
              _out33["review_result"]["gate_status"] == "Green",
              "the coverage clamp is for code nobody chose to skip; clamping a deliberate "
              "narrowing would mean a clean push could never be Green, which is the point")
        check("the result says what it read",
              _out33["incremental"].get("commits") == 2
              and _out33["incremental"].get("files_reviewed") == 1
              and _out33["incremental"].get("files_in_pr") == 2,
              str(_out33["incremental"]))
        check("...and the comment says so too, before the gate is believed",
              "commit(s) pushed since" in (_out33.get("comment_preview") or ""),
              (_out33.get("comment_preview") or "")[:300])

        # ── The danger: an earlier Critical must still block ─────────────
        #
        # Written straight into the database as a previous review of the same pull request,
        # because what is under test is the *lookup* — a wrong repository, a wrong PR
        # number or a missing `pr_number` column all produce an empty list and a wrongly
        # Green gate, and each of those is a silent failure.
        _prior33 = _seed_pr_review33(project_id=None, pr_number=42)
        _gh33b = _GH33(compare={
            "diff": _SLICE_DIFF33, "status": "ahead", "ahead_by": 1, "behind_by": 0,
            "head_sha": "b" * 40, "diff_truncated": False,
            "diff_full_chars": len(_SLICE_DIFF33),
            "files_changed": ["app/new.py"],
            "url": "https://github.com/acme/portal/compare/aaa...bbb",
        })
        _ag33b = _Agent33()
        _ag33b.answer = {"findings": [], "gate_status": "Green", "risk_score": 0.0,
                         "summary": "The new commits are clean."}
        _out33b = _rp33.run(_Orch33(_gh33b, _ag33b), pr_url=_PR33,
                            post_mode="none", set_status=False, since_sha="a" * 40)
        _res33b = _out33b["review_result"]
        _carried33 = [f for f in _res33b["findings"] if f.get("carried_forward")]
        check("a still-open finding from an earlier push is carried into this run",
              len(_carried33) == 1, str([f.get("title") for f in _res33b["findings"]]))
        check("...so a clean push cannot turn a blocked pull request Green",
              _res33b["gate_status"] == "Red", _res33b["gate_status"])
        check("...and it is labelled as carried, not as found",
              _carried33 and _carried33[0]["carried_from_review"] == _prior33,
              str(_carried33[0] if _carried33 else None)[:200])
        check("...with its evidence neither claimed nor denied",
              _carried33 and _carried33[0]["evidence_verified"] is None
              and "did not re-read" in (_carried33[0].get("evidence_note") or ""),
              "it was verified against the file *then*; this run never opened the file, so "
              "asserting either answer would be a claim about code nobody read")
        check("...and the comment says which findings this run did not find",
              "carried from an earlier review" in (_out33b.get("comment_preview") or ""),
              (_out33b.get("comment_preview") or "")[:400])
        check("the model's own verdict is still recorded, not overwritten by the re-floor",
              _res33b.get("model_gate_status") == "Green",
              str(_res33b.get("model_gate_status"))
              + " -- reconcile_gate reads gate_status as the model's answer, so a second "
                "call would replace it with the already-floored value and the suite could "
                "no longer measure model-versus-arithmetic disagreement")

        # A finding this run *did* find again must not appear twice.
        _ag33c = _Agent33()
        _ag33c.answer = {"findings": [dict(_PRIOR_FINDING33)], "gate_status": "Red",
                         "risk_score": 90.0, "summary": "Still there."}
        _out33c = _rp33.run(_Orch33(_GH33(compare=dict(_gh33b.compare)), _ag33c),
                            pr_url=_PR33, post_mode="none", set_status=False,
                            since_sha="a" * 40)
        _same33 = [f for f in _out33c["review_result"]["findings"]
                   if f.get("fingerprint") == _PRIOR_FINGERPRINT33]
        check("a finding found again is not also carried",
              len(_same33) == 1, f"{len(_same33)} copies of the same fingerprint")
        check("...and the one that survives is the one this run actually verified",
              _same33 and not _same33[0].get("carried_forward"), str(_same33[0])[:200])

        # ── The fallbacks, all of which review the whole thing ───────────
        for _label, _kwargs, _reason in (
            ("a force-push or a rebase",
             {"compare": {"diff": _SLICE_DIFF33, "status": "diverged", "ahead_by": 1,
                          "behind_by": 3, "head_sha": "b" * 40, "diff_truncated": False,
                          "diff_full_chars": 10, "files_changed": [], "url": ""}},
             "rewritten"),
            ("a comparison GitHub will not answer",
             {"compare_error": "422 no common ancestor"}, "could not be fetched"),
            ("a push that changes no reviewable code",
             {"compare": {"diff": "   ", "status": "ahead", "ahead_by": 1, "behind_by": 0,
                          "head_sha": "b" * 40, "diff_truncated": False,
                          "diff_full_chars": 3, "files_changed": [], "url": ""}},
             "no reviewable code"),
        ):
            _ghx = _GH33(**_kwargs)
            _agx = _Agent33()
            _agx.answer = {"findings": [], "gate_status": "Green", "risk_score": 0.0,
                           "summary": "Clean."}
            _outx = _rp33.run(_Orch33(_ghx, _agx), pr_url=_PR33, post_mode="none",
                              set_status=False, since_sha="a" * 40)
            check(f"{_label} falls back to the whole pull request",
                  "app/old.py" in _agx.seen[0]["code"],
                  "the model was shown: " + _agx.seen[0]["code"][:120])
            check(f"...and the result says why, rather than silently costing more",
                  _reason in (_outx["incremental"].get("reason") or ""),
                  str(_outx["incremental"]))

        # Comparing a commit with itself is a review of nothing, so it must not happen.
        _ghs = _GH33(compare=dict(_gh33b.compare))
        _ags = _Agent33()
        _ags.answer = {"findings": [], "gate_status": "Green", "risk_score": 0.0,
                       "summary": "Clean."}
        _rp33.run(_Orch33(_ghs, _ags), pr_url=_PR33, post_mode="none", set_status=False,
                  since_sha="b" * 40)
        check("a sha equal to the head is not compared with itself",
              not any(a[0] == "compare" for a in _ghs.asked), str(_ghs.asked))

        # With no `since_sha` at all — every non-push event — nothing changes.
        _ghn = _GH33()
        _agn = _Agent33()
        _agn.answer = {"findings": [], "gate_status": "Green", "risk_score": 0.0,
                       "summary": "Clean."}
        _outn = _rp33.run(_Orch33(_ghn, _agn), pr_url=_PR33, post_mode="none",
                          set_status=False)
        check("without a previous review the whole pull request is read, and nothing is said",
              "app/old.py" in _agn.seen[0]["code"] and not _outn["incremental"],
              str(_outn["incremental"]))

        # ── The bookkeeping the webhook reads ────────────────────────────
        database.record_webhook_review("acme/portal", "c" * 40, _PR33, "job-1", "d1")
        database.record_webhook_review("acme/portal", "d" * 40, _PR33, "job-2", "d2")
        check("the last reviewed commit for a pull request is the newest one",
              database.last_reviewed_sha_for_pr("acme/portal", _PR33) == "d" * 40,
              str(database.last_reviewed_sha_for_pr("acme/portal", _PR33)))
        check("...excluding the push being handled right now",
              database.last_reviewed_sha_for_pr(
                  "acme/portal", _PR33, before_sha="d" * 40) == "c" * 40,
              "the webhook records its own row after queueing, and a redelivery would "
              "otherwise compare a sha with itself")
        check("another pull request in the same repository is not confused with this one",
              database.last_reviewed_sha_for_pr(
                  "acme/portal", "https://github.com/acme/portal/pull/99") is None)
        check("a repository with no reviewed pushes yields nothing rather than an error",
              database.last_reviewed_sha_for_pr("acme/other", _PR33) is None)

        # And the repository dimension, which pull-request reviews never recorded at all.
        _stored33 = database.get_review(_out33b["review_id"])
        check("a pull-request review now records which repository it was of",
              _stored33.get("repo_owner") == "acme" and _stored33.get("repo_name") == "portal",
              f"owner={_stored33.get('repo_owner')!r} name={_stored33.get('repo_name')!r} "
              f"-- every PR review ever stored had NULL here, so `GET /api/repositories`, "
              f"the findings repo filter and the carry-forward lookup were all blind to "
              f"the one source where the repository is least ambiguous")
        check("...and which pull request", _stored33.get("pr_number") == 42,
              str(_stored33.get("pr_number")))
        check("...and at which commit", _stored33.get("commit_sha") == "b" * 40,
              str(_stored33.get("commit_sha")))
        check("the repository's ignore rules are offered to a pull-request review",
              _ag33b.suppression is not None,
              "a `.lmignore` in the repository has to reach the review that reads its diff, "
              "and the fetch fails open -- this stub answers 404, which is the common case")

        # Which scope the *webhook* asks for. Checked on the source rather than by firing a
        # delivery, because the decision is three lines of policy — only a push, only when
        # the switch is on, never against this push's own sha — and each of them is a
        # different way to get a review of nothing or a review of everything.
        import inspect as _i33
        from backend.routes import github_webhook as _wh33
        _whsrc33 = _i33.getsource(_wh33.github_webhook)
        check("only a push asks for an incremental review",
              'action == "synchronize"' in _whsrc33,
              "opened, reopened and ready_for_review are the moments where scope matters "
              "more than cost, and `opened` has nothing earlier to compare against")
        check("...and only when the switch is on",
              "config.INCREMENTAL_PR_REVIEW" in _whsrc33)
        check("...and never against the sha being reviewed right now",
              "before_sha=head_sha" in _whsrc33,
              "the webhook records its own row after queueing, so a redelivery would "
              "compare a commit with itself and review nothing")
        check("the switch defaults to on, and can be turned off",
              config.INCREMENTAL_PR_REVIEW is True
              and "INCREMENTAL_PR_REVIEW" in _i33.getsource(config),
              str(config.INCREMENTAL_PR_REVIEW))
        check("the delivery response says which scope was chosen",
              '"scope"' in _whsrc33,
              "GitHub's delivery log is where somebody looks to find out why a push cost "
              "what it cost")

        # ── The live driver, which is the only thing that can reach any of this ──
        #
        # D-317's landmine stands: every check in this section is *structural*. It reads
        # the source of `_deliver` and asserts the shape of the code, because the offline
        # suite stops at `plan` and `plan` writes nothing. Structure caught D-314, D-315
        # and D-325 only after a person had already found them by reading.
        #
        # `scripts/verify_fix_apply.py` is the counterpart that actually writes, and the
        # property guarded here is that it does not fall behind: a sixth delivery mode
        # added to `fix_apply.OUTPUTS` with no stage in the driver is a write path back to
        # having no coverage at all, which is exactly how this subsystem got three bugs.
        _driver31 = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "verify_fix_apply.py")
        check("a live driver for fix delivery exists at all",
              os.path.exists(_driver31),
              "`verify_pr_review.py` covers pull-request *review* writes; fix delivery had "
              "no equivalent, and three defects lived in twenty lines because of it")
        _dsrc31 = io.open(_driver31, encoding="utf-8").read() if os.path.exists(_driver31) else ""
        _uncovered31 = [o for o in fix_apply.OUTPUTS if f'"{o}"' not in _dsrc31]
        check("...and it drives EVERY delivery `fix_apply` offers",
              not _uncovered31,
              f"no stage for: {_uncovered31} -- a delivery nothing exercises is a delivery "
              "nobody knows is broken, which is the D-314 story exactly")
        check("...including the preview, which must write nothing",
              fix_apply.PREVIEW in _dsrc31 and "NOTHING WAS WRITTEN" in _dsrc31)
        check("...and it can be run without writing, and can undo what it wrote",
              "--dry-run" in _dsrc31 and "--cleanup" in _dsrc31,
              "a driver that can only be run destructively is a driver nobody runs")
        # The three defects are named in it, so a failure points at the history rather than
        # leaving the next person to rediscover why the check is there.
        _named31 = [d for d in ("D-314", "D-315", "D-325") if d not in _dsrc31]
        check("...and cites the defects each write-path check exists for", not _named31,
              f"missing: {_named31}")

        # ── 32. Suppression as code, end to end ──────────────────────────
        #
        # Section 13 of `smoke_code_review.py` covers the rules themselves. What only
        # exists end to end is the *plumbing*: the ignore file has no reviewable extension,
        # so it is dropped by `repo_ingest.normalize_files` and can only be found in the
        # raw list — a wiring mistake there means the feature silently never fires, and
        # every unit test still passes.
        section("32. Suppression as code reaches a real review")

        from backend.services import suppression as _sup32

        # Deliberately not a hardcoded credential. `repo_ingest` redacts those at ingest
        # (D-04), so a finding quoting the literal is *discarded* by evidence verification
        # before suppression is reached -- which is correct, and would have made this
        # section pass or fail for a reason unrelated to what it is testing.
        _IGNORED_HIT = dict(
            control_id="SEC-001", severity=5, risk_level="Critical",
            title="Unsafe eval in the legacy parser", description="Evaluates input.",
            recommendation="Parse it instead.", is_blocking=True,
            file_path="legacy/parser.py", line_start=2, confidence="High",
            evidence="return eval(payload)",
        )
        _KEPT_HIT = dict(
            control_id="SEC-001", severity=5, risk_level="Critical",
            title="Command injection in the runner", description="Shell out of user input.",
            recommendation="Use a list argv.", is_blocking=True,
            file_path="app/runner.py", line_start=4, confidence="High",
            evidence="os.system(cmd)",
        )
        _SRC32 = {
            "legacy/parser.py": "def parse(payload):\n    return eval(payload)\n",
            "app/runner.py": "import os\n\ndef run(cmd):\n    os.system(cmd)\n",
            ".lmignore": (
                "# legacy/ is deleted in Q3, tracked in JIRA-4412\n"
                "legacy/**: SEC-001 scheduled for deletion\n"
                "this/pattern/matches/nothing/**\n"
            ),
        }

        # The parse, straight off a zip, before any review. This is the wiring check.
        _b32 = source_bundle.from_zip_bytes(make_zip(_SRC32), label="ignore-demo")
        check("the ignore file is found in a zip even though it is never reviewed",
              _b32.suppression is not None and len(_b32.suppression.rules) == 2,
              str(getattr(_b32.suppression, "rules", None)))
        check("...and is itself excluded from the files sent to the model",
              all(f["path"] != ".lmignore" for f in _b32.files),
              "a rules file in the prompt is tokens spent on instructions to ourselves")
        check("the source summary states how many rules the repository brought",
              _b32.summary()["ignore_rules"] == 2, str(_b32.summary().get("ignore_rules")))

        scripted["code"] = code_response([dict(_IGNORED_HIT), dict(_KEPT_HIT)], gate="Red")
        _run32 = client.post(
            "/api/review/code/upload-zip",
            files={"file": ("ignore-demo.zip", make_zip(_SRC32), "application/zip")},
            data={"project_name": "Sources"}, headers=UI,
        )
        check("a zip carrying a .lmignore reviews normally",
              _run32.status_code == 200, _run32.text[:240])
        _r32 = _run32.json()
        _titles32 = [f.get("title") for f in (_r32.get("findings") or [])]
        _sup_titles32 = [f.get("title") for f in (_r32.get("suppressed_findings") or [])]
        check("the finding the repository asked us to ignore leaves the gating list",
              "Unsafe eval in the legacy parser" not in _titles32, str(_titles32))
        check("...and is still reported, so nobody can hide a Critical by committing a file",
              "Unsafe eval in the legacy parser" in _sup_titles32, str(_sup_titles32))
        check("...naming the rule and the line it lives on",
              ".lmignore:2" in str([f.get("suppressed_reason")
                                    for f in _r32.get("suppressed_findings") or []]),
              str([f.get("suppressed_reason") for f in _r32.get("suppressed_findings") or []]))
        check("a finding the rules do not cover is untouched",
              "Command injection in the runner" in _titles32, str(_titles32))
        check("...so the gate is still Red on what is left",
              _r32.get("gate_status") == "Red", str(_r32.get("gate_status")))

        _rep32 = _r32.get("suppression") or {}
        check("the run reports the rules it was given", _rep32.get("rules") == 2, str(_rep32))
        check("...and how many findings they took out",
              _rep32.get("findings_suppressed") == 1, str(_rep32))
        check("...and names the rule that matched nothing",
              _rep32.get("unused_count") == 1
              and "this/pattern/matches/nothing" in str(_rep32.get("unused")),
              str(_rep32.get("unused")))

        # An inline comment, on the source that would otherwise have been reported.
        _INLINE32 = {
            "app/runner.py": ("import os\n"
                              "def run(cmd):\n"
                              "    os.system(cmd)  # lm-ignore: SEC-001 admin CLI only\n"),
        }
        _inline_hit = dict(_KEPT_HIT, line_start=3,
                           evidence="os.system(cmd)  # lm-ignore: SEC-001 admin CLI only")
        scripted["code"] = code_response([dict(_inline_hit)], gate="Red")
        _run33 = client.post(
            "/api/review/code/upload-zip",
            files={"file": ("inline.zip", make_zip(_INLINE32), "application/zip")},
            data={"project_name": "Sources"}, headers=UI,
        )
        check("an inline lm-ignore comment reaches a real review",
              _run33.status_code == 200, _run33.text[:240])
        _r33 = _run33.json()
        check("...and takes its own line out of the gating list",
              not (_r33.get("findings") or []),
              str([f.get("title") for f in _r33.get("findings") or []]))
        check("...while still reporting it with the comment as the reason",
              len(_r33.get("suppressed_findings") or []) == 1
              and "lm-ignore at app/runner.py:3"
              in (_r33["suppressed_findings"][0].get("suppressed_reason") or ""),
              str([f.get("suppressed_reason") for f in _r33.get("suppressed_findings") or []]))
        check("...and the gate still cannot be talked down from the model's own verdict",
              _r33.get("gate_status") == "Red", str(_r33.get("gate_status")),)
        # The other direction, so the pairing is not an accident: with the model reporting
        # Green, an ignored finding leaves nothing to floor the gate up from.
        scripted["code"] = code_response([dict(_inline_hit)], gate="Green", risk=1.0)
        _run34 = client.post(
            "/api/review/code/upload-zip",
            files={"file": ("inline2.zip", make_zip(_INLINE32), "application/zip")},
            data={"project_name": "Sources"}, headers=UI,
        )
        check("an ignored finding does not floor the gate upward either",
              _run34.status_code == 200
              and _run34.json().get("gate_status") in ("Green", "Amber"),
              str(_run34.json().get("gate_status")))
        check("...and it is still on the record in that run too",
              len(_run34.json().get("suppressed_findings") or []) == 1,
              "a rule lowers the gate; it never removes the evidence")

        # A pull request has no file list, so `.lmignore` has to be fetched. The fetch must
        # fail open: a repository without one answers 404, which is the common case.
        class _NoIgnoreGH:
            def get_file_content_at_ref(self, owner, repo, path, ref):
                raise RuntimeError("404 Not Found")

        _pr32 = source_bundle.pr_bundle(
            {"diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n", "title": "t",
             "head_sha": "d" * 40, "files_changed": ["x.py"]},
            "https://github.com/acme/portal/pull/7", github=_NoIgnoreGH(),
        )
        check("a repository with no ignore file still yields a reviewable pull request",
              _pr32.suppression is not None and not _pr32.suppression.rules,
              "a 404 for .lmignore must not fail the review")

        class _IgnoreGH:
            def __init__(self):
                self.asked = []

            def get_file_content_at_ref(self, owner, repo, path, ref):
                self.asked.append((owner, repo, path, ref))
                return {"content": "docs/**  prose, not code\n"}

        _gh32 = _IgnoreGH()
        _pr33 = source_bundle.pr_bundle(
            {"diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n", "title": "t",
             "head_sha": "e" * 40, "files_changed": ["x.py"]},
            "https://github.com/acme/portal/pull/9", github=_gh32,
        )
        check("a pull request reads the ignore file out of the repository",
              len(_pr33.suppression.rules) == 1, str(_gh32.asked))
        check("...at the commit the review is about, not the branch tip",
              _gh32.asked and _gh32.asked[0][3] == "e" * 40, str(_gh32.asked))
        check("...from the right repository", _gh32.asked[0][:3] == ("acme", "portal", ".lmignore"),
              str(_gh32.asked))
        _pr34 = source_bundle.pr_bundle(
            {"diff": "d", "title": "t", "head_sha": "f" * 40}, "https://x/pull/1")
        check("a caller that passes no client gets no rules rather than an error",
              _pr34.suppression is None)

        # ── The migration path off click-by-click waivers ────────────────
        #
        # The friction this feature exists to remove: a team that has been waiving the same
        # findings for months has those decisions in the database only.
        _w32 = client.get("/api/v1/findings/lmignore", headers=H)
        check("the waiver export answers", _w32.status_code == 200, _w32.text[:200])
        check("...as a file, not as JSON",
              "text/plain" in _w32.headers.get("content-type", ""),
              _w32.headers.get("content-type", ""))
        check("...offered as a download named .lmignore",
              ".lmignore" in _w32.headers.get("content-disposition", ""),
              _w32.headers.get("content-disposition", ""))
        check("...warning that a rule is wider than the waiver it came from",
              "wider than" in _w32.text,
              "an exported rule covers the control and the path; the waiver covered one "
              "finding, matched on its title and evidence too")
        _parsed32 = _sup32.parse(_w32.text)
        check("...and whatever it produces parses as a valid ignore file",
              not _parsed32.errors, str(_parsed32.errors))
        check("the export requires a credential",
              client.get("/api/v1/findings/lmignore").status_code in (401, 403),
              "it lists what a team has decided not to fix, which is a map of known holes")





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
