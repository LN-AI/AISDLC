"""smoke_cli — `scripts/lmsdlc.py` against a real server, gateway stubbed.

Part of the offline suite: no API key, no network, no spend. It starts a server in its own
process on its own database, mints a real API key, and drives the client as a subprocess —
so it checks the thing a user runs, not an importable half of it.

Why a real server rather than a TestClient: the client's whole value is the parts that only
exist end to end. An exit code. A SARIF file on disk. An API key that authenticates. Two
defects were found here and could not have been found anywhere else:

  * `--files` sent the path the shell handed over, so an absolute path was refused
    server-side as `unsafe_path` — and the message a user saw talked about binary and
    generated files rather than about their path.
  * The fix for that used `os.path.relpath`, which on Windows **raises** across drives
    rather than returning something starting with `..`, so a repository on D: and a file on
    C: died with a ValueError about mount points instead of the sentence written for it.

Run:  venv/Scripts/python.exe scripts/smoke_cli.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import httpx  # noqa: E402

PORT = 8719
db = os.path.join(tempfile.gettempdir(), f"clitest_{uuid.uuid4().hex}.db")
boot = os.path.join(tempfile.gettempdir(), f"clitest_{PORT}.py")
io.open(boot, "w", encoding="utf-8").write(
    "import sys, uvicorn\n"
    f"sys.path.insert(0, r'{REPO}')\n"
    "from backend.services import llm_gateway\n"
    "from backend import config as _cfg\n"
    "def _stub(system_prompt, user_content, schema, override=None, max_tokens=None, **kw):\n"
    "    llm_gateway._record_usage('anthropic', _cfg.MODEL, 1800, 420)\n"
    "    return dict(risk_score=90.0, gate_status='Green', summary='Stubbed.',\n"
    "                compliance_flags=[], recommendations=['Parameterize queries.'],\n"
    "                findings=[dict(control_id='SEC-003', severity=5, risk_level='Critical',\n"
    "                    title='SQL injection in login', description='Concatenated query.',\n"
    "                    recommendation='Use a parameterized query.', is_blocking=True,\n"
    "                    file_path='out-clitest/cli_auth.py', line_start=1, confidence='High',\n"
    "                    evidence='q = \"SELECT * FROM users WHERE u=\" + u')])\n"
    "llm_gateway.call_tool = _stub\n"
    "import backend.main as _m\n"
    "_m.llm_settings_service.is_llm_configured = lambda: True\n"
    f"uvicorn.run('backend.main:app', host='127.0.0.1', port={PORT}, log_level='warning')\n"
)
env = {k: str(v) for k, v in os.environ.items()}
env.update({"DATABASE_PATH": db, "PYTHONIOENCODING": "utf-8",
            "SECRET_KEY": "clitest-not-for-production"})
log = io.open(os.path.join(tempfile.gettempdir(), f"clitest_{PORT}.log"), "w",
              encoding="utf-8")
proc = subprocess.Popen([sys.executable, boot], cwd=REPO, env=env, stdout=log,
                        stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"
failures = []
src_dir = os.path.join(REPO, "out-clitest")


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label)
    if not ok:
        failures.append(label)
        if detail:
            print("        " + str(detail).replace("\n", "\n        ")[:600])


try:
    for _ in range(80):
        try:
            if httpx.get(base + "/", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.25)
    else:
        print("server never came up; see", log.name)
        sys.exit(1)

    tok = httpx.post(base + "/api/auth/login",
                     json={"username": "admin", "password": "Admin@1234"},
                     timeout=10).json()["token"]
    H = {"Authorization": f"Bearer {tok}"}
    proj = httpx.post(base + "/api/v1/projects", headers=H,
                      json={"name": "cli-test"}, timeout=10).json()["project"]
    key = httpx.post(base + "/api/v1/keys", headers=H,
                     json={"team_name": "ci", "scopes": "all",
                           "project_id": proj["id"]}, timeout=10).json()
    # The response is {"message": ..., "key": record}; the raw key is returned only at
    # creation. Asserting its shape here rather than discovering a 401 nine checks later,
    # which is what the first version of this harness did.
    api_key = (key.get("key") or {}).get("raw_key") or ""
    if not api_key.startswith("lmsdlc_"):
        print("could not mint an API key:", json.dumps(key)[:400])
        sys.exit(1)

    cli_env = {k: str(v) for k, v in os.environ.items()}
    cli_env.update({"LM_SDLC_URL": base, "LM_SDLC_API_KEY": api_key,
                    "PYTHONIOENCODING": "utf-8"})

    # A file inside the repository, named relative to it — how somebody in a checkout
    # actually invokes this.
    os.makedirs(src_dir, exist_ok=True)
    io.open(os.path.join(src_dir, "cli_auth.py"), "w", encoding="utf-8").write(
        'q = "SELECT * FROM users WHERE u=" + u\n')
    REL = "out-clitest/cli_auth.py"

    def run(*a):
        return subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "lmsdlc.py"), *a],
            cwd=REPO, env=cli_env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)

    sys.path.insert(0, REPO)
    from backend.services import suppression as _suppression

    print("\nlmsdlc against a real server\n" + "-" * 62)

    r = run("review", "--files", REL, "--quote-only")
    check("--quote-only prices the run", r.returncode == 0 and "ceiling" in r.stdout,
          r.stdout + r.stderr)
    check("...and says nothing was spent", "Nothing was spent" in r.stdout, r.stdout)

    r = run("review", "--files", REL)
    check("a file review runs and reports the gate",
          r.returncode == 0 and "Gate:" in r.stdout, r.stdout + r.stderr)
    check("...and prints the finding with its severity",
          "CRITICAL" in r.stdout and "SQL injection" in r.stdout, r.stdout)
    check("...and what it cost", "Cost: $" in r.stdout, r.stdout)
    check("...and that the arithmetic overruled the model's Green", "arithmetic" in r.stdout,
          "the stub answers Green over a Critical, so the gate floor must show Red and "
          "say why -- " + r.stdout)

    # The exit code is the whole point of having a client rather than curl.
    r = run("review", "--files", REL, "--fail-on", "red")
    check("--fail-on red exits 1 on a Red gate", r.returncode == 1,
          f"rc={r.returncode}\n" + r.stdout + r.stderr)
    check("...and says why it failed", "FAILING" in r.stdout, r.stdout)
    r = run("review", "--files", REL, "--fail-on", "amber")
    check("--fail-on amber also fails on Red, being stricter", r.returncode == 1,
          f"rc={r.returncode}")

    sarif_path = os.path.join(tempfile.gettempdir(), "cli.sarif")
    if os.path.exists(sarif_path):
        os.remove(sarif_path)
    r = run("review", "--files", REL, "--sarif", sarif_path)
    ok = os.path.exists(sarif_path)
    check("--sarif writes a file", ok, r.stdout + r.stderr)
    if ok:
        doc = json.loads(io.open(sarif_path, encoding="utf-8").read())
        check("...and it is SARIF 2.1.0", doc.get("version") == "2.1.0", str(doc)[:200])
        results = doc["runs"][0]["results"]
        check("...carrying the finding", len(results) >= 1, json.dumps(results)[:300])
        check("...with a fingerprint, so an alert survives code moving down a file",
              "partialFingerprints" in results[0], json.dumps(results[0])[:400])

    r = run("--json", "review", "--files", REL)
    ok = False
    try:
        ok = r.returncode == 0 and bool(json.loads(r.stdout).get("gate_status"))
    except Exception:
        pass
    check("--json emits parseable output", ok, r.stdout[:300] + r.stderr[:300])

    r = run("findings", "--severity", "5")
    check("findings lists what is open", r.returncode == 0 and "critical" in r.stdout,
          r.stdout + r.stderr)
    r = run("findings", "--severity", "5", "--repo", "no-such-repo")
    check("...and a repository filter matching nothing says zero, not everything",
          r.returncode == 0 and "0 matching" in r.stdout, r.stdout + r.stderr)

    # `--lmignore` writes a file. Only a subprocess can tell you that it landed on disk
    # with the right content, which is the same reason this whole script exists.
    ign = os.path.join(src_dir, "exported.lmignore")
    r = run("findings", "--lmignore", ign)
    check("--lmignore reports what it wrote",
          r.returncode == 0 and "Wrote" in r.stdout and "rule(s)" in r.stdout,
          r.stdout + r.stderr)
    check("...and the file is really on disk", os.path.exists(ign), ign)
    body = io.open(ign, encoding="utf-8").read() if os.path.exists(ign) else ""
    check("...carrying the warning that a rule is wider than the waiver",
          "wider than" in body, body[:300])
    check("...and parses as an ignore file with no errors",
          not _suppression.parse(body).errors,
          str(_suppression.parse(body).errors))
    check("...and it warns the reader before they commit it",
          "Read it before committing" in r.stdout, r.stdout)
    r = run("findings", "--lmignore", "-")
    check("'-' writes the file to stdout instead",
          r.returncode == 0 and "wider than" in r.stdout and "Wrote" not in r.stdout,
          r.stdout[:300] + r.stderr[:200])

    # Both of these were real client defects, found here and nowhere else.
    outside = os.path.join(tempfile.gettempdir(), "lmsdlc_outside.py")
    io.open(outside, "w", encoding="utf-8").write("x = 1" + chr(10))
    r = run("review", "--files", outside)
    check("a file outside the repository is refused with a sentence about paths",
          r.returncode == 2 and "repository-relative" in r.stderr
          and "Traceback" not in r.stderr, f"rc={r.returncode}\n" + r.stderr[:400])
    r = run("review", "--pr", "https://github.com/does-not-exist/nope/pull/1")
    check("an unreachable source fails with a sentence, not a traceback",
          r.returncode == 2 and "lmsdlc:" in r.stderr and "Traceback" not in r.stderr,
          f"rc={r.returncode}\n" + r.stderr[:300])

    print("-" * 62)
    if failures:
        print(f"FAILED: {len(failures)} check(s) failed")
        for f in failures:
            print("  - " + f)
        sys.exit(1)
    print("OK: all CLI checks passed")
    sys.exit(0)
finally:
    proc.terminate()
    log.close()
    shutil.rmtree(src_dir, ignore_errors=True)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass
