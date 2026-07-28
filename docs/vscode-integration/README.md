# VS Code Integration — LM AI-SDLC Code Review

Run the Code Review agent on the file you're editing, your working diff, or a GitHub PR — without
leaving VS Code. This is a small script + a `tasks.json` you copy into your repo, not a marketplace
extension, so there's nothing to install beyond Python 3.

## Prerequisites

- Python 3.8+ on your PATH (`python` on Windows, `python3` on macOS/Linux).
- `git` on your PATH (only needed for the diff tasks).
- An LM AI-SDLC API key — **Dashboard → API Keys → Create API Key**.

## Setup (per repo, one time)

1. Copy these three files into your repo's `.vscode/` folder (create it if it doesn't exist):
   - `lm_sdlc_review.py`
   - `tasks.json` — if you already have a `.vscode/tasks.json`, merge the `tasks` and `inputs` arrays
     instead of overwriting.
   - `.env.example` → rename to `.env` and fill in `LM_SDLC_BASE_URL` and `LM_SDLC_API_KEY`.
2. Add `.vscode/.env` to your `.gitignore` — it holds your API key and must never be committed.

Your repo should now have:
```
your-repo/
  .vscode/
    tasks.json
    lm_sdlc_review.py
    .env          ← your real values, gitignored
```

## Running it

**Command Palette → Tasks: Run Task** → pick one:

| Task | What it reviews |
|---|---|
| `LM AI-SDLC: Review Current File` | The file you currently have open |
| `LM AI-SDLC: Review Working Diff` | `git diff` — your uncommitted changes |
| `LM AI-SDLC: Review Staged Diff` | `git diff --cached` — what you're about to commit |
| `LM AI-SDLC: Review GitHub PR` | Prompts for a PR URL, reviews it (fetched server-side via GitHub) |

The report prints in a dedicated VS Code terminal panel: gate status, risk score, every finding with
its control ID and fix, and a link to the full stored report.

### Optional: bind a keyboard shortcut

Add to your **Keyboard Shortcuts (JSON)** (`Ctrl+Shift+P` → "Preferences: Open Keyboard Shortcuts (JSON)"):

```json
{
  "key": "ctrl+alt+r",
  "command": "workbench.action.tasks.runTask",
  "args": "LM AI-SDLC: Review Current File"
}
```

### Optional: run before every commit

Add a call to `python .vscode/lm_sdlc_review.py --diff --staged` inside a [pre-commit](https://pre-commit.com)
hook or a simple `.git/hooks/pre-commit` script if you want this to run automatically rather than on demand.

## Notes

- This calls the same `POST /api/v1/agents/code` endpoint documented in the main
  [README](../../README.md#external-team-access-api-v1) — everything that applies there (rate limits,
  Bring Your Own LLM) applies here too. To route your VS Code reviews through your own LLM, set BYOL on
  the API key you put in `.env` (`PUT /api/v1/llm-config`), no script changes needed.
- The GitHub PR task posts back to the PR only if you pass `--post-back` — edit `tasks.json`'s
  `LM AI-SDLC: Review GitHub PR` task to add `"--post-back"` to its `args` if you want that by default.
- For a heavier, native-feeling integration (inline squiggles, a sidebar report view, Problems-panel
  entries) a real published VS Code extension would be the next step up from this — ask if you want
  that scoped out.
