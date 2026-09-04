"""Entry point — run with: python run.py

Two things happen before uvicorn starts, and both exist because of a real morning lost to
this on 2026-09-02.

**The port is checked on every address, not just the one we bind.** `host="0.0.0.0"` and
`127.0.0.1` are *different addresses* to Windows, so a server already listening on
`127.0.0.1:8000` does not stop a new one binding `0.0.0.0:8000` — both listen, and neither
complains. A browser then resolves `localhost` to `127.0.0.1` and talks to whichever process
got there first, which may be one started days earlier holding entirely stale code. The
symptom is the worst kind: the restart looks clean, the log says "Application startup
complete", and the app still serves the old build. The check below refuses to start in that
state and names the process to kill.

**Reload can be turned off without remembering an incantation.** `RELOAD=0 python run.py`.
The reloader has its own history of going quiet (C-65 in DECISIONS.md), and the previous way
round it was to run `uvicorn backend.main:app` by hand — which silently changes the bind
address from `0.0.0.0` to `127.0.0.1` and so walks straight into the trap above.
"""
import os
import socket
import subprocess
import sys

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
RELOAD = os.getenv("RELOAD", "1").lower() not in ("0", "false", "no")

# Every address a client could reach this port on. `0.0.0.0` is checked as `127.0.0.1`
# because that is what `localhost` resolves to and therefore what a browser actually hits.
def _addresses_to_check(host: str):
    return ["127.0.0.1"] if host in ("0.0.0.0", "::", "") else [host]


def _in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def _owner_pids(port: int):
    """PIDs listening on `port`, best effort. Only used to make the message actionable."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            if parts[4] not in pids:
                pids.append(parts[4])
    return pids


def _describe(pid: str) -> str:
    """The command line and start time of a PID, so the reader can tell whose it is."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
             f"if ($p) {{ \"$($p.CreationDate)  $($p.CommandLine)\" }}"],
            capture_output=True, text=True, timeout=15).stdout.strip()
        return out[:200] or "(could not read the command line)"
    except Exception:
        return "(could not read the command line)"


def preflight() -> None:
    for address in _addresses_to_check(HOST):
        if not _in_use(address, PORT):
            continue
        pids = _owner_pids(PORT)
        print(f"\nPort {PORT} is already serving on {address}.", file=sys.stderr)
        print(
            f"\nStarting anyway would bind {HOST}:{PORT} successfully -- Windows treats "
            f"{HOST} and {address} as different addresses -- and your browser would keep "
            f"talking to the process below, which may be running older code.\n",
            file=sys.stderr,
        )
        for pid in pids:
            print(f"  PID {pid}  {_describe(pid)}", file=sys.stderr)
        if pids:
            print(f"\nStop it with:  taskkill /F /PID {' /PID '.join(pids)}", file=sys.stderr)
        else:
            print("\nNo listening PID was found, which usually means the socket is in "
                  "TIME_WAIT -- wait a few seconds and try again.", file=sys.stderr)
        print(f"\nOr run this one elsewhere:  PORT=8001 python run.py\n", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    import uvicorn

    preflight()
    print(f"Starting on {HOST}:{PORT}"
          + (" with reload" if RELOAD else " with reload OFF (RELOAD=0)"))
    uvicorn.run(
        "backend.main:app",
        host=HOST,
        port=PORT,
        reload=RELOAD,
        reload_dirs=["backend"] if RELOAD else None,
    )
