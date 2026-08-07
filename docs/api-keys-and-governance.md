# API keys, logins, and who gets metered

Plain-language guide to the two ways of authenticating to this platform, why both exist, and how
plans, budgets and quotas apply the same way to both.

---

## There are two different credentials

**A login (username + password)** is for **a person using the web UI**. You sign in, the server
gives back a token (a JWT), and the browser attaches it to every request. It expires. It belongs to
a user account with a role like super admin or project admin.

**An API key** (`lmsdlc_…`) is for **a machine**. A CI pipeline, a Jira automation, a VS Code
extension, another team's service. It is sent as an `X-API-Key` header, never expires until someone
revokes it, and belongs to a *project*.

Both work on the `/api/v1/*` endpoints. The server checks for an API key first, then a login token.

### Does the web UI use an API key?

**No.** The frontend never sends one — it only ever uses the login token. API keys exist purely for
callers that are not the UI. Creating a key does not change how the UI behaves.

---

## The limit belongs to the project, not to the credential

This is the thing to hold on to. A plan, a budget and a quota are attached to a **project**. The
credential only decides *how you got in*; the project decides *what you may do and how much*.

```
  API key  ──┐
             ├──► PROJECT ──► plan · budget · quotas · usage
  UI login ──┘        (one allowance, whichever door)
```

An API key belongs to a project from birth. A browser session picks one from the **Working in**
dropdown in the sidebar, and every call it makes carries that choice. Both then read the same plan
and draw down the same budget.

So if Acme Platform has a $500 monthly cap, that $500 covers Acme's CI pipeline *and* Acme's
engineers clicking around in the UI. Spend $300 through the API and the browser has $200 left. One
number per customer, not two you have to add up.

| | API key | UI login |
|---|---|---|
| Plan entitlements (what you may run) | Enforced | Enforced |
| Monthly budget cap | Enforced | Enforced |
| Per-product quotas | Enforced | Enforced |
| Rate limits | Per key, plus the project's | The project's |
| Reading stored reviews and reports | Yes | Yes |
| Bring your own LLM credentials | Supported | Not applicable |
| Recorded in the usage ledger | Yes, as `api_key` | Yes, as `ui` |

### A key can also have its own smaller allowance

A key keeps its own optional plan, budget and rate limit. Those act as a **ceiling inside** the
project's envelope: "Acme has $500, and the CI bot may use at most $100 of it."

Both are always checked, and they **intersect** — a key's plan can only narrow what the project's
plan allows, never widen it. That is what makes this safe to turn on: adding governance can only
restrict, so nothing that works today starts working *more*.

---

## How the pieces fit together

| Thing | Question it answers | Attached to |
|---|---|---|
| **Project** | Whose work is this, and what may they spend? | The key, or the browser session |
| **Plan** | What may they run, and how much? | The project (and optionally the key) |
| **Usage ledger** | What did it actually cost? | Every call |

What happens on every call, in order:

1. **Rate limit** — too many requests this minute or today? → `429`
2. **Entitlement** — is this product in the project's plan (and the key's, if it has one)? → `403`
3. **Budget** — has the project or the key spent its monthly cap? → `402`
4. **Quota** — has this product hit its monthly limit for this project or key? → `429`
5. Run the agent or workflow
6. **Meter** — write one row to the usage ledger with product, key, project and cost

Fallbacks mean nothing breaks until you opt in:

- A project with **no plan** imposes nothing.
- A key with **no plan** falls back to its old `scopes` column, which in practice means unrestricted.
- **Deactivating a plan** drops its subjects back to those fallbacks rather than locking them out.
- **Deleting a plan** does the same, so it makes its subjects *less* restricted — the one
  direction a governance change should never take by accident. The delete is refused while
  anything is still on the plan, and the refusal names what. Confirm it and both the
  projects and the keys are released cleanly; neither is left pointing at a plan that no
  longer exists.
- A signed-in user who has **not picked a project** has no plan and no budget, exactly as the whole
  UI behaved before this existed. Set `UI_REQUIRE_PROJECT=true` to refuse those calls instead.
- Everyone who was a **project admin** before members existed is still able to work in their
  project — administering one implies membership, so nobody is locked out by the change.

---

## Who can work in a project

Two different rights, deliberately kept apart:

