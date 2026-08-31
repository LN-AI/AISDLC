"""Build a converted codebase and report the compiler's verdict back to the platform.

    python scripts/build_run.py --fixture
    python scripts/build_run.py --run <run_id> --user admin --password ...
    python scripts/build_run.py --loop --serve --run <run_id> --user admin --password ...

This is the shortest path from "the run finished" to "does it actually compile", and it needs
no GitHub, no Actions secret and no Docker — only the .NET SDK already on the machine. It
downloads the run's export, unzips it, runs `dotnet build`, and POSTs the captured log to
`POST /api/v1/modernization/runs/{id}/build-results`, where the failing units are reopened and
re-converted with the compiler's own errors in the prompt.

`--loop` closes that loop rather than describing it. One build reports and stops, printing
"resume the run to re-convert them" — so reaching a clean build was a manual ping-pong between
two commands, which is why a real 45-file run finished at round 3 with four errors outstanding
and nobody ever built again. The loop alternates build, report, resume, build until the export
compiles or the platform says another build cannot help. It never answers a blocking checkpoint
it was not told to answer: that is a decision the product deliberately handed to a human.

`--serve` asks the question after that one: does it *start*, and does it answer its own URLs.
Compiling is not the bar. This product has twice shipped an export that built with zero errors
and failed on its first request — a view emitted under a name its controller does not ask for,
and a container missing a registration — and both were found by a human running it. The URLs
come from the run's own route evidence via `GET /runs/{id}/startup-plan`, so what is probed is
the application's declared surface rather than a list maintained beside it.

**This does not weaken D-05.** That decision says no compiling or subprocess execution *on the
API host*. This is a developer-run script — the same tier as the customer's CI, which is
exactly the path D-05 named — and the backend still only ever receives diagnostics and
observations. Nothing here imports the FastAPI app or runs inside it, and nothing in `backend/`
may import this.

`--fixture` builds the scaffold for a synthetic project offline, with no server and no API
key, and exits non-zero if it does not compile. That check exists because a `global.json`
pinning an SDK band nobody had installed shipped undetected: every export failed at SDK
resolution before a line of C# was read, and no offline test could see it because none of
them ever ran a compiler.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Long enough for a cold `dotnet restore`, which downloads the whole package graph the first
# time and is routinely minutes on a fresh machine.
BUILD_TIMEOUT_SECONDS = 900


def _dotnet_available() -> bool:
    return shutil.which("dotnet") is not None


def _run_build(directory: str) -> tuple:
    """-> (exit_code, combined output). stderr is merged into stdout because MSBuild splits
    diagnostics across both and the parser wants them in one stream."""
    proc = subprocess.run(
        ["dotnet", "build", "--nologo"],
        cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", timeout=BUILD_TIMEOUT_SECONDS,
    )
    return proc.returncode, proc.stdout


def login(api: str, user: str, password: str) -> str:
    """Exchange a username and password for a bearer token, as the browser does.

    This exists because the friction was the point of failure. `build_run.py` has been in the
    repository, working, since the build loop was written, and the first thing it asked for
    was an API key somebody had to go and provision — so the export of a real 45-file run was
    never compiled, and shipped with a project reference cycle MSBuild refuses outright
    (D-99). `get_caller` already accepts a bearer token on every one of these endpoints; only
    this script did not offer one.
    """
    request = urllib.request.Request(
        f"{api}/api/auth/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        # `token`, which is what `POST /api/auth/login` returns and what
        # `drive_modernization.py` reads. `access_token` is accepted too so a deployment that
        # renames it does not turn a working script into a silent 401.
        payload = json.loads(response.read().decode())
    return payload.get("token") or payload.get("access_token") or ""


def _http(url: str, key: str, method: str = "GET", payload: dict = None,
          token: str = "", project_id: str = "") -> tuple:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if key:
        request.add_header("X-API-Key", key)
    if project_id:
        # Some deployments bind every call to a project and answer 403 without this, with a
        # message that does not mention the header. See NEXT-SESSION's landmine list.
        request.add_header("X-Project-Id", project_id)
    if body:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def download_export(api: str, key: str, run_id: str, keep: str = "",
                    token: str = "", project_id: str = ""):
    """-> (workdir, exit_code). `workdir` is "" when the code is non-zero."""
    status, blob = _http(f"{api}/api/v1/modernization/runs/{run_id}/export", key,
                         token=token, project_id=project_id)
    if status != 200:
        print(f"Could not download the export: HTTP {status}\n{blob[:400].decode(errors='replace')}")
        return "", 2

    workdir = keep or tempfile.mkdtemp(prefix="lm-build-")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        # `extractall` is safe here only because these paths came from our own export, which
        # `repo_ingest.safe_relpath` already vetted. Re-checking costs nothing and this is the
        # one place a bad path would land on a developer's disk.
        for member in archive.namelist():
            if member.startswith(("/", "\\")) or ".." in member.replace("\\", "/").split("/"):
                print(f"Refusing to extract {member!r}")
                return "", 2
        archive.extractall(workdir)
    print(f"Extracted {len(archive.namelist())} file(s) to {workdir}")
    return workdir, 0


def post_results(api: str, key: str, run_id: str, code: int, log: str,
                 token: str = "", project_id: str = ""):
    """-> (result dict, exit_code). Reports one build to the platform."""
    payload = {"log": log, "source": "local_dotnet", "command": "dotnet build"}
    if code == 0:
        # The positive claim. An empty parse of a clean log is indistinguishable from a log
        # nothing could be read out of, and the platform refuses the latter.
        payload["diagnostics"] = []
    status, response = _http(
        f"{api}/api/v1/modernization/runs/{run_id}/build-results", key, "POST", payload,
        token=token, project_id=project_id)
    if status != 200:
        print(f"Reporting failed: HTTP {status}\n{response[:400].decode(errors='replace')}")
        return {}, 4
    return json.loads(response), 0


def build_export(api: str, key: str, run_id: str, keep: str = "",
                 token: str = "", project_id: str = "") -> int:
    """One build, one report. The single-shot path."""
    workdir, err = download_export(api, key, run_id, keep, token, project_id)
    if err:
        return err
    if not _dotnet_available():
        print("No `dotnet` on PATH — install the .NET SDK to build.")
        return 3
    code, log = _run_build(workdir)
    print(log[-3000:])

    result, err = post_results(api, key, run_id, code, log, token, project_id)
    if err:
        return err
    print(f"\nReported round {result.get('round')}: {result.get('summary')}")
    if result.get("reopened"):
        print(f"{result['reopened']} unit(s) reopened — resume the run to re-convert them.")
    return 0 if code == 0 else 1


# ── Does it start, and does it answer? ─────────────────────────────────────
#
# The question between "does it compile" and "does it behave the same", and the one this
# product has twice shipped the wrong answer to with every offline check green: a view emitted
# under a name its controller does not ask for (C-60) and a container missing a registration
# (C-59). The first throws on one route, the second on all of them, and no compiler sees either.
#
# D-05 covers this exactly as it covers `dotnet build` above: the API host runs nothing, and a
# developer's machine or the customer's CI is the tier the decision names. Nothing in `backend/`
# may import this.

STARTUP_TIMEOUT_SECONDS = 90
PROBE_TIMEOUT_SECONDS = 20

# What the generated host reads for its connection string (`wireup.CONNECTION_NAME`), as the
# environment-variable spelling ASP.NET Core maps onto configuration.
_CONNECTION_ENV = "ConnectionStrings__DefaultConnection"

# Providers the generated `Program.cs` can register. Only SQLite can be stood up with nothing
# installed, so it is the only one this gets a free default for.
_PROVIDERS = (("UseSqlite", "sqlite"), ("UseSqlServer", "sqlserver"),
              ("UseNpgsql", "postgresql"), ("UseMySql", "mysql"), ("UseMySQL", "mysql"),
              ("UseOracle", "oracle"))


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _find_entry(workdir: str, named: str = "") -> str:
    """The project directory to run. `named` is what the platform's plan said."""
    if named and os.path.isdir(os.path.join(workdir, named)):
        return named
    hosts = []
    for root, _dirs, files in os.walk(workdir):
        if "Program.cs" in files:
            hosts.append(os.path.relpath(root, workdir).replace("\\", "/"))
    # Only when there is no ambiguity. Guessing between two hosts would start the wrong one and
    # report its 404s as the conversion's fault.
    return hosts[0] if len(hosts) == 1 else ""


