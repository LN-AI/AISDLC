"""Drive a full app-testing session against several real applications, one after another.

Not a test and not part of the offline net: it spends real money, drives real browsers
against public demo sites, and its whole purpose is to produce evidence the corpus cannot.
Three eval cases are three *shapes* chosen to measure the agent (C-95), and they are all
ordinary semantic HTML served from a bare origin with labelled controls -- which is exactly
why every defect found on 2026-09-18 was found by a live run and not by them (C-97): a
sanitiser dropping the one locator shape a real login needs, an entry URL with a path in it,
a `<select>` named from its own options, a suite claiming journeys it never walked.

Each application is here because it stresses something different:

  saucedemo   e-commerce, a catalogue of cards -- the shape where a price has to belong to
              a product, and the one `APP-TESTING-FINDINGS-2026-09-17.md` §1 measured at
              five prices and no names.
  parabank    banking, a table of accounts -- balances that have to belong to an account,
              login fields named only by their `name` attribute, and the session that once
              failed 16 of 16 on its sign-in helper.
  orangehrm   an HR portal on a modern framework -- a large menu, which is where C-107 says
              the candidate budget goes entirely to chrome, and a target market this product
              is aimed at and has never been pointed at.
  shoelace    component documentation built from custom elements -- open shadow roots
              (D-436) and the biggest navigation of the four.

Reports per application and then together, because the interesting numbers are the ones
that differ across shapes rather than any single run.

    venv/Scripts/python.exe scripts/run_live_apps.py --api http://127.0.0.1:8000 \
        --user admin --project <id> --into .live-verify
    venv/Scripts/python.exe scripts/run_live_apps.py --only parabank --max-steps 24
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `app_user`/`app_password` are the demo accounts these sites publish on their own front
# pages. Shoelace has no sign-in at all, which also makes it the only one where a session
# starts with the whole application already in front of it.
APPS = [
    {"key": "saucedemo",
     "url": "https://www.saucedemo.com/",
     "requirement": "req-saucedemo.txt",
     "app_user": "standard_user", "app_password": "secret_sauce",
     "why": "a catalogue of cards: a price has to belong to a product"},
    {"key": "parabank",
     "url": "https://parabank.parasoft.com/parabank/index.htm",
     "requirement": "parabank-requirement.txt",
     "app_user": "john", "app_password": "demo",
     "why": "a table of accounts, and login fields with no accessible name"},
    {"key": "orangehrm",
     "url": "https://opensource-demo.orangehrmlive.com/web/index.php/auth/login",
     "requirement": "req-orangehrm.txt",
     "app_user": "Admin", "app_password": "admin123",
     "why": "an HR portal on a modern framework, with a large menu"},
    {"key": "shoelace",
     "url": "https://shoelace.style/",
     "requirement": "req-shoelace.txt",
     "app_user": "", "app_password": "",
     "why": "custom elements and open shadow roots, with the biggest navigation"},
]


def run_one(app: dict, args) -> dict:
    """One session, verified against the application it was written from."""
    into = os.path.join(args.into, "suite-" + app["key"])
    # A fresh directory per application, so one suite cannot be read as another's. Node
    # resolves `node_modules` up the tree, so the runner installed once in `--into` serves
    # every subdirectory beneath it.
    if os.path.isdir(os.path.join(into, "tests")):
        shutil.rmtree(os.path.join(into, "tests"), ignore_errors=True)
    os.makedirs(into, exist_ok=True)

    argv = [sys.executable, os.path.join(ROOT, "scripts", "explore_app.py"),
            "--url", app["url"],
            "--api", args.api, "--user", args.user,
            "--project", args.project,
            "--requirement-file", os.path.join(args.into, app["requirement"]),
            "--max-steps", str(args.max_steps),
            "--attest", "--verify", "--verify-into", into]
    if app["app_user"]:
        argv += ["--app-user", app["app_user"], "--app-password", app["app_password"]]

    env = dict(os.environ)
    env["LM_PASSWORD"] = args.password
    env["PYTHONIOENCODING"] = "utf-8"

    started = time.time()
    print(f"\n{'=' * 78}\n{app['key'].upper()}  —  {app['why']}\n{app['url']}\n{'=' * 78}",
          flush=True)
    proc = subprocess.run(argv, env=env, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=args.timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    log = os.path.join(into, "session.log")
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(out)
    print(out[-args.tail:] if len(out) > args.tail else out, flush=True)
    return {"app": app["key"], "url": app["url"], "exit": proc.returncode,
            "seconds": round(time.time() - started, 1), "log": log,
            "review_id": _grep(out, "review id"), "session_id": _grep(out, "session")}


def _grep(text: str, label: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith(label):
            parts = line.split()
            return parts[-1] if parts else ""
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.environ.get("LM_PASSWORD", ""))
    ap.add_argument("--project", required=True)
    ap.add_argument("--into", default=".live-verify")
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=2400)
    ap.add_argument("--tail", type=int, default=6000)
    ap.add_argument("--only", default="", help="comma-separated keys, e.g. parabank,shoelace")
    args = ap.parse_args()

    chosen = [a for a in APPS
              if not args.only or a["key"] in {k.strip() for k in args.only.split(",")}]
    if not chosen:
        print(f"No application matched --only {args.only!r}. "
              f"Known: {', '.join(a['key'] for a in APPS)}")
        return 2

    results = []
    for app in chosen:
        try:
            results.append(run_one(app, args))
        except subprocess.TimeoutExpired:
            print(f"  {app['key']}: timed out after {args.timeout}s", flush=True)
            results.append({"app": app["key"], "exit": 124, "seconds": args.timeout})
        except Exception as exc:                                  # noqa: BLE001
            print(f"  {app['key']}: {type(exc).__name__}: {exc}", flush=True)
            results.append({"app": app["key"], "exit": 1, "error": str(exc)[:200]})

    print(f"\n{'=' * 78}\nSESSIONS\n{'=' * 78}")
    for r in results:
        print(f"  {r['app']:<12} exit={r.get('exit')}  {r.get('seconds', 0):>6}s  "
              f"review={r.get('review_id') or '(none)'}")
    path = os.path.join(args.into, "live-runs.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nWritten to {path}")
    print("Each session's full output is in its own suite directory as session.log.")
    # Never non-zero on a failing *suite*: a red suite is a result, and the sessions that
    # produced one cost money whatever it said. Only an unusable run is a failure here.
    return 0


if __name__ == "__main__":
    sys.exit(main())
