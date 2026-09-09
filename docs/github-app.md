# Getting findings onto the lines they are about

By default a pull-request review gives you two things: a summary comment, and a **commit
status** — a coloured dot with a description, which branch protection can require to block
a merge. What it cannot give you is an annotation on line 47 of `auth.py`, inside the diff,
where the person reviewing the code is already looking.

That is not a missing feature here. GitHub refuses `POST /repos/{owner}/{repo}/check-runs`
for a personal access token, whatever its scopes. **A check run needs a GitHub App.** There
is no way around it.

## Do you actually need one?

Probably not, at first. Two cheaper things get you most of the way:

* **The commit status already blocks merges.** Add "LM AI-SDLC" as a required status check
  in your branch protection rules and a Red gate greys out the merge button. This works
  with the token you already have.
* **`--sarif` puts findings in the Security tab.** `scripts/lmsdlc.py review --pr <url>
  --sarif out.sarif` plus `github/codeql-action/upload-sarif` gets every finding into
  GitHub's own code-scanning UI, with fingerprints so an alert survives code moving down a
  file. `docs/ci/github-actions.yml` is a ready workflow. This also needs no App.

An App buys you **placement**: the finding appears on the line, in the diff, in the review
UI, without anyone navigating anywhere. If your reviewers work in the Files Changed tab,
that is the difference between a tool they use and a tool they check.

## What to do

**1. Create the App.** Organisation settings → Developer settings → GitHub Apps → New
GitHub App. (A personal App works for personal repositories; an organisation App is what
you want for a team.)

- **Name** — anything. It appears as the check's author, so make it recognisable.
- **Homepage URL** — anything; GitHub requires the field.
- **Webhook** — *uncheck "Active"*. This platform's webhook is configured separately, per
  credential, and does not go through the App (see `/api/github/webhook/setup`).

**2. Set exactly these permissions.** Under "Repository permissions":

| Permission | Access | Why |
|---|---|---|
| Checks | **Read and write** | Create the check run. This is the whole point. |
| Pull requests | **Read and write** | Post and update the summary comment. |
| Contents | **Read** | Read `.lmignore`, and re-read a file at the commit a review recorded. |
| Metadata | Read | GitHub sets this and will not let you unset it. |

Nothing else. If you are also using Apply Fixes to open pull requests, Contents needs
**Read and write** as well — that is the one permission that lets this platform change your
code, so grant it deliberately rather than by default.

**3. Generate a private key.** At the bottom of the App's settings page. GitHub downloads a
`.pem` once and cannot show it again.

**4. Install it.** "Install App" in the left sidebar → choose the organisation → all
repositories, or a chosen few. **An App that exists is not an App with access**: this step
is the one people miss, and its symptom is a 404 when a review tries to create the check
run.

**5. Configure this deployment.** Three environment variables:

```
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=/etc/lm-sdlc/app-private-key.pem
```

or, if you would rather hold the key in the environment:

```
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
```

Literal `\n` in the variable is fine and expected — most deployment tools flatten a PEM
that way, and it is un-flattened when read.

**6. Check it.** `GET /api/github/app/status` says whether both variables are set and
whether the key parses, without making a network call and without returning anything
secret. A configured deployment that still cannot create a check run will say so in the
log, per repository, with the reason.

## What happens when it is set up

A pull-request review sets the commit status **and** creates a check run. Both, always —
not one instead of the other. If your branch protection already requires the status by
name, quietly replacing it with a differently-named check would stop protecting the branch
while appearing to work.

* **Conclusion** — Red is `failure` (blocks a merge under branch protection), Green is
  `success`, Amber is `neutral`. Amber deliberately does not block: whether an Amber gate
  should stop a merge is a policy question this platform has not answered for you, and
  choosing `action_required` here would answer it silently for everybody.
* **Annotations** — Critical and High are `failure`, Moderate is `warning`, the rest are
  `notice`. A diff with forty orange warnings is a diff nobody reads.
* **What is not annotated** — a finding with no file, no line, or whose quoted code was not
  found in the file it names. Its line number would be a guess, and an annotation on the
  wrong line sends a reviewer to code that is fine and teaches them to distrust the tool.
  Those findings stay in the summary and in the comment, and the check run says how many
  there were.
* **The 50-annotation cap** is GitHub's. More can be added by updating the run, which this
  deliberately does not do — the fiftieth annotation on a pull request is not read, and the
  count of what was omitted is more useful than the rows.

## If it does not work

| Symptom | Cause |
|---|---|
| `404` creating the check run | The App is not installed on that repository. Step 4. |
| `403` creating the check run | The App lacks **Checks: Read and write**. Step 2. |
| `401` naming the installation | The App id and the private key belong to different Apps. |
| "could not be read… starting with `-----BEGIN`" | The PEM's newlines were lost. Use the `_PATH` variable instead. |
| No check run, no error | No App configured. `GET /api/github/app/status`. |

Every one of these degrades to the commit status rather than losing the review: by the time
a check run is attempted, the review is stored and paid for, so a failure here costs
richness and never the report.

## What has not been verified

Honestly: **the two network calls have never run against GitHub from this codebase.**
Minting an installation token and creating a check run are both exercised offline against a
stub that records what it was asked for — the JWT's structure and claims, the annotation
mapping, the conclusion mapping, the cap, the fallback when no App is configured. The
requests themselves have not been made.

`scripts/verify_pr_review.py` is the tool that would prove it; it writes to a real
repository and spends real money, and it has not been run with an App configured. If you
set one up, run it on a throwaway pull request before relying on this in front of a team.
`DECISIONS.md` D-314 and D-315 record two GitHub write paths that were broken from the day
they shipped precisely because no offline test could reach them, which is why this section
exists rather than a claim that it works.