def _db_provider(workdir: str, entry: str) -> str:
    try:
        with io.open(os.path.join(workdir, entry, "Program.cs"), encoding="utf-8",
                     errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    for needle, name in _PROVIDERS:
        if needle in text:
            return name
    return ""


def _seed_sqlite(workdir: str, db_path: str) -> str:
    """Apply any `schema*.sql` the export carries. -> a note, or ""."""
    import glob
    import sqlite3
    scripts = sorted(glob.glob(os.path.join(workdir, "**", "schema*.sql"), recursive=True))
    if not scripts:
        return "no schema*.sql in the export, so the database is empty"
    applied = []
    for script in scripts:
        try:
            with io.open(script, encoding="utf-8", errors="replace") as handle:
                sql = handle.read()
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(sql)
                connection.commit()
            finally:
                connection.close()
            applied.append(os.path.basename(script))
        except Exception as exc:  # noqa: BLE001
            # A MySQL dump is not SQLite, and that is a fact about the source rather than a
            # defect in the conversion. Reported and carried on: an empty database still tells
            # us whether the application starts.
            return f"{os.path.basename(script)} would not apply to SQLite ({exc})"
    return f"seeded from {', '.join(applied)}"


def _probe(base_url: str, urls: list) -> list:
    checks = []
    for entry in urls:
        if not entry.get("probe"):
            continue
        method, path = entry.get("method") or "GET", entry.get("path") or "/"
        request = urllib.request.Request(base_url.rstrip("/") + path, method=method)
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
                status, error = response.status, ""
        except urllib.error.HTTPError as exc:
            status, error = exc.code, ""
        except Exception as exc:  # noqa: BLE001
            status, error = 0, str(exc)[:200]
        checks.append({"method": method, "path": path, "status": status, "error": error,
                       "ms": int((time.time() - started) * 1000)})
        print(f"  {method} {path} -> {status or error}")
    return checks


def serve_export(api: str, key: str, run_id: str, workdir: str, token: str = "",
                 project_id: str = "", connection: str = "") -> int:
    """Start the built export, request its own URLs, and report what answered."""
    status, body = _http(f"{api}/api/v1/modernization/runs/{run_id}/startup-plan", key,
                         token=token, project_id=project_id)
    if status != 200:
        print(f"Could not read the startup plan: HTTP {status}")
        return 4
    plan = json.loads(body)
    urls = plan.get("urls") or []

    entry = _find_entry(workdir, plan.get("entry_project") or "")
    if not entry:
        return _report_runtime(api, key, run_id, token, project_id, started=False, checks=[],
                              skipped_reason="no single project with a Program.cs to start")

    provider = _db_provider(workdir, entry)
    note = ""
    if connection:
        pass
    elif provider in ("", "sqlite"):
        db_path = os.path.join(workdir, "startup-smoke.db")
        connection = f"Data Source={db_path}"
        note = _seed_sqlite(workdir, db_path)
    else:
        # Not a conversion defect, and it must not read as a pass. A skip is its own state.
        return _report_runtime(
            api, key, run_id, token, project_id, started=False, checks=[],
            skipped_reason=(f"the generated host registers {provider}, which is not available "
                            f"on this machine — pass --connection to point it at one"))

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ)
    environment.update({
        "ASPNETCORE_URLS": base_url,
        # Better exception bodies in the log tail. It must not change pass/fail, and does not:
        # the verdict is computed from status codes.
        "ASPNETCORE_ENVIRONMENT": "Development",
        # Mandatory. `wireup.apply_to_appsettings` writes the connection string deliberately
        # empty, because it refuses to commit a credential — so with nothing here the host
        # fails on its first query rather than on anything the conversion got wrong.
        _CONNECTION_ENV: connection,
    })
    print(f"Starting {entry} on {base_url}" + (f" ({note})" if note else ""))
    process = subprocess.Popen(
        ["dotnet", "run", "--project", os.path.join(entry, f"{os.path.basename(entry)}.csproj"),
         "--no-build", "--no-launch-profile"],
        cwd=workdir, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    try:
        deadline = time.time() + STARTUP_TIMEOUT_SECONDS
        alive = False
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base_url + "/", timeout=3):
                    alive = True
                    break
            except urllib.error.HTTPError:
                # Any HTTP answer at all means the host is up; the status is the probe's
                # business, not the readiness check's.
                alive = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        checks = _probe(base_url, urls) if alive else []
    finally:
        process.terminate()
        try:
            tail = process.communicate(timeout=20)[0] or ""
        except subprocess.TimeoutExpired:
            process.kill()
            tail = process.communicate()[0] or ""
    if not alive:
        print("The application never answered on its port.")
    print(tail[-2000:])
    return _report_runtime(api, key, run_id, token, project_id, started=alive, checks=checks,
                           base_url=base_url, log_tail=tail[-20000:])


