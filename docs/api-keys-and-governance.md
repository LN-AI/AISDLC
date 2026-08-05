# API keys, logins, and who gets metered

Plain-language guide to the two ways of authenticating to this platform, why both exist, and
what would have to change to apply plans, limits and budgets to people using the web UI.

---

## There are two different credentials

**A login (username + password)** is for **a person using the web UI**. You sign in, the server
gives back a token (a JWT), and the browser attaches it to every request. It expires. It belongs to
a user account with a role like super admin or project admin.

**An API key** (`lmsdlc_…`) is for **a machine**. A CI pipeline, a Jira automation, a VS Code
extension, another team's service. It is sent as an `X-API-Key` header, never expires until someone
revokes it, and belongs to a *project* rather than to a person.

Both work on the `/api/v1/*` endpoints. The server checks for an API key first, then a login token.

### Does the web UI use an API key?

**No.** The frontend never sends one — it only ever uses the login token. API keys exist purely for
callers that are not the UI. Creating a key does not change how the UI behaves.

---

## The important difference: only API keys are governed

This is the part that usually causes confusion. The two credentials are not just different ways of
proving who you are — they are treated completely differently once you are through the door.

| | API key | UI login |
|---|---|---|
| Rate limits (per minute / per day) | **Enforced** | Not applied |
| Plan entitlements (what you may call) | **Enforced** | Not applied — sees everything |
| Monthly budget cap | **Enforced** | Not applied |
| Per-product quotas | **Enforced** | Not applied |
| Bring your own LLM credentials | **Supported** | Not applicable |
| Recorded in the usage ledger | Yes, as `api_key` | Yes, as `ui` |

So the mental model is:

> **A login says who you are. An API key says what a system is allowed to spend.**

Everything in the product/plan layer — entitlements, quotas, budgets, bring-your-own-LLM — hangs off
the **key**, not off the person. A key is the unit you meter and, eventually, bill.

The reason is mechanical: when a request arrives with an API key, the server loads that key's
database row and carries it along. Every limit is read from that row. A login token has no such row,
so there is nothing to read a limit from, and the checks are skipped.

---

## How the pieces fit together

| Thing | Question it answers | Attached to |
|---|---|---|
| **Project** | Whose work is this? | The API key |
| **Plan** | What may they run, and how much? | The API key |
| **Usage ledger** | What did it actually cost? | Every call |

The API key is the hinge — it is the only object carrying both a project and a plan.

What happens on every API-key call, in order:

1. **Rate limit** — too many requests this minute or today? → `429`
2. **Entitlement** — is this product in the key's plan? → `403` if not
3. **Budget** — has the key spent its monthly cap? → `402` if so
4. **Quota** — has this specific product hit its monthly limit? → `429` if so
5. Run the agent or workflow
6. **Meter** — write one row to the usage ledger with product, key, project and cost

Two deliberate fallbacks mean nothing breaks until you opt in:

- A key with **no plan** falls back to its old `scopes` column, which in practice means unrestricted.
- **Deleting a plan** drops its keys back to scopes rather than locking them out.

---

## Using an API key

Create one on the **API Keys** page (super admin only), choose the project, and copy the key — it is
shown once and never again.

### PowerShell

In Windows PowerShell, `curl` is *not* real curl — it is an alias for `Invoke-WebRequest`, which does
not understand `-H` or `-d`. Use one of these instead.

Real curl, which ships with Windows 10 and later — note the `.exe`:

```powershell
curl.exe -X POST "http://localhost:8000/api/v1/agents/code" `
  -H "X-API-Key: lmsdlc_YOUR_KEY" `
  -H "Content-Type: application/json" `
  -d '{\"code\":\"def f(): pass\",\"project_name\":\"My Project\"}'
```

Or the native PowerShell way, which is easier to read and gives you a parsed object back:

```powershell
$headers = @{ "X-API-Key" = "lmsdlc_YOUR_KEY" }
$body = @{ code = "def f(): pass"; project_name = "My Project" } | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/v1/agents/code" `
  -Headers $headers -ContentType "application/json" -Body $body
```

### bash / macOS / Linux / CI

```bash
curl -X POST http://localhost:8000/api/v1/agents/code \
  -H "X-API-Key: lmsdlc_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"code": "def f(): pass", "project_name": "My Project"}'
```

### Checking what a key is allowed to do

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/products" `
  -Headers @{ "X-API-Key" = "lmsdlc_YOUR_KEY" } |
  Select-Object -ExpandProperty products |
  Format-Table id, kind, entitled, quota_calls_per_month
