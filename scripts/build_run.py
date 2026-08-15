"""Build a converted codebase and report the compiler's verdict back to the platform.

    python scripts/build_run.py --fixture
    python scripts/build_run.py --run <run_id> --api http://localhost:8000 --key lmsdlc_xxx

This is the shortest path from "the run finished" to "does it actually compile", and it needs
no GitHub, no Actions secret and no Docker — only the .NET SDK already on the machine. It
downloads the run's export, unzips it, runs `dotnet build`, and POSTs the captured log to
`POST /api/v1/modernization/runs/{id}/build-results`, where the failing units are reopened and
re-converted with the compiler's own errors in the prompt.

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


def _http(url: str, key: str, method: str = "GET", payload: dict = None) -> tuple:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("X-API-Key", key)
    if body:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def build_export(api: str, key: str, run_id: str, keep: str = "") -> int:
    status, blob = _http(f"{api}/api/v1/modernization/runs/{run_id}/export", key)
    if status != 200:
        print(f"Could not download the export: HTTP {status}\n{blob[:400].decode(errors='replace')}")
        return 2

    workdir = keep or tempfile.mkdtemp(prefix="lm-build-")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        # `extractall` is safe here only because these paths came from our own export, which
        # `repo_ingest.safe_relpath` already vetted. Re-checking costs nothing and this is the
        # one place a bad path would land on a developer's disk.
        for member in archive.namelist():
            if member.startswith(("/", "\\")) or ".." in member.replace("\\", "/").split("/"):
                print(f"Refusing to extract {member!r}")
                return 2
        archive.extractall(workdir)
    print(f"Extracted {len(archive.namelist())} file(s) to {workdir}")

    if not _dotnet_available():
        print("No `dotnet` on PATH — install the .NET SDK to build.")
        return 3
    code, log = _run_build(workdir)
    print(log[-3000:])

    payload = {"log": log, "source": "local_dotnet", "command": "dotnet build"}
    if code == 0:
        # The positive claim. An empty parse of a clean log is indistinguishable from a log
        # nothing could be read out of, and the platform refuses the latter.
        payload["diagnostics"] = []
    status, response = _http(
        f"{api}/api/v1/modernization/runs/{run_id}/build-results", key, "POST", payload)
    if status != 200:
        print(f"Reporting failed: HTTP {status}\n{response[:400].decode(errors='replace')}")
        return 4
    result = json.loads(response)
    print(f"\nReported round {result.get('round')}: {result.get('summary')}")
    if result.get("reopened"):
        print(f"{result['reopened']} unit(s) reopened — resume the run to re-convert them.")
    return 0 if code == 0 else 1


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
    parser.add_argument("--keep", default="", help="Extract to this directory instead of a "
                                                   "temp one, and leave it in place.")
    args = parser.parse_args()

    if args.fixture:
        return build_fixture()
    if not args.run:
        parser.error("one of --fixture or --run is required")
    if not args.key:
        parser.error("--key or LM_API_KEY is required to reach the platform")
    return build_export(args.api.rstrip("/"), args.key, args.run, args.keep)


if __name__ == "__main__":
    sys.exit(main())
