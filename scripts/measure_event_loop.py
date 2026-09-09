"""Measure C-31: does a long agent call block the whole server?

C-31 has been open since it was first measured during a UI demo — "while a 10-minute VAPT
ran, `GET /` did not even return" — and its status says the remaining ~30 legacy routes
want *measuring, not a blind edit*, because flipping them all changes concurrency
everywhere at once. This is that measurement.

**What it does.** Starts a real server whose model gateway is replaced by a stub that
sleeps, so a review takes a known number of seconds and costs nothing. Fires one review at
a route, and while it is in flight asks the server for `GET /` several times, timing each.
A route that holds the event loop cannot answer; a route that runs on the threadpool
answers immediately.

**Why the answer is not obvious from reading the code.** FastAPI runs an `async def`
handler *on the event loop* and a plain `def` handler *on a threadpool*. So a handler that
does blocking work is fine when declared `def` and blocks the entire process when declared
`async def` — the same body, opposite behaviour. D-228 already flipped the routes that scan
repositories for this reason; this says what the rest cost.

Offline, no API key, no spend:

    venv/Scripts/python.exe scripts/measure_event_loop.py
    venv/Scripts/python.exe scripts/measure_event_loop.py --sleep 8
"""
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import httpx  # noqa: E402

# One route per shape, so the report says which kind of handler each result belongs to.
# `declared` is read off the app at run time rather than hardcoded, because the whole point
# is to catch these drifting apart.
PROBES = [
    ("/api/review/requirement", {"content": "The system shall log every access to a patient record."}),
    ("/api/review/code", {"code": "q = 'SELECT * FROM users WHERE u=' + u"}),
    ("/api/review/prompt", {"system_prompt": "You are a helpful assistant.", "tools": []}),
    # C-83, and the reason it needed its own probe. The upload variants are *correctly*
    # `async def` — they have to `await file.read()` — so they could not be flipped to plain
    # `def` with the 23 in D-334, and `smoke_products.py` §19 cannot see them either: its
    # rule is "async but never awaits", and these do await. They then called the orchestrator
    # synchronously, so the block was merely deferred until after the read. A probe is the
    # only thing that tells the difference between "awaits something" and "does not block".
    ("/api/review/requirement/upload",
     {"__file__": ("requirement.txt",
                   b"The system shall log every access to a patient record.",
                   "text/plain")}),
]


