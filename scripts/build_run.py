"""Build a converted codebase and report the compiler's verdict back to the platform.

    python scripts/build_run.py --fixture
    python scripts/build_run.py --run <run_id> --user admin --password ...
    python scripts/build_run.py --loop --run <run_id> --user admin --password ...

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

**This does not weaken D-05.** That decision says no compiling or subprocess execution *on the
API host*. This is a developer-run script — the same tier as the customer's CI, which is
exactly the path D-05 named — and the backend still only ever receives diagnostics. Nothing
here imports the FastAPI app or runs inside it.

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
         project_id: str = "", max_rounds: int = 5, deadline_hours: float = 3.0) -> int:
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
        if not result.get("reopened"):
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
    parser.add_argument("--max-rounds", type=int, default=5,
                        help="Rounds the loop may use (default 5, matching MAX_BUILD_ROUNDS).")
    parser.add_argument("--hours", type=float, default=3.0,
                        help="Give up after this many hours (default 3).")
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
                    max_rounds=args.max_rounds, deadline_hours=args.hours)
    return build_export(api, args.key, args.run, args.keep, token, args.project)


if __name__ == "__main__":
    sys.exit(main())
