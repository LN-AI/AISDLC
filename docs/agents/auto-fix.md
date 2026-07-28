# Auto-Fix & Open PR

Reviews a GitHub PR with the Code Review agent, generates corrected file contents for its findings, commits them to a new `lm-autofix/pr-N` branch off the PR's head branch, and opens a follow-up PR back into it. **This writes real commits to a real repository** — it requires a `GITHUB_TOKEN` with write (`repo`) scope, not just read access. Every generated patch is scanned for newly-introduced hardcoded secrets before being committed; any file that fails the scan is skipped and listed in `blocked_patches` instead of being written.

**Module:** Developer Intelligence
**Endpoint:** `POST /api/v1/agents/auto-fix`
**Agent name (for scopes/BYOL config):** `auto-fix`

## Authentication

Supply either:
- `X-API-Key: lmsdlc_<64 hex chars>` — for external/team access
- `Authorization: Bearer <jwt>` — for UI-authenticated calls

Requires the platform's `GITHUB_TOKEN` to have **write** (`repo`) scope — HTTP 503 if `GITHUB_TOKEN` is unset at all. Because this endpoint mutates a real repository, restrict its `scopes` grant on API keys to teams that should be able to trigger commits/PRs, not just read reviews.

## Request

This agent uses `LLMOverrideFields` directly — there is no generic source resolver.

| Field | Type | Required | Description |
|---|---|---|---|
| `pr_url` | string | **yes** | GitHub PR URL, e.g. `https://github.com/acme/patient-portal/pull/42` |
| `jira_ticket` | string | no | Jira ticket ID for audit-trail context only (not fetched or posted to) |
| `max_files` | integer | no (default `5`, 1–15) | Max number of changed files to attempt to patch |
| `llm_provider`, `llm_api_key`, `llm_base_url`, `llm_model` | — | no | Per-call BYOL override |

**Sources accepted:** `pr_url` only. Internally, the PR diff is reviewed by the Code Review agent first (see [`code.md`](code.md)); its `is_blocking` findings (or all findings, if none are blocking) become the list the Auto-Fix Agent attempts to remediate.

## Response

Custom envelope, fields returned at the top level:

| Field | Description |
|---|---|
| `pr_url`, `pr_number`, `pr_title` | The original PR |
| `fix_applied` | bool — `false` if nothing was fixed (see `reason`) |
| `reason` | Present when `fix_applied` is `false` — e.g. `"No findings to fix."`, `"Could not fetch content for any changed file (deleted/binary/renamed)."`, `"Auto-Fix Agent produced no patches for these findings."`, `"Every generated patch was blocked by the secret-scan guardrail — nothing committed."` |
| `fix_branch` | e.g. `lm-autofix/pr-42` — the new branch created off the PR's head branch |
| `fix_pr_url`, `fix_pr_number` | The new follow-up PR opened back into the original PR's head branch |
| `files_patched[]` | Each: commit metadata (path, sha, etc.) plus `explanation` (1–2 sentences on what changed and why) |
| `blocked_patches[]` | Each: `path`, `reason` (`"Blocked: patch appears to introduce a hardcoded secret."`), `matches[]` (redacted matched snippets) — patches that were generated but deliberately **not** committed |
| `review_result` | The full Code Review result that drove the fix (same shape as [`code.md`](code.md)'s response) |
| `_budget_stats` | **At the top level**, explicitly included by this handler |
| `llm_used`, `created_at` | Standard |

The underlying Auto-Fix Agent also fixes only what findings describe — it will not refactor unrelated code, and any finding it can't safely fix without more context is listed (with a reason) in the internal `fix_result.unfixed_finding_titles`, though that specific sub-field is not surfaced in the top-level response shape above; consult `review_result.findings` against `files_patched`/`blocked_patches` to determine what wasn't addressed.

## Guardrails applied

- **Input size limit** — HTTP 413 over `MAX_AGENT_INPUT_CHARS` (400,000 chars), applied to the PR diff during the Code Review step
- **Timeout & retry** — 120s timeout per LLM call (there are at least two: the Code Review call and the Auto-Fix generation call, raised to `max_tokens=8000`), up to 2 retries each
- **Prompt-injection scan** — non-blocking, applied during the Code Review sub-call
- **Untrusted content wrapping** — the PR diff is wrapped with `<<<UNTRUSTED_GITHUB_PR_DIFF_START/END>>>` markers before either the Code Review or Auto-Fix step analyzes it
- **Secret-scan commit guardrail (blocking, unique to this agent)** — every generated patch's `fixed_content` is scanned with `scan_for_hardcoded_secrets()` (AWS keys, private key blocks, generic `api_key=`/`secret=`/`token=`/`password=` assignments, GitHub PATs, OpenAI/Anthropic-style secret keys) before being committed. A match blocks that one file from being committed — it's listed in `blocked_patches` instead — while any other clean patches in the same batch are still committed normally

## Cost control

- **RBAC** — this key's `scopes` must include `auto-fix` or be `all`, else HTTP 403
- **Budget** — HTTP 402 once `monthly_budget_usd` is exhausted for the month (checked once before the Code Review + Auto-Fix pipeline starts)
- `_budget_stats` appears at the top level of the response, summed across both the Code Review and Auto-Fix LLM calls

## Example

```bash
curl -X POST https://your-host/api/v1/agents/auto-fix \
  -H "X-API-Key: lmsdlc_xxx" -H "Content-Type: application/json" \
  -d '{
    "pr_url": "https://github.com/acme/patient-portal/pull/42",
    "max_files": 5
  }'
```

Abbreviated response:

```json
{
  "success": true,
  "agent": "auto-fix",
  "pr_url": "https://github.com/acme/patient-portal/pull/42",
  "pr_number": 42,
  "fix_applied": true,
  "fix_branch": "lm-autofix/pr-42",
  "fix_pr_url": "https://github.com/acme/patient-portal/pull/43",
  "fix_pr_number": 43,
  "files_patched": [
    {"path": "app/api/users.py", "sha": "a1b2c3...", "explanation": "Parameterized the SQL query in get_user() to remove the injection vector."}
  ],
  "blocked_patches": [
    {"path": "app/config.py", "reason": "Blocked: patch appears to introduce a hardcoded secret.", "matches": ["sk-abc...wxyz"]}
  ],
  "_budget_stats": {"input_tokens": 4210, "output_tokens": 3100, "cost_usd": 0.087, "monthly_budget_usd": 100.0, "budget_used_usd": 22.4}
}
```

## Frameworks / controls referenced

`OWASP-Web-Top10` · `CWE` (via the underlying Code Review findings it remediates — see [`code.md`](code.md)). No separate control library of its own.