def _boot_script(port: str, sleep_s: float) -> str:
    """A server whose gateway sleeps instead of calling a provider."""
    return (
        "import sys, time, uvicorn\n"
        f"sys.path.insert(0, r'{REPO}')\n"
        "from backend.services import llm_gateway\n"
        "from backend import config as _cfg\n"
        "def _slow(system_prompt, user_content, schema, override=None, max_tokens=None, **kw):\n"
        f"    time.sleep({sleep_s})\n"
        "    llm_gateway._record_usage('anthropic', _cfg.MODEL, 100, 50)\n"
        "    return dict(risk_score=10.0, gate_status='Green', summary='Stub.',\n"
        "                compliance_flags=[], recommendations=[], findings=[])\n"
        "llm_gateway.call_tool = _slow\n"
        "import backend.main as _m\n"
        "_m.llm_settings_service.is_llm_configured = lambda: True\n"
        f"uvicorn.run('backend.main:app', host='127.0.0.1', port={port}, log_level='warning')\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleep", type=float, default=6.0,
                    help="Seconds the stubbed model call takes. Long enough to be "
                         "unambiguous, short enough to run in a coffee break.")
    ap.add_argument("--port", type=int, default=8709)
    args = ap.parse_args()

    db = os.path.join(tempfile.gettempdir(), f"measure_loop_{uuid.uuid4().hex}.db")
    boot = os.path.join(tempfile.gettempdir(), f"measure_loop_{args.port}.py")
    io.open(boot, "w", encoding="utf-8").write(_boot_script(str(args.port), args.sleep))
    env = {**os.environ, "DATABASE_PATH": db, "PYTHONIOENCODING": "utf-8",
           "SECRET_KEY": "measure-loop-not-for-production",
           # The point of the measurement is the handler's concurrency, not the guard, so
           # the legacy routes are left open rather than authenticated per probe.
           "LEGACY_ROUTES_REQUIRE_AUTH": "false"}
    log = io.open(os.path.join(tempfile.gettempdir(), f"measure_loop_{args.port}.log"),
                  "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, boot], cwd=REPO, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{args.port}"
    try:
        for _ in range(80):
            try:
                if httpx.get(base + "/", timeout=2).status_code == 200:
                    break
            except Exception:
                time.sleep(0.25)
        else:
            print("The server never came up. See", log.name)
            return 1

        # A review needs a signed-in caller bound to a project, or the guard answers 403 in
        # a quarter of a second and nothing is being measured. The first version of this
        # script missed that and reported "C-31 does not reproduce" over three 403s — a
        # false all-clear from no data, which is worse than no measurement.
        token = httpx.post(base + "/api/auth/login",
                           json={"username": "admin", "password": "Admin@1234"},
                           timeout=10).json().get("token")
        if not token:
            print("Could not sign in — the measurement needs a real session.")
            return 1
        hdr = {"Authorization": f"Bearer {token}"}
        project = httpx.post(base + "/api/v1/projects", headers=hdr,
                             json={"name": f"loop-measure-{uuid.uuid4().hex[:6]}"},
                             timeout=10).json()["project"]
        hdr["X-Project-Id"] = project["id"]

        # How each probe's handler is declared, read off the running app's own source.
        import inspect
        import backend.main as main_module
        declared = {}
        for route in main_module.app.routes:
            path = getattr(route, "path", "")
            if path in [p for p, _ in PROBES]:
                fn = getattr(route, "endpoint", None)
                declared[path] = ("async def" if fn and inspect.iscoroutinefunction(fn)
                                  else "def")

        print(f"\nStubbed model call: {args.sleep:.1f}s. "
              f"A handler that holds the event loop cannot answer anything while it runs.\n")
        print(f"{'route':<32}{'declared':<12}{'review':<10}"
              f"{'GET / while busy':<20}{'verdict'}")
        print("-" * 92)

        rows = []
        for path, payload in PROBES:
            probe_times = []
            done = threading.Event()

            def hammer():
                # Wait for the review to be genuinely in flight before judging the server.
                time.sleep(min(1.0, args.sleep / 4))
                while not done.is_set():
                    t0 = time.perf_counter()
                    try:
                        httpx.get(base + "/", timeout=max(2.0, args.sleep + 3))
                        probe_times.append(time.perf_counter() - t0)
                    except Exception:
                        probe_times.append(float("inf"))
                    time.sleep(0.3)

            t = threading.Thread(target=hammer, daemon=True)
            t.start()
            started = time.perf_counter()
            filespec = payload.get("__file__") if isinstance(payload, dict) else None
            try:
                if filespec:
                    r = httpx.post(base + path, files={"file": filespec},
                                   data={"project_name": "loop-measure"}, headers=hdr,
                                   timeout=args.sleep + 60)
                else:
                    r = httpx.post(base + path, json=payload, headers=hdr,
                                   timeout=args.sleep + 60)
                status = r.status_code
            except Exception as exc:
                status = f"err {type(exc).__name__}"
            took = time.perf_counter() - started
            done.set()
            t.join(timeout=5)

            worst = max(probe_times) if probe_times else 0.0
            # Nothing can be concluded from a review that did not run. A 403 in a quarter
            # of a second, or a call that finished before the first probe went out, leaves
            # the question open — and saying "concurrent" there is a false all-clear.
            ran = took >= (args.sleep * 0.5) and len(probe_times) > 0
            if not ran:
                verdict = "INCONCLUSIVE"
            elif worst > (args.sleep * 0.5):
                verdict = "BLOCKS THE SERVER"
            else:
                verdict = "concurrent"
            rows.append({"route": path, "declared": declared.get(path, "?"),
                         "status": status, "review_seconds": round(took, 2),
                         "worst_probe_seconds": (None if worst == float("inf")
                                                 else round(worst, 3)),
                         "probes": len(probe_times), "verdict": verdict})
            print(f"{path:<32}{declared.get(path, '?'):<12}{str(status):<10}"
                  f"{(('%.2fs' % worst) if worst != float('inf') else 'timed out'):<20}"
                  f"{verdict}")

        blocking = [r for r in rows if r["verdict"] == "BLOCKS THE SERVER"]
        unknown = [r for r in rows if r["verdict"] == "INCONCLUSIVE"]
        print("\n" + "=" * 92)
        if blocking:
            print(f"{len(blocking)} of {len(rows)} probed routes hold the event loop for the "
                  f"whole call.\nWhile one runs, this deployment answers nothing else — "
                  f"including the static page.\nThe fix for each is removing `async` from "
                  f"the handler, which moves it to the threadpool (D-228).")
        if unknown:
            print(f"\n{len(unknown)} route(s) told us nothing: "
                  + ", ".join(f"{r['route']} (status {r['status']}, "
                              f"{r['review_seconds']}s, {r['probes']} probes)"
                              for r in unknown)
                  + "\nA review that returned before the stub could sleep was refused or "
                    "short-circuited. Fix that before reading anything into it — "
                    "'concurrent' over a 403 is a false all-clear.")
        if not blocking and not unknown:
            print("Every probed route ran concurrently with other requests. C-31 does not "
                  "reproduce on these routes.")
        print("=" * 92)
        print("\nMachine-readable:")
        print(json.dumps(rows, indent=2))
        return 0
    finally:
        proc.terminate()
        log.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
