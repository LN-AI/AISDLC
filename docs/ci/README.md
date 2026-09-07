# Running a review in CI

`scripts/lmsdlc.py` is the client. It exists because the two things a pipeline needs were
missing: **an exit code to gate a merge on**, and **an output another tool can read**.
The n8n templates and the VS Code `tasks.json` are examples of writing the `curl` by hand;
they give you neither.

```bash
export LM_SDLC_URL=https://sdlc.example.com
export LM_SDLC_API_KEY=lmsdlc_...          # API Keys page. Carries the project, plan and budget.

python scripts/lmsdlc.py review --pr "$PR_URL" --fail-on red
```

Three flags do most of the work:

| Flag | Why it matters |
|---|---|
| `--fail-on red` | Exits 1 on a Red gate, so the review **is** the build step rather than a report beside it. `--fail-on amber` is stricter. |
| `--sarif out.sarif` | Writes the format GitHub code scanning, GitLab and most security dashboards ingest. Findings then appear in the Security tab beside everything else — **no GitHub App required**. |
| `--quote-only` | Prices the run and calls no model. A pipeline can refuse to spend rather than discovering the bill afterwards. |

Only `httpx` is needed, which the server already depends on.

## What to point it at

```bash
lmsdlc review --pr https://github.com/acme/shop/pull/88        # a pull request diff
lmsdlc review --repo https://github.com/acme/shop --ref main   # a whole repository at a ref
lmsdlc review --repo https://github.com/acme/shop \
              --base-ref main --head-ref feat/x                # base...head, before a PR exists
lmsdlc review --diff - < changes.diff                          # a diff on stdin
lmsdlc review --files src/auth.py src/db.py                    # an explicit file list
```

`--files` paths are sent **repository-relative**, because a finding's path is what places it
in a file and on a line in a SARIF alert. Run from the repository root, or pass `--root`.

`--agent` selects `code` (default), `vapt` or `security`.

## GitHub Actions

[`github-actions.yml`](github-actions.yml) — copy into `.github/workflows/`. It reviews the
pull request, uploads SARIF so findings land in the Security tab, and fails the check on a
Red gate.

Two deliberate choices in it:

* **`--quote-only` runs first**, on its own step, and the run is skipped if the quote is
  over a ceiling you set. Without that, a pull request touching 800 files spends whatever
  it spends and you find out on the invoice.
* **`continue-on-error` on the review step, with the SARIF upload after it.** A Red gate
  should still publish its findings — failing the job *before* the upload means the
  developer is blocked and cannot see why, which is how a check gets switched off.

## GitLab CI

[`gitlab-ci.yml`](gitlab-ci.yml) — the same shape. GitLab has no native integration in this
platform, so SARIF is the interop path: GitLab reads it as a Code Quality / SAST report.

## Jenkins, and everything else

There is nothing GitHub-specific in the client. Set the two environment variables, run it,
check the exit code:

```groovy
sh 'python scripts/lmsdlc.py review --repo "$GIT_URL" --ref "$GIT_COMMIT" --fail-on red'
```

## What this does not do

Stated plainly, because a CI integration that overpromises gets removed after the first
surprise:

* **No inline annotations from the CLI.** SARIF puts findings in the Security tab, not
  next to the lines in Files-changed. Line annotations need the GitHub Checks API, which
  GitHub refuses for a personal access token — so they come from a **server-side
  pull-request review with a GitHub App configured** (`docs/github-app.md`), not from this
  client. If you want annotations, point the platform's webhook at your repository and set
  up the App; if you want a build step with an exit code, this is it. The commit-status
  route (`--post-back`, server-side) greys out the merge button either way.
* **No incremental review from the CLI.** A re-run reviews whatever source you hand it. A
  *webhook* review of a push does review only the new commits (`INCREMENTAL_PR_REVIEW`),
  because it knows which commit it last looked at; a pipeline invocation carries no such
  memory, so on a long-lived branch each run costs the same. Use `--quote-only` with a
  ceiling if that matters — the workflows here do.
* **No caching between runs.** Each invocation is a fresh review. The findings *baseline*
  is per project, so a repeat run still reports "new" versus "pre-existing" correctly — but
  the model work is done again.
* **`--fail-on` reads the gate, not individual findings.** If you want "fail on any
  Critical regardless of the gate", read `--json` and decide yourself; the gate arithmetic
  already floors at Red for a Critical, so in practice these agree.