def _report_runtime(api: str, key: str, run_id: str, token: str, project_id: str, *,
                    started: bool, checks: list, skipped_reason: str = "",
                    base_url: str = "", log_tail: str = "") -> int:
    payload = {"checks": checks, "started": started, "skipped_reason": skipped_reason,
               "source": "local_dotnet", "base_url": base_url, "log_tail": log_tail,
               "command": "dotnet run"}
    status, body = _http(f"{api}/api/v1/modernization/runs/{run_id}/runtime-results", key,
                         "POST", payload, token=token, project_id=project_id)
    if status != 200:
        print(f"Reporting failed: HTTP {status}\n{body[:400].decode(errors='replace')}")
        return 4
    result = json.loads(body)
    print(f"\nStartup check: {result.get('status')} — {result.get('checks')} URL(s) probed, "
          f"{result.get('failing')} failing")
    if skipped_reason:
        print(f"Skipped: {skipped_reason}")
    return 0 if result.get("status") == "passed" else 1


# ── The loop ───────────────────────────────────────────────────────────────

# Statuses `ingest` returns that mean the platform has stopped trying. Each one already raised
# its own blocking checkpoint, so the loop prints it and stops rather than asking again.
_TERMINAL_BUILD_STATUSES = ("stuck", "round_limit", "unmapped", "rework_exhausted")

