"""What a run costs the customer, as opposed to what it costs us. One table, one place.

Until this existed every product was billed at **pass-through model cost**: whatever the
tokens cost Anthropic was what appeared in `usage_events`, on the screen and against the
quota. That is a defensible default and it is still the default here — but it prices a
product at the cost of its raw material, which for anything with real engineering behind it
is not a price at all.

D-383 asked what a `test-automation` run should cost and recorded the reasoning; this
answers it, and the answer is **deliberately trivial to change**: three numbers in `POLICY`
below. Nothing else in the platform knows how it is calculated.

The basis, and the three it was chosen over
-------------------------------------------
**A flat platform fee per run, plus marked-up model spend, under a ceiling.**

* **Not per test.** The count is decided by the model *during* the run, so a preflight could
  not quote what the invoice would say — and every screen here quotes a ceiling before it
  spends. Worse, revenue per test pays the agent to pad, in the one product whose entire
  value is that it does not (C-66).
* **Not per file**, for the same quoting reason, and it would need a counter that does not
  exist.
* **Not pass-through alone.** The cost driver is *input* size: a live run read a 3.4MB
  codebase for $0.27 and produced nineteen tests, while a small app costs $0.07. Billing
  only the tokens means the hardest runs earn least, and a customer's bill swings with our
  model choice rather than with anything they did.

The fee is what makes a small run worth serving, the markup is what makes a large one worth
serving, and the ceiling is what makes either safe to approve.

Two kinds of run are never charged the fee. One that never reached the model is charged
nothing at all — a failed request is not a service, and billing one would make failing the
cheapest way to charge somebody. One that *did* reach the model and then failed is recorded
by `agent_guard._record_failed_call` as unbillable and never passes through this module, so
it moves no cap and appears on no invoice: the tokens are logged for visibility, and the
customer is not asked to pay for an answer they did not get.

What this deliberately does not do
----------------------------------
It does not change any other product. Every product without a row in `POLICY` is
pass-through, priced exactly as it was, so this is one product's pricing rather than a
platform-wide repricing nobody asked for.

It also does not hide our own cost. `usage_events.model_cost_usd` keeps what the tokens
actually cost alongside `cost_usd`, which is what the customer owes. Storing only the price
would make margin unanswerable, and storing only the cost would make the invoice a
derivation nobody could check — the same reason `database.get_review_spend` refuses to keep
a second copy of a number.

**One number reaches the customer.** The quote, the figure on the screen and the ledger row
all come through `price()`, for the reason `legacy_guard` records at its own header block: a
display cost derived any other way is a number that will eventually disagree with the
invoice, and that is worse than showing nothing at all.
"""
from typing import Any, Dict, Optional

# Pass-through: the platform takes nothing and the customer pays what the tokens cost. This
# is what every product had before this module and what every product still has unless it
# appears in POLICY below.
PASS_THROUGH: Dict[str, Any] = {"fee_usd": 0.0, "markup": 1.0, "ceiling_usd": 0.0}

# ── The one place a price changes ────────────────────────────────────────────
#
# `fee_usd`     charged once per run that actually calls the model
# `markup`      multiplier on the model spend (1.0 means pass-through)
# `ceiling_usd` the most one run can ever be charged; 0 means uncapped
#
# Chosen 2026-09-10 against measured spend rather than guessed: a small app generation
# measures $0.063–$0.075 (three runs, `evals/test_automation/results/measured-baseline.json`)
# and a 173-file hybrid run measured $0.268. Under this policy those become $0.63 and $1.04.
# The ceiling sits above the largest run seen so far with room to spare, so it caps runaway
# input rather than ordinary use.
POLICY: Dict[str, Dict[str, Any]] = {
    "test-automation": {"fee_usd": 0.50, "markup": 2.0, "ceiling_usd": 5.00},

    # The flagship, sellable-on-their-own products — see D-464 for the reasoning behind
    # pricing this set now and leaving the rest pass-through. Starting numbers, not a
    # considered business decision: deliberately round, deliberately easy to change, and
    # unvalidated against real usage volume the way test-automation's were (D-383). Revisit
    # once real customer usage gives an actual cost distribution to price against.
    "code":                  {"fee_usd": 0.25, "markup": 2.00, "ceiling_usd": 3.00},
    "vapt":                  {"fee_usd": 0.50, "markup": 2.00, "ceiling_usd": 5.00},
    "security":              {"fee_usd": 0.50, "markup": 2.00, "ceiling_usd": 5.00},
    "architecture":          {"fee_usd": 0.50, "markup": 2.00, "ceiling_usd": 5.00},
    "compliance":            {"fee_usd": 0.25, "markup": 1.75, "ceiling_usd": 3.00},
    "gxp-document-check":    {"fee_usd": 0.50, "markup": 1.75, "ceiling_usd": 5.00},
    "gxp-generate-plan":     {"fee_usd": 0.75, "markup": 2.00, "ceiling_usd": 7.50},
    "gxp-generate-document": {"fee_usd": 1.00, "markup": 2.00, "ceiling_usd": 10.00},
    "full":                  {"fee_usd": 1.00, "markup": 1.75, "ceiling_usd": 10.00},
    "auto-fix":              {"fee_usd": 0.75, "markup": 2.00, "ceiling_usd": 7.50},
    "migration-execute":     {"fee_usd": 0.75, "markup": 1.75, "ceiling_usd": 7.50},
    "app-testing":           {"fee_usd": 0.50, "markup": 2.00, "ceiling_usd": 5.00},
}