```

Every product comes back with `entitled` plus this period's usage against its quota.

### Reading the response

Every response includes `X-RateLimit-Remaining-Minute` and `X-RateLimit-Remaining-Day`.

| Status | Meaning |
|---|---|
| `403` | The key's plan does not include this product |
| `402` | The key's monthly budget cap is spent |
| `429` | Either the rate limit or a per-product quota was hit — the message says which |

### Keep keys secret

A key is a bearer credential: anyone holding it can spend against it. Do not paste one into a chat,
a ticket, a screenshot, or a commit. If a key is exposed, revoke it from the API Keys page and issue
a new one — revoking takes effect immediately.

---

## The gap: browser use is not governed

Because a login token carries no key row, **everything done through the web UI bypasses every
limit**. No rate limit, no entitlement check, no budget, no quota. It is still recorded in the usage
ledger as `caller_type: ui`, so you can see the spend after the fact — but nothing prevents it.

That is fine while the UI is an internal admin console used by a handful of trusted staff. It stops
being fine the moment external customers get browser logins, because someone could run unlimited
spend through the UI while their API key sits at a $50 cap.

---

## What to change to govern browser use too

This is a design change, not a bug fix. The work below is roughly in dependency order.

### The obstacle to solve first

An API key belongs to exactly one project, which is what makes it easy to govern. A UI session does
not: the SPA sends **`project_name` as free text** in the request body, and there is no
"current project" bound to the session. So before any limit can be applied to a browser call, the
platform has to know *which* project or user to charge.

Two ways out:

- **Bind a project to the session.** Add a project switcher to the UI, store the chosen project, and
  send `project_id` on every agent call. More UI work, but it makes attribution exact and also fixes
  the fact that today's ledger cannot reliably attribute UI calls to a project.
- **Attach the plan to the user instead.** Simpler, no UI switcher needed, but a person working
  across several projects gets one shared allowance.

Binding a project is the better long-term answer, because plans, budgets and the usage ledger are
already project-shaped.

### 1. Give non-key callers something to read limits from

Today every limit lives on the `api_keys` row. Add the same three fields to whichever object becomes
the unit of governance — `projects` is the natural choice:

- `plan_id` — which plan applies
- `monthly_budget_usd` and `budget_used_usd` — the spend cap and running total
- `budget_period_start` — so the monthly reset works the same way

Follow the existing pattern in `backend/database.py`: new tables go in the `executescript` block, new
columns go in the ALTER-and-swallow loop.

### 2. Stop treating "no API key" as "no limits"

Several places currently short-circuit when there is no key record:

- `backend/services/agent_guard.py` — `preflight()` and `check_scope()` both return early when the
  caller has no key row. That early exit is what makes the UI unrestricted.
- `backend/services/entitlement_service.py` — `resolve_entitlements()` returns an unrestricted
  entitlement for login and anonymous callers.

Both need to resolve limits from the caller's project or user instead of giving up.

### 3. Make the caller carry its governance source

`get_caller()` in `backend/routes/v1_agents.py` returns a bare `{"type": "jwt", ...}` for logins.
It should also attach whatever the limits will be read from, so the guard does not have to know
where a caller came from.

The cleanest version of this change: make the guard depend on an abstract "billing subject" rather
than on the API key row specifically. Otherwise every limit check grows an if/else for caller type
and the two paths drift apart — which has already happened once in this codebase.

### 4. Apply rate limiting to logins

Rate limiting currently runs only in the API-key branch of `get_caller`. A per-user equivalent is
needed, or a UI user can hammer the platform freely.

### 5. Close the anonymous route

The legacy `/api/review/*` endpoints still accept unauthenticated callers by default, resolving them
to unrestricted. Governing the UI is pointless while an unauthenticated caller can hit the same
endpoints — so `LEGACY_ROUTES_REQUIRE_AUTH` must be set to `true`, and
`backend/services/legacy_guard.py` needs the same treatment as the guard above.

Set that flag only after confirming every UI screen sends its token, or screens will start failing
with 401.

### 6. Show people their limits

Once the UI is subject to limits, it has to explain them, or users just see failures:

- Show remaining quota and budget somewhere visible.
- Turn `402`, `403` and `429` into clear messages — "your plan does not include Code Review", not a
  raw error.
- Grey out or hide agents the current user is not entitled to.

### 7. Roll it out without breaking anything

Default every project and user to unrestricted, exactly as keys without a plan behave today. Then
opt in one project at a time. The moment this ships enabled-by-default, every existing user inherits
whatever limits happen to be configured — which is how a governance feature turns into an outage.

### What to verify

- A user on a restricted plan gets `403` in the UI for a product not in their plan, and a readable
  message rather than a stack trace.
- The same user still gets `200` for a product that is in it.
- A UI call and an API-key call for the same product and project both land in the usage ledger and
  both count against the same budget.
- Users with no plan are still unrestricted — the no-regression guarantee.
- With `LEGACY_ROUTES_REQUIRE_AUTH=true`, an unauthenticated call gets `401` and every UI screen
  still works.

`scripts/smoke_products.py` is the place for the backend checks; it already covers the equivalent
API-key paths and needs no test framework.
