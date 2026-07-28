#!/usr/bin/env python3
"""LM AI-SDLC — run a Code Review from inside VS Code (via a Task).

Usage:
    python lm_sdlc_review.py --file <path>          Review a single file
    python lm_sdlc_review.py --diff [--staged]      Review your working (or staged) git diff
    python lm_sdlc_review.py --pr <github_pr_url>   Review a GitHub PR [--post-back to comment on it]

Config (env vars, or a .vscode/.env / .env file next to this script):
    LM_SDLC_BASE_URL   Your LM AI-SDLC instance, e.g. https://lm-sdlc.yourcompany.com
    LM_SDLC_API_KEY    Your API key — Dashboard -> API Keys -> Create API Key
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

SEVERITY_LABELS = {5: "CRITICAL", 4: "HIGH", 3: "MODERATE", 2: "MINOR", 1: "LOW"}
GATE_ICON = {"Green": "✅", "Amber": "⚠️", "Red": "\U0001F534"}


def _load_dotenv():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")):
        if not os.path.exists(candidate):
            continue
        with open(candidate, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(
            f"Error: {name} is not set.\n"
            f"Copy docs/vscode-integration/.env.example to .vscode/.env and fill it in.",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


def _post(base_url: str, api_key: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/agents/code",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"Request failed ({e.code}): {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Could not reach {base_url}: {e.reason}", file=sys.stderr)
        sys.exit(1)


def _print_report(result: dict, base_url: str) -> None:
    gate = result.get("gate_status", "Unknown")
    print()
    print(f"{GATE_ICON.get(gate, '')} Gate: {gate}    Risk score: {result.get('risk_score', 0):.0f}/100")
    if result.get("summary"):
        print(result["summary"])

    findings = sorted(result.get("findings", []), key=lambda f: f.get("severity", 0), reverse=True)
    if not findings:
        print("\nNo findings.")
    for f in findings:
        sev = SEVERITY_LABELS.get(f.get("severity", 1), "LOW")
        print(f"\n[{sev}] {f.get('control_id', '')} — {f.get('title', '')}")
        if f.get("description"):
            print(f"  {f['description']}")
        if f.get("recommendation"):
            print(f"  Fix: {f['recommendation']}")

    llm_used = result.get("llm_used") or {}
    if llm_used:
        print(f"\n(reviewed via {llm_used.get('provider', 'platform')} / {llm_used.get('model', '')})")

    if result.get("review_id"):
        print(f"\nFull report: {base_url.rstrip('/')}/api/reviews/{result['review_id']}")
    if result.get("post_back_status") not in (None, "not_requested"):
        print(f"Post-back to PR: {result['post_back_status']}")


def _fix_windows_console_encoding() -> None:
    # Legacy Windows consoles (cp1252/cp437) can't encode the emoji this script prints,
    # which would otherwise crash mid-report. VS Code's own terminal is UTF-8 already,
    # but `python` run outside it may not be.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _fix_windows_console_encoding()
    _load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", help="Review this specific file")
    parser.add_argument("--diff", action="store_true", help="Review the current git diff")
    parser.add_argument("--staged", action="store_true", help="With --diff, review staged changes only")
    parser.add_argument("--pr", help="Review a GitHub PR URL instead of local code")
    parser.add_argument("--post-back", action="store_true", help="With --pr, post the review as a PR comment")
    args = parser.parse_args()

    base_url = _env("LM_SDLC_BASE_URL")
    api_key = _env("LM_SDLC_API_KEY")
    project_name = os.path.basename(os.getcwd())

    if args.pr:
        payload = {"github_pr_url": args.pr, "post_back": args.post_back, "project_name": project_name}
    elif args.diff:
        cmd = ["git", "diff", "--cached"] if args.staged else ["git", "diff"]
        try:
            diff = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"git diff failed: {e}", file=sys.stderr)
            sys.exit(1)
        if not diff.strip():
            print("No changes to review — git diff is empty.")
            return
        payload = {"code": diff, "project_name": project_name, "context": "VS Code Task — working diff review"}
    elif args.file:
        try:
            with open(args.file, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except OSError as e:
            print(f"Could not read {args.file}: {e}", file=sys.stderr)
            sys.exit(1)
        payload = {
            "code": content,
            "language": os.path.splitext(args.file)[1].lstrip("."),
            "project_name": project_name,
            "context": f"VS Code Task — file: {args.file}",
        }
    else:
        parser.error("Supply one of --file, --diff, or --pr")
        return

    print(f"Reviewing via LM AI-SDLC ({base_url})…")
    result = _post(base_url, api_key, payload)
    _print_report(result, base_url)


if __name__ == "__main__":
    main()