def policy_for(product_id: str) -> Dict[str, Any]:
    """The policy for a product, or pass-through. Never raises on an unknown product."""
    return dict(POLICY.get((product_id or "").strip(), PASS_THROUGH))


def has_policy(product_id: str) -> bool:
    """Whether this product is priced rather than passed through. Lets a caller decide
    whether a customer-facing explanation is worth printing at all."""
    return (product_id or "").strip() in POLICY


def price(product_id: str, model_cost_usd: float, calls: int = 1) -> Dict[str, Any]:
    """What to charge for one run. -> every component, not just the total.

    Every part is returned because a total nobody can decompose is a total nobody can
    dispute, and the fee is the component most likely to be queried.

    `calls == 0` means nothing reached the model — a validation failure, a cache hit, a
    refusal — and charges nothing at all. Charging a platform fee for a request that
    performed no work would make the cheapest way to bill a customer be to fail.
    """
    rule = policy_for(product_id)
    raw = max(0.0, float(model_cost_usd or 0.0))
    if not calls:
        return {"product_id": product_id, "model_cost_usd": round(raw, 6),
                "fee_usd": 0.0, "markup": rule["markup"], "marked_up_usd": 0.0,
                "total_usd": 0.0, "ceiling_usd": rule["ceiling_usd"], "capped": False,
                "priced": has_policy(product_id)}

    marked_up = raw * float(rule["markup"])
    total = float(rule["fee_usd"]) + marked_up
    ceiling = float(rule["ceiling_usd"] or 0.0)
    capped = bool(ceiling and total > ceiling)
    return {
        "product_id": product_id,
        "model_cost_usd": round(raw, 6),
        "fee_usd": round(float(rule["fee_usd"]), 6),
        "markup": float(rule["markup"]),
        "marked_up_usd": round(marked_up, 6),
        "total_usd": round(min(total, ceiling) if capped else total, 6),
        "ceiling_usd": round(ceiling, 6),
        "capped": capped,
        "priced": has_policy(product_id),
    }


def total_for(product_id: str, model_cost_usd: float, calls: int = 1) -> float:
    """Just the number, for the two metering sites that only need the number."""
    return price(product_id, model_cost_usd, calls)["total_usd"]


def describe(priced: Dict[str, Any]) -> str:
    """One sentence a customer can check, or "" for a pass-through product.

    Empty rather than "no markup applied", because a product that is not priced this way
    should not have a pricing sentence attached to it at all.
    """
    if not priced or not priced.get("priced"):
        return ""
    parts = []
    if priced.get("fee_usd"):
        parts.append(f"${priced['fee_usd']:.2f} per run")
    if priced.get("markup", 1.0) != 1.0:
        parts.append(f"{priced['markup']:g}x the ${priced['model_cost_usd']:.4f} of model "
                     f"time it used")
    sentence = f"${priced['total_usd']:.2f} — " + " plus ".join(parts) if parts else \
        f"${priced['total_usd']:.2f}"
    if priced.get("capped"):
        sentence += (f", capped at ${priced['ceiling_usd']:.2f} (the run would otherwise "
                     f"have cost more)")
    elif priced.get("ceiling_usd"):
        sentence += f". No run is charged more than ${priced['ceiling_usd']:.2f}"
    return sentence + "."


def apply_to_quote(quote: Dict[str, Any], product_id: str) -> Dict[str, Any]:
    """Add what a quote will actually be *charged* at, leaving the token estimate intact.

    Both numbers matter and they answer different questions: `expected_usd` is what the
    model time is expected to cost, `billing.total_usd` is what will appear on the invoice.
    A quote that showed only the first would understate the bill, which is the direction
    that loses trust; one that showed only the second would make the ceiling unexplainable.
    """
    if not isinstance(quote, dict) or not has_policy(product_id):
        return quote
    priced = quote.copy()
    priced["billing"] = {
        "expected": price(product_id, quote.get("expected_usd") or 0.0),
        # `ceiling_usd` is the quote's own worst case — the run button names it as the
        # amount the customer approved, so it is the one that has to be priced too.
        "ceiling": price(product_id,
                         quote.get("ceiling_usd") or quote.get("expected_usd") or 0.0),
    }
    priced["billing"]["explanation"] = describe(priced["billing"]["expected"])
    return priced