| | Member | Project admin |
|---|---|---|
| Select the project in **Working in** | Yes | Yes |
| Run agents against it, under its plan and budget | Yes | Yes |
| See its reviews in History and on the Dashboard | Yes | Yes |
| Manage its API keys | No | Yes |
| Manage its members | No | Yes |
| Change its plan, budget or rate limits | No | No — super admin only |

Being an admin implies membership, so an admin does not need adding twice; they appear in the
member list flagged as an admin.

The distinction matters because there used to be only one of these. Letting a developer run
agents against a project meant making them a project admin, which also handed them the
project's API keys — so the safer-looking choice was to assign nobody, and then everyone
worked outside any project, under no plan and no budget at all.

## Setting it up

1. **Projects → Limits** (super admin) — put the project on a plan, give it a monthly cap, and
   optionally a rate limit. Leave anything blank for unlimited.
2. **Plans** — build the plan itself: tick the agents and workflows it includes, and set per-product
   quotas if you want them.
3. **Projects → Members** — add the people who will work in the project. Until you do, their
   calls run outside its plan and budget entirely.
4. **Projects → Manage Admins** (super admin) — who can manage this project's keys and members.
5. **API Keys → Edit** — optionally give a key a tighter plan or cap than the project's.

Users see their remaining budget under the project dropdown, and agents their plan doesn't include
appear greyed out with a lock. A refusal comes back as plain English — "Monthly budget of $50.00 for
project 'Acme Platform' exhausted" — not a raw error.

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
| `403` | The plan does not include this product, or you may not work in that project |
| `402` | A monthly budget cap is spent — the message says whose, the project's or the key's |
| `429` | A rate limit or a per-product quota was hit — the message says which |

Every refusal names the scope it came from, so "why did this fail" never needs a support ticket.

### Keep keys secret

A key is a bearer credential: anyone holding it can spend against it. Do not paste one into a chat,
a ticket, a screenshot, or a commit. If a key is exposed, revoke it from the API Keys page and issue
a new one — revoking takes effect immediately.

---

## Spend limits are not read permissions

Plans, budgets and quotas govern what a caller may **spend**. They say nothing about what a
caller may **read**, and the endpoints behind the Dashboard and History pages return the
artifacts people submitted along with the findings produced from them.

Those endpoints now require a credential — any valid one, a login or an API key. There is
no setting to reopen them: a spend cap is a business policy that can reasonably be switched
off, and "anyone may read customer source code" is not.

They are also **scoped**. A review records the project it was run under, and History, the
Dashboard, single reviews, reports and scaffold downloads all obey it:

- Pick a project in **Working in** and every one of those screens shows that project. The
  picker means the same thing for history as it does for spend.
- With nothing picked, you see every project you are a member of.
- An API key sees its own project and nothing else.
- A super admin with nothing picked sees everything.

Asking for a review in a project you cannot see returns **404**, not 403 — whether it exists
is itself something you have no business learning.

**Reviews from before this existed have no project.** There is no way to work out
retrospectively whose they were, so they are visible to super admins only. Anything run
without a project selected joins them there.

## Two settings worth knowing

**`LEGACY_ROUTES_REQUIRE_AUTH`** (ships as `true`). The `/api/review/*` endpoints the web UI posts to
once accepted requests with no credentials at all and treated them as unrestricted — a way around
every limit above. It is now closed by default. The UI always sends its token, so signing in is
unaffected; what stops working is *anonymous* browser use (dismissing the login modal and running
agents anyway). Setting it to `false` reopens the bypass.

**`UI_REQUIRE_PROJECT`** (ships as `false`). A signed-in user who hasn't picked a project has nothing
to read a limit from, so they run unrestricted. That is right for an internal console and wrong once
customers have logins — turn it on and unbound calls are refused with a `403` telling them to pick a
project. Add people to their projects under **Projects → Members** first, or they will be
locked out.

### Checking what a project is allowed to do

```powershell
$headers = @{ "Authorization" = "Bearer $token"; "X-Project-Id" = "<project id>" }
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/products" -Headers $headers |
  Select-Object -ExpandProperty products |
  Where-Object entitled |
  Format-Table id, kind, quota_calls_per_month, used_calls_this_period
```

The usage figures are the project's — they include what a browser session and an API key have both
spent — because that is the number the quota is actually checked against.

---
