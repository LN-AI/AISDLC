# n8n Integration — LM AI-SDLC

These are importable [n8n](https://n8n.io) workflow templates that call the LM AI-SDLC external API
(`/api/v1/agents/*`, `/api/v1/swarm`) so a team can wire governance checks into their own process
without writing any code. n8n is optional — the API works the same from a `curl` command, a GitHub
Actions step, or any HTTP client. Use these templates when you want an event (a PR, a Jira transition,
a schedule) to fan out to multiple downstream systems (Slack, GitHub status checks, Jira comments)
without maintaining custom scripts for that routing.

All three use only n8n's built-in **Webhook / Cron / Set / If / Function / HTTP Request** nodes — no
community nodes required, so they'll import into any n8n instance (self-hosted or cloud).

## Prerequisites

1. A running LM AI-SDLC instance reachable from your n8n instance (a real URL, not `localhost`, if n8n
   is hosted elsewhere).
2. An API key: **Dashboard → API Keys → Create API Key** (or `POST /api/v1/keys` as an admin).
3. An n8n instance (self-hosted or [n8n.cloud](https://n8n.io)).

## Import

**Workflow menu → Import from File** (or **Import from URL** if hosting this repo's raw files) → select
the `.json` file. Each workflow has one **Config** node near the start — open it and replace the
placeholder values before activating the workflow.

---

## 1. `pr-gate.json` — PR Governance Gate

Runs the Governance Swarm on every PR open/update, sets a GitHub commit status, and posts a Slack alert
when the gate is `RED` or `BLOCKED`.

**Flow:** GitHub PR webhook → Config → filter to `opened`/`synchronize`/`reopened` → `POST /api/v1/swarm`
→ branch on `swarm_gate` → set GitHub commit status (success/failure) → Slack alert on failure.

**Setup:**
1. Fill in the **Config** node: `lm_sdlc_base_url`, `lm_sdlc_api_key`, `policy_name`
   (`standard`/`hipaa`/`pci`/`ai-product` — see `GET /api/v1/swarm/policies`), `github_token` (a token
   with `repo:status` scope), `slack_webhook_url` (a Slack [Incoming Webhook](https://api.slack.com/messaging/webhooks) URL).
2. Activate the workflow — n8n shows you the generated webhook URL.
3. In each repo you want gated: **Settings → Webhooks → Add webhook**, paste the n8n URL, content type
   `application/json`, events: **Pull requests**.
4. Optional: mark `lm-ai-sdlc/governance` as a **required status check** on your branch protection rule
   so a RED/BLOCKED gate actually blocks the merge button.

---

## 2. `nightly-compliance-sweep.json` — Nightly Compliance Sweep

Runs Compliance + Sensitive Data agents against a fixed list of repos every night and posts a Slack
summary — for a standing compliance watch that isn't tied to any single PR.

**Flow:** Cron (2am daily) → Config → Repo List (edit this node) → Compliance Scan + Sensitive Data Scan
in parallel → summarize → post to Slack.

**Setup:**
1. Fill in the **Config** node: `lm_sdlc_base_url`, `lm_sdlc_api_key`, `slack_webhook_url`.
2. Edit the **Repo List** node's code — replace the two example entries with your own
   `{ project_name, github_repo_url }` pairs.
3. Adjust the **Nightly Trigger** node's time if 2am doesn't suit your timezone.

**Note:** Sensitive Data Scan results are intentionally *not* posted to Slack — they may contain
obfuscated PII/credential samples, which shouldn't land in a chat channel. Review those via the
dashboard's Review History, or wire your own alerting onto that branch if you need it.

---

## 3. `jira-requirement-gate.json` — Jira Requirement Gate

Reviews a ticket's requirement text whenever your Jira Automation rule calls this webhook, and comments
back on the ticket when the gate isn't Green.

**Flow:** Jira webhook → Config → `POST /api/v1/agents/requirement` (using the ticket key) → if gate ≠
Green → comment on the Jira issue.

**Setup:**
1. Fill in the **Config** node: `lm_sdlc_base_url`, `lm_sdlc_api_key`, `jira_base_url`.
2. In n8n, create an **HTTP Basic Auth** credential using your Jira account email as the username and a
   [Jira API token](https://id.atlassian.com/manage-profile/security/api-tokens) as the password. Open
   the **Comment on Jira Issue** node and select that credential.
3. Activate the workflow, copy the webhook URL.
4. In Jira: **Project Settings → Automation → Create rule** — trigger on the transition you care about
   (e.g. "moved to Ready for Dev"), action: **Send web request** to the n8n webhook URL, method POST,
   body: the issue payload (Jira's automation editor has a built-in "issue" variable for this).

---

## Notes for all three

- Every `HTTP Request` node authenticates with `X-API-Key` — the same header used by `curl` or CI. See
  the main [README](../../README.md#external-team-access-api-v1) for the full endpoint list.
- These templates use the platform's default LLM. To route a specific workflow through a
  [Bring Your Own LLM](../../README.md#external-team-access-api-v1) config instead, either set BYOL on
  the API key these workflows use (`PUT /api/v1/llm-config`), or add `llm_provider`/`llm_api_key`/etc.
  fields into the `jsonBody` expressions.
- Placeholder values (`REPLACE_ME`, `your-lm-sdlc-host.example.com`, etc.) must be filled in before
  activating — n8n will run the workflow with literal placeholder text otherwise.
