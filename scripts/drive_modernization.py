"""Drive a modernization run end to end through the HTTP API, as the browser does.

    python scripts/drive_modernization.py --zip path/to/repo.zip --name "acme -> .NET 8"
    python scripts/drive_modernization.py --run <run_id>          # continue an existing run

Calls the same endpoints `frontend/app.js` calls, in the same order: upload, read the
deterministic charter suggestions, set the charter, approve the discovery gate, start, approve
the plan gate, poll. Nothing here is a test double — it spends real money against whichever
LLM connection is active.

Why a script rather than clicking
--------------------------------
A conversion takes tens of minutes and its interesting moments are two spend gates and a
handful of checkpoints. Watching those in a browser means being present for all of it; this
prints one line per state change and per run event, so the log afterwards *is* the record of
what the pipeline decided and when. It also makes a run reproducible: same zip, same charter,
same sequence.

It stops on any **blocking** checkpoint it was not told to approve, because a blocking
checkpoint is a decision the product has deliberately handed to a human — auto-answering those
would defeat the only mechanism that asks.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Gates that exist to quote a price. Approving them is what a human does after reading the
# number; everything else stops the driver.
SPEND_GATES = {"discovery_approval", "plan_approval"}


def feasible_repairs(point):
    """The layout repairs a plan gate is offering, best first, or [].

    `plan_approval` stopped being only a price quote when it started carrying
    `layering.repair_options`: deterministic ways to make an unbuildable layout buildable,
    each with what it measurably closes and opens, applied only if someone picks one. This
    driver approved every spend gate with an empty answer, which selects "none" — so an
    unattended run converted into a layout the product had already told it would not build,
    and then spent the attempt budget discovering that one component at a time.

    Ordered by what closes most and opens least, so `--repair best` means something defensible
    rather than "the first one".
    """
    repairs = [r for r in ((point.get("payload") or {}).get("repairs") or [])
               if r.get("feasible") and r.get("id")]
    return sorted(repairs, key=lambda r: (-len(r.get("closes") or []),
                                          len(r.get("opens") or []), r["id"]))


class Client:
    """Talks to the platform as whatever credential it was given.

    A bearer token or an API key, decided by the shape of the value: `get_caller` has always
    accepted either, and which one you happen to have depends entirely on how you got here.
    A password is what a person has; a key is what a CI job has and what somebody who has
    already provisioned one has. Requiring the password was the same friction that kept
    `build_run.py` unused until it learned to log in (D-102), pointing the other way.
    """

    def __init__(self, base, token, project_id):
        self.base, self.token, self.project_id = base.rstrip("/"), token, project_id

    def call(self, path, method="GET", body=None, timeout=180):
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
        )
        if self.token.startswith("lmsdlc_"):
            request.add_header("X-API-Key", self.token)
        else:
            request.add_header("Authorization", f"Bearer {self.token}")
        if self.project_id:
            request.add_header("X-Project-Id", self.project_id)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")

    def upload(self, zip_path, name, stack, cap):
        """Multipart by hand: the only call that is not JSON, and pulling in `requests` for
        one endpoint is not worth the dependency."""
        boundary = "----lmsdlcdriver"
        parts, blob = [], open(zip_path, "rb").read()
        fields = [("name", name), ("target_stack", stack)]
        if cap:
            fields.append(("budget_cap_usd", str(cap)))
        for key, value in fields:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n"
                         f"\r\n{value}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                     f"filename=\"{os.path.basename(zip_path)}\"\r\n"
                     f"Content-Type: application/zip\r\n\r\n".encode())
        parts.append(blob)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"{self.base}/api/v1/modernization/runs/upload", data=b"".join(parts), method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        request.add_header("Authorization", f"Bearer {self.token}")
        if self.project_id:
            request.add_header("X-Project-Id", self.project_id)
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))


def say(message, log):
    """Print a line, and never let printing be the thing that kills the run monitor.

    A Windows console is cp1252, and everything this prints is *someone else's text* — event
    details written by a model, file paths from a customer's repository. One `→` in a
    `manual_followup` raised `UnicodeEncodeError` here and took the driver down mid-conversion,
    which is the worst possible moment: the run is spending money, the server carries on
    perfectly well, and the only thing watching it has gone. The log file is already UTF-8; it
    is the terminal that cannot take it, so the terminal gets a lossy copy and the log keeps
    the real thing.
    """
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    if log:
        log.write(line + "\n")
        log.flush()
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(encoding, "replace").decode(encoding, "replace"), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default=os.getenv("DEFAULT_ADMIN_PASSWORD", "Admin@1234"))
    parser.add_argument("--token", default=os.getenv("LM_TOKEN", ""),
                        help="An API key or bearer token to use instead of logging in.")
    parser.add_argument("--project", default="", help="X-Project-Id. Required where the "
                                                      "deployment binds every call to a project.")
    parser.add_argument("--zip", help="Source repository to convert.")
    parser.add_argument("--run", help="Continue an existing run instead of creating one.")
    parser.add_argument("--name", default="driven conversion")
    parser.add_argument("--stack", default=".NET 8 / ASP.NET Core MVC with Razor views")
    parser.add_argument("--cap", type=float, default=None, help="budget_cap_usd.")
    parser.add_argument("--log", default="", help="Append the transcript to this file too.")
    parser.add_argument("--hours", type=float, default=3.0, help="Give up after this long.")
    parser.add_argument("--repair", default="stop",
                        help="What to do when the plan gate offers layout repairs: 'stop' "
                             "(default — print them and let a human choose), 'best' (take the "
                             "one that closes most and opens least), 'none' (convert into the "
                             "layout as designed), or a specific option id.")
    args = parser.parse_args()
    if not args.zip and not args.run:
        parser.error("one of --zip or --run is required")

    log = open(args.log, "a", encoding="utf-8") if args.log else None

    if args.token:
        client = Client(args.api, args.token, args.project)
    else:
        status, auth = Client(args.api, "", "").call(
            "/api/auth/login", "POST", {"username": args.user, "password": args.password})
        if status != 200:
            say(f"login failed ({status}): {json.dumps(auth)[:200]}", log)
            return 1
        client = Client(args.api, auth["token"], args.project)

    # If no project was named and the deployment needs one, take the first visible project
    # rather than failing with a 403 the caller has to go and decode.
    if not client.project_id:
        _s, projects = client.call("/api/v1/projects")
        rows = projects.get("projects") if isinstance(projects, dict) else projects
        if rows:
            client.project_id = rows[0]["id"]
            say(f"using project '{rows[0].get('name')}'", log)

    if args.run:
        run_id = args.run
    else:
        run = client.upload(args.zip, args.name, args.stack, args.cap)
        run_id = run["id"]
        stats = run.get("stats") or {}
        say(f"run {run_id} created — {stats.get('files_ingested')} file(s), "
            f"{stats.get('edges')} edge(s), {stats.get('levels')} level(s)", log)
        say(f"  evidence: {json.dumps(stats.get('evidence'))}", log)
        say(f"  external dependencies: {stats.get('external_dependencies')}", log)

    base = f"/api/v1/modernization/runs/{run_id}"

    if not (client.call(base)[1].get("charter")):
        # Deterministic suggestions, first option per field — what a user accepting the
        # defaults would send. Free: no model call happens behind this endpoint.
        _s, suggestions = client.call(f"{base}/charter-suggestions")
        charter = {field["key"]: field["options"][0]["value"]
                   for field in suggestions.get("fields", []) if field.get("options")}
        status, _ = client.call(f"{base}/charter", "POST", {"charter": charter})
        say(f"charter set ({status}) — {len(charter)} field(s)", log)

    last, seen_event = None, 0
    deadline = time.time() + args.hours * 3600
    while time.time() < deadline:
        run = client.call(base)[1]
        key = (run["status"], run["phase"], run.get("progress_pct"),
               round(run.get("spent_usd") or 0, 4))
        if key != last:
            say(f"{run['status']} / {run['phase']} — {run.get('progress_pct')}%, "
                f"${round(run.get('spent_usd') or 0, 4)}, "
                f"units={json.dumps(run.get('units'))}", log)
            last = key

        # Newest-first from the API, and the manifest carries a *count* of checkpoints rather
        # than the rows — both easy to get wrong from the field names alone.
        events = sorted(client.call(f"{base}/events?limit=400")[1].get("events") or [],
                        key=lambda e: e["id"])
        for event in events:
            if event["id"] > seen_event:
                say(f"    . {event.get('event')} [{event.get('level')}] "
                    f"{(event.get('detail') or '')[:240]}", log)
        if events:
            seen_event = max(seen_event, max(e["id"] for e in events))

        open_points = client.call(f"{base}/checkpoints?status=open")[1].get("checkpoints") or []
        for point in open_points:
            kind = point.get("kind")
            repairs = feasible_repairs(point) if kind == "plan_approval" else []
            if repairs and args.repair == "stop":
                # The driver's own rule, applied to what this gate has become: it stops on a
                # decision the product deliberately handed to a human. A layout repair is
                # exactly that — the architect may be right and the repair may be the wrong
                # trade — so it is not auto-picked by default. `--repair best` opts in.
                say(f"  !! PLAN GATE offers {len(repairs)} layout repair(s), and none is "
                    f"chosen by default", log)
                for repair in repairs:
                    say(f"     {repair['id']}: {repair.get('title')} — closes "
                        f"{len(repair.get('closes') or [])}, opens "
                        f"{len(repair.get('opens') or [])}", log)
                say("  re-run with --repair best, --repair none, or --repair <id>", log)
                return 2
            if kind in SPEND_GATES:
                # `discovery_approval` carries its numbers under `estimate` and
                # `plan_approval` under `totals`. Reading only the first printed
                # "expected $None, ceiling $None" on the larger of the two spend gates.
                _payload = point.get("payload") or {}
                quote = _payload.get("estimate") or _payload.get("totals") or {}
                say(f"  GATE {kind}: {point.get('title')} — expected "
                    f"${quote.get('expected_usd')}, ceiling ${quote.get('ceiling_usd')}", log)
                chosen = ""
                if repairs and args.repair == "best":
                    chosen = repairs[0]["id"]
                elif repairs and args.repair not in ("none", "stop"):
                    chosen = args.repair
                if chosen:
                    say(f"  applying layout repair {chosen}", log)
                client.call(f"/api/v1/modernization/checkpoints/{point['id']}/answer",
                            "POST", {"decision": "approve",
                                     "answer": {"repair": chosen} if chosen else {}})
            elif not point.get("blocking"):
                say(f"  noting {kind}: {point.get('title')}", log)
                client.call(f"/api/v1/modernization/checkpoints/{point['id']}/answer",
                            "POST", {"decision": "acknowledge", "answer": {}})
            else:
                say(f"  !! BLOCKING {kind}: {point.get('title')}", log)
                say(f"     {(point.get('detail') or '')[:900]}", log)
                say("  stopping — this is a decision the product asked a human for", log)
                return 2

        if run["status"] in ("completed", "failed", "cancelled"):
            say(f"terminal: {run['status']}"
                + (f" — {json.dumps(run.get('error'))[:400]}" if run.get("error") else ""), log)
            return 0

        # Approving a gate does not restart the loop: Approve and Start are two clicks in the
        # UI, and the run sits in `awaiting_approval` until the second one.
        if run["status"] in ("awaiting_approval", "paused") and not any(
                p.get("blocking") for p in open_points):
            verb = "resume" if run.get("job_id") else "start"
            status, body = client.call(f"{base}/{verb}", "POST", {})
            say(f"  {verb} -> {status} {json.dumps(body)[:200]}", log)
            if status >= 400:
                return 5
            time.sleep(3)
            continue
        time.sleep(6)

    say("gave up waiting", log)
    return 4


if __name__ == "__main__":
    sys.exit(main())