# The build gates a rework round is expected to raise and withdraw. The loop does not treat
# these as "a human is needed" — every other blocking checkpoint it does.
_LOOP_ANSWERS = ("build_required", "build_errors_outstanding")

POLL_SECONDS = 6


def _manifest(api: str, key: str, run_id: str, token: str, project_id: str) -> dict:
    status, body = _http(f"{api}/api/v1/modernization/runs/{run_id}", key,
                         token=token, project_id=project_id)
    return json.loads(body) if status == 200 else {}


def _blocking_kinds(api: str, key: str, run_id: str, token: str, project_id: str) -> list:
    status, body = _http(f"{api}/api/v1/modernization/runs/{run_id}/checkpoints", key,
                         token=token, project_id=project_id)
    if status != 200:
        return []
    rows = json.loads(body)
    rows = rows.get("checkpoints", rows) if isinstance(rows, dict) else rows
    return [c.get("kind") for c in rows
            if c.get("blocking") and c.get("status") == "open"]


def loop(api: str, key: str, run_id: str, keep: str = "", token: str = "",
         project_id: str = "", max_rounds: int = 5, deadline_hours: float = 3.0,
         serve: bool = False, connection: str = "") -> int:
    """Build, report, resume, build again — until it compiles or provably cannot.

    The single-shot path prints "resume the run to re-convert them" and stops, so getting to a
    clean build was a manual ping-pong between two commands. That is why the traced 45-file run
    finished at round 3 with four errors outstanding and nobody built again: the loop existed in
    the backend and nothing drove it.

    **A blocking checkpoint the loop was not told to answer stops it.** Same rule
    `drive_modernization.py` states: a blocking checkpoint is a decision the product has
    deliberately handed to a human, and auto-answering it would defeat the only mechanism that
    asks. The two build gates are the exception — they exist to be answered by *this*, and a
    rework round withdraws them itself.

    The export is re-downloaded every round, because the whole point is that the artifacts
    changed.
    """
    if not _dotnet_available():
        print("No `dotnet` on PATH — install the .NET SDK to build.")
        return 3
    started = time.time()

    for round_no in range(1, max_rounds + 1):
        workdir, err = download_export(api, key, run_id, keep, token, project_id)
        if err:
            return err
        code, log = _run_build(workdir)
        result, err = post_results(api, key, run_id, code, log, token, project_id)
        if err:
            return err

        status = result.get("status") or ""
        print(f"[round {round_no}] {status}: {result.get('errors', '?')} error(s), "
              f"{result.get('reopened', 0)} unit(s) reopened — {result.get('summary', '')}")

        if status == "passed":
            print(f"Clean build after {round_no} round(s).")
            if serve:
                # The bar is "compiles and runs", so a clean build is the middle of the check
                # rather than the end of it. Same workdir: the export was just built there, so
                # a failure here is provably a runtime failure and not a stale binary.
                return serve_export(api, key, run_id, workdir, token, project_id, connection)
            return 0
        if status in _TERMINAL_BUILD_STATUSES:
            print(f"Stopping: the platform reported {status!r}. "
                  f"Read the checkpoint it raised — this is not something another build fixes.")
            return 1

        held = [k for k in _blocking_kinds(api, key, run_id, token, project_id)
                if k not in _LOOP_ANSWERS]
        if held:
            print(f"Stopping: {', '.join(sorted(set(held)))} needs a human. "
                  f"That is a decision the product asked for, not a build failure.")
            return 2
        if status == "awaiting_rework":
            # Not a dead end, and reading it as one stopped a run whose repair was already
            # queued: the loop built, reopened three units, built *again* without resuming,
            # saw the same six errors, and gave up while the fix sat in the queue untouched.
            # The platform's own summary says "resume the run" in those words. Falling through
            # to the resume below is doing what it asked.
            print("  the previous round's rework has not been converted yet — resuming "
                  "rather than rebuilding into the same errors")
        elif not result.get("reopened"):
            print("Stopping: nothing was reopened, so another build would report the same "
                  "errors. Nothing here can move it forward.")
            return 1

        status_code, body = _http(f"{api}/api/v1/modernization/runs/{run_id}/resume", key,
                                  "POST", {}, token=token, project_id=project_id)
        if status_code not in (200, 202):
            print(f"Could not resume: HTTP {status_code}\n{body[:400].decode(errors='replace')}")
            return 4

        # Poll until the rework has settled. A run that goes back to `paused` or a terminal
        # state has stopped making progress, and so has one that raised something blocking.
        while True:
            if time.time() - started > deadline_hours * 3600:
                print(f"Stopping: {deadline_hours}h elapsed without a clean build.")
                return 1
            time.sleep(POLL_SECONDS)
            run = _manifest(api, key, run_id, token, project_id)
            if not run:
                print("Stopping: the run manifest could not be read.")
                return 4
            if run.get("blocking_checkpoints"):
                break
            if run.get("status") in ("paused", "completed", "failed", "cancelled"):
                break
            print(f"  {run.get('status')} / {(run.get('stage') or {}).get('id', '?')} — "
                  f"{run.get('progress_pct', 0)}%, ${run.get('spent_usd', 0)}")

    print(f"Stopping: {max_rounds} round(s) used without a clean build.")
    return 1


def build_fixture() -> int:
    """Generate a scaffold offline and compile it. No server, no API key, no model."""
    from backend.services.modernization import scaffold

    charter = {
        "target_stack": ".NET 8 / ASP.NET Core MVC",
        "project_layout": "one project per layer",
        "data_access": "EF Core 8 with the Pomelo MySQL provider",
        "logging": "ILogger<T>, structured",
        "ui_strategy": "Razor views",
    }
    projects = [{"name": "Fixture.Web", "purpose": "entry"},
                {"name": "Fixture.Domain", "purpose": "domain"}]
    files = scaffold.build(charter["target_stack"], charter, projects, ["db_password"])
    if not files:
        print("FAIL: the scaffold produced no files for this target stack.")
        return 1

    workdir = tempfile.mkdtemp(prefix="lm-fixture-")
    for entry in files:
        path = os.path.join(workdir, entry["path"].replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(entry["content"])
    print(f"Wrote {len(files)} scaffold file(s) to {workdir}")

    if not _dotnet_available():
        print("SKIP: no `dotnet` on PATH. Install the .NET SDK to run this check.")
        return 0
    code, log = _run_build(workdir)
    print(log[-2000:])
    if code != 0:
        print("\nFAIL: the generated scaffold does not compile. Nothing converted can build "
              "on top of it, so this is the first thing to fix.")
        return 1
    print("\nOK: the generated scaffold compiles.")

    # And does it *start*? The compile check exists because a `global.json` pinning an SDK band
    # nobody had installed shipped undetected; this one exists for the same reason one layer up.
    # A host that builds and throws on startup — a service the container cannot construct, a
    # configuration key read before it is set — is invisible to every offline check in the repo,
    # and it is the whole reason `--serve` exists. Only "it answered", not "it served a route":
    # the fixture has no controllers, so a 404 is the correct response and not a defect.
    entry = _find_entry(workdir)
    if not entry:
        print("SKIP: the fixture has no single project with a Program.cs.")
        shutil.rmtree(workdir, ignore_errors=True)
        return 0
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ)
    environment.update({"ASPNETCORE_URLS": base_url, "ASPNETCORE_ENVIRONMENT": "Development",
                        _CONNECTION_ENV: f"Data Source={os.path.join(workdir, 'fixture.db')}"})
    process = subprocess.Popen(
        ["dotnet", "run", "--project",
         os.path.join(entry, f"{os.path.basename(entry)}.csproj"),
         "--no-build", "--no-launch-profile"],
        cwd=workdir, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    answered = False
    try:
        deadline = time.time() + STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline and process.poll() is None:
            try:
                with urllib.request.urlopen(base_url + "/", timeout=3):
                    answered = True
                    break
            except urllib.error.HTTPError:
                answered = True          # any HTTP answer means the host is up
                break
            except Exception:            # noqa: BLE001
                time.sleep(1)
    finally:
        process.terminate()
        try:
            tail = process.communicate(timeout=20)[0] or ""
        except subprocess.TimeoutExpired:
            process.kill()
            tail = process.communicate()[0] or ""
    if not answered:
        print(tail[-2000:])
        print("\nFAIL: the generated scaffold compiles and does not start. Every conversion "
              "built on it would ship an application that throws on its first request.")
        shutil.rmtree(workdir, ignore_errors=True)
        return 1
    print("OK: and it starts and answers on its port.")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixture", action="store_true",
                        help="Build a synthetic scaffold offline. No server or key needed.")
    parser.add_argument("--run", help="Modernization run id to build.")
    parser.add_argument("--api", default="http://localhost:8000", help="Platform base URL.")
    parser.add_argument("--key", default=os.getenv("LM_API_KEY", ""),
                        help="Platform API key (or set LM_API_KEY).")
    parser.add_argument("--user", default="", help="Username, if you have no API key.")
    parser.add_argument("--password", default=os.getenv("LM_PASSWORD", ""),
                        help="Password for --user (or set LM_PASSWORD).")
    parser.add_argument("--project", default="", help="X-Project-Id. Required where the "
                                                      "deployment binds every call to a project.")
    parser.add_argument("--keep", default="", help="Extract to this directory instead of a "
                                                   "temp one, and leave it in place.")
    parser.add_argument("--loop", action="store_true",
                        help="Build, report, resume, build again until it compiles or the "
                             "platform says another build cannot help.")
    parser.add_argument("--max-rounds", type=int, default=8,
                        help="Rounds the loop may use (default 8, matching MAX_BUILD_ROUNDS).")
    parser.add_argument("--hours", type=float, default=3.0,
                        help="Give up after this many hours (default 3).")
    parser.add_argument("--serve", action="store_true",
                        help="After a clean build, start the app and request every URL the "
                             "run's own analysis found. Reports to /runtime-results.")
    parser.add_argument("--connection", default="",
                        help="Connection string for the startup check. Only needed when the "
                             "generated host registers something other than SQLite.")
    args = parser.parse_args()

    if args.fixture:
        return build_fixture()
    if not args.run:
        parser.error("one of --fixture or --run is required")
    api = args.api.rstrip("/")

    token = ""
    if not args.key:
        if not args.user:
            parser.error("--key/LM_API_KEY, or --user with --password, is required to reach "
                         "the platform")
        try:
            token = login(api, args.user, args.password)
        except Exception as exc:
            print(f"Could not log in as {args.user!r}: {exc}")
            return 5
        if not token:
            print("The login succeeded but returned no token.")
            return 5
    if args.loop:
        return loop(api, args.key, args.run, args.keep, token, args.project,
                    max_rounds=args.max_rounds, deadline_hours=args.hours,
                    serve=args.serve, connection=args.connection)
    if args.serve:
        # Without --loop, start whatever is already in the export. Useful when the build is
        # known good and only the runtime question is open.
        workdir, err = download_export(api, args.key, args.run, args.keep, token, args.project)
        if err:
            return err
        if not _dotnet_available():
            print("No `dotnet` on PATH — install the .NET SDK to run the app.")
            return 3
        code, log = _run_build(workdir)
        if code != 0:
            print(log[-3000:])
            print("The export does not compile, so there is nothing to start.")
            return 1
        return serve_export(api, args.key, args.run, workdir, token, args.project,
                            args.connection)
    return build_export(api, args.key, args.run, args.keep, token, args.project)


if __name__ == "__main__":
    sys.exit(main())
