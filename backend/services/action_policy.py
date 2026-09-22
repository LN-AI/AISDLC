"""What an explorer is allowed to do to somebody's running application.

This is the safety core of application-based testing, and it is the reason that product can
be pointed at a live system at all. An agent exploring an application it has never seen will
eventually be offered a button that says *Delete account*, *Cancel subscription* or *Submit
payment*, and clicking it is not a failed test -- it is damage in a real environment that no
amount of retrying undoes.

Three properties do the work, and none of them is "the model was told not to":

**A closed action vocabulary.** `ACTION_KINDS` is a tuple of six verbs. There is no
"execute", no "evaluate", no free-text selector, and therefore nothing to escape into. A
response naming anything else is refused before it reaches a browser.

**The model never authors a selector.** An action names a *candidate index* -- a position in
a list the driver built from the page's own accessibility tree. The model chooses among
things that demonstrably exist; it cannot invent a CSS path and it cannot reach an element
nobody offered it. That also removes the brittleness problem for free: every candidate
carries a role and an accessible name, so the generated suite emits a role/name locator
rather than a CSS path that dies on the next redesign.

**Screened on both sides of the model, and again where it lands.** `screen()` removes
dangerous candidates *before* the model sees the list, `permits()` re-checks whatever it
chose, and the driver calls `permits()` a third time on the machine that will actually
perform the click. The first two mirror `finding_gate` flooring a model's verdict -- the
model's choice can only ever be narrowed, never widened. The third matters most, because it
runs where the consequences are.

**What this does not do, said plainly.** The denylist matches words in an accessible name.
A button labelled "Finalise", "Commit", an icon with no accessible name at all, or a control
whose label is an image, will not match it and will be offered to the model. This is a
heuristic that removes the obvious cases; the thing actually keeping a customer safe is that
the session refuses to start without an attestation that the target is a test environment.
Treat it as a seatbelt, not a wall.

Stdlib only, and it imports nothing from `backend` -- the same rule `test_runner_plan.py`
follows and for the same reason: `scripts/explore_app.py` imports this module too, and a
second copy of a safety rule living in the driver is exactly how the rule comes back wrong.
"""
import hashlib
import re

# The complete vocabulary. Six verbs, chosen because they are what a person does to a page
# and because each maps onto one Playwright call the driver already knows how to make.
# Anything not in here is refused rather than interpreted -- an open vocabulary is how a
# page's own text ends up deciding what runs.
ACTION_KINDS = ("click", "fill", "select", "press", "goto", "back")

# Keys a caller may send for a key press. A closed list again: `press` is the one verb that
# takes a raw string, so it is the one that has to be bounded.
PRESS_KEYS = ("Enter", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight")

# Word *stems* that make a control dangerous wherever they appear in its accessible name,
# grouped by the kind of harm -- the refusal message names the group, and "this looked like
# it spends money" is a more useful sentence to an operator than "this was on a list".
#
# Stems rather than whole words, matched with `startswith`, because inflection is how a
# denylist silently fails: the first version of this listed "subscribe" and happily allowed
# a button called **Cancel subscription**. `startswith` catches subscribe/subscription/
# subscriber and delete/deleting/deletion without matching "display" the way a bare
# substring test would.
#
# The calibration is deliberate and is not "refuse everything stateful". A session cannot
# start without an attestation that the target is a test environment, and that attestation
# is what actually protects a customer; over-refusing here costs discovery breadth, which is
# the product's whole value. So this catches the catastrophic and the plainly irreversible,
# and leaves ordinary state changes alone. "send", "notify" and "reset" are left out on
# purpose -- resetting a password is a journey worth discovering in a test environment.
DESTRUCTIVE_GROUPS = {
    "destroys data": (
        "delet", "destroy", "eras", "wipe", "purg", "remov", "discard",
        "deactivat", "terminat", "revok", "suspend", "archiv", "unpublish", "cancel",
    ),
    "spends money": (
        "pay", "purchas", "buy", "checkout", "subscri", "billing",
        "charg", "refund", "withdraw", "transfer", "donat", "upgrad",
    ),
    "contacts real people": (
        "invit", "broadcast", "publish", "announc",
    ),
    "cannot be undone": (
        "finali", "irreversib", "permanent", "deploy", "releas", "merge", "confirm",
    ),
}

# Whole phrases that are dangerous even though no single word in them is. "Order" on its own
# is navigation on every commerce site there is, so it cannot go in the stem list above
# without refusing the nav bar; "Place order" is a purchase.
DESTRUCTIVE_PHRASES = {
    "spends money": ("place order", "submit order", "order now", "complete order"),
    "destroys data": ("close account", "empty trash", "clear all", "reset everything"),
}

# Words that end the session rather than damage anything. Refused for a different reason:
# an explorer that logs out on step three explores nothing afterwards, and the run is spent.
SESSION_ENDING = ("log out", "logout", "sign out", "signout", "log off")

# Names that read as destructive but are the *safe* half of a confirmation dialog. "Cancel"
# alone dismisses a modal; "Cancel subscription" does not. The rule is the whole name: if a
# control is called exactly one of these and nothing else, it is a dismiss button.
DISMISS_EXACT = ("cancel", "close", "back", "no", "dismiss", "not now", "go back",
                 "never mind", "keep", "abort")

# Roles that can be interacted with at all. A heading or a paragraph is not a candidate, and
# offering one wastes a step and a model call.
INTERACTIVE_ROLES = (
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
    "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "switch",
    "tab", "spinbutton", "slider",
)

_WORD = re.compile(r"[a-z0-9]+")
_NUMERIC = re.compile(r"[0-9]+")
_IDISH = re.compile(r"[0-9a-f]{8,}(-[0-9a-f]{4,}){0,4}")


def normalise(text):
    """Lowercase a label and collapse everything that is not alphanumeric to one space.

    Deliberately generous, because the comparison it feeds is a *refusal*: matching too
    widely costs a candidate the model never sees, and matching too narrowly costs a
    customer a deleted record.
    """
    return " ".join(_WORD.findall(str(text or "").lower()))


def classify(candidate, allow=None):
    """Judge one candidate. Returns `(verdict, reason)` where verdict is allow or refuse.

    `reason` is empty on an allow and is a sentence on a refusal, because the session
    records what it would not do and an operator reads that list to decide whether the
    denylist is too tight for their application.
    """
    verdict, reason, _group = classify_detail(candidate, allow)
    return verdict, reason


def classify_detail(candidate, allow=None):
    """The same judgement, plus *which rule* refused it: `(verdict, reason, group)`.

    The group is a bare noun phrase -- "destroys data", "spends money" -- and the reason is
    a sentence built around it. Kept separate rather than parsed back out of the sentence,
    because a screen groups refusals by rule and re-deriving a field by stripping a label
    prefix off a sentence is the kind of thing that works until somebody renames a button.

    `classify` stays as it was so its two existing callers, and the driver that imports this
    module, are untouched.

    `allow` is a set of **normalised, exact** control names a person has permitted for this
    project. It is measured rather than theoretical: pointed at a banking application the
    denylist withheld `Transfer Funds`, `Withdraw Funds` and `Bill Pay` -- which is most of
    what a bank *is* -- and at a shop it withheld `Remove` as destroying data and `Checkout`
    as spending money, leaving six of fifteen supplied requirements untestable. This list
    reads button labels with no context, so it cannot tell "Remove [from basket]" from
    "Remove [user]", and nothing it could be taught would let it. The customer knows; we
    do not.
    """
    name = normalise(candidate.get("name"))
    role = normalise(candidate.get("role"))
    label = str(candidate.get("name") or "")

    if candidate.get("enabled") is False:
        return "refuse", "the control is disabled", "is disabled"
    if role and role not in INTERACTIVE_ROLES:
        return "refuse", role + " is not an interactive role", "is not interactive"

    # A control somebody has explicitly permitted for this project.
    #
    # Exact and normalised, never a substring: a substring allow-list would let "delete"
    # permit "Delete account", which is the failure this whole module exists to prevent,
    # reached from the other side. It sits *after* the two checks above on purpose --
    # disabled and non-interactive are validity rules rather than safety rules, and are not
    # anybody's to waive -- and before every rule that is about danger, which are the ones
    # a customer is entitled to overrule for their own test environment.
    if allow and name in allow:
        return "allow", "", ""

    # The dismiss exception, checked before the denylist: "Cancel" on its own is how a
    # person backs out of a dialog safely, and refusing it traps the explorer inside modals.
    if name in DISMISS_EXACT:
        return "allow", "", ""

    for group, phrases in DESTRUCTIVE_PHRASES.items():
        for phrase in phrases:
            if phrase in name:
                return "refuse", label + " " + group, group

    words = name.split()
    for group, stems in DESTRUCTIVE_GROUPS.items():
        for stem in stems:
            if any(word.startswith(stem) for word in words):
                return "refuse", label + " " + group, group

    for term in SESSION_ENDING:
        if term in name:
            return "refuse", label + " ends the session", "ends the session"

    return "allow", "", ""


def screen(candidates, allow=None):
    """Split candidates into what the model may be offered and what it may not.

    Called before the model sees anything. The allowed list is re-indexed from zero so the
    index the model returns means a position in the list it was actually given -- handing it
    a list with gaps in the numbering is how an off-by-one becomes a click on the wrong row.

    **`allow` must be the same on both sides.** The platform screens to choose an index and
    the driver screens again to resolve it, and the index only means anything while the two
    lists are identical. That held by construction -- same input, same module -- until this
    argument existed. The platform therefore sends the list it used back to the driver
    rather than letting it look one up, and `permits` cross-checks the chosen name against
    the position, so a divergence becomes a refusal instead of a click on the wrong row.
    """
    allowed = []
    refused = []
    for original in candidates or []:
        if not isinstance(original, dict):
            continue
        verdict, reason, group = classify_detail(original, allow)
        row = dict(original)
        if verdict == "allow":
            row["index"] = len(allowed)
            allowed.append(row)
        else:
            row["refused_because"] = reason
            row["refused_group"] = group
            refused.append(row)
    return allowed, refused


def permits(action, allowed, allow=None):
    """Re-check a chosen action against the list it was chosen from.

    This runs after the model answers and again inside the driver. It is not redundant with
    `screen`: a model can return an index nobody offered, a verb that is not in the
    vocabulary, or a candidate that passed the screen but whose *name* changed between the
    observation and the click. All three are refusals, not corrections -- silently choosing
    a different element for the model is how an explorer does something nobody can explain
    afterwards.
    """
    if not isinstance(action, dict):
        return False, "the action was not an object"

    kind = str(action.get("action_kind") or "").strip()
    if kind not in ACTION_KINDS:
        return False, (kind + " is not one of the permitted actions ("
                       + ", ".join(ACTION_KINDS) + ")")

    if kind == "back":
        return True, ""

    if kind == "goto":
        path = str(action.get("value") or "")
        if not path.startswith("/"):
            return False, "goto takes a path beginning with a slash, not a full URL"
        if "//" in path[1:] or ".." in path:
            return False, "that path tries to leave the application"
        return True, ""

    if kind == "press":
        key = str(action.get("value") or "")
        if key not in PRESS_KEYS:
            return False, (key + " is not one of the permitted keys ("
                           + ", ".join(PRESS_KEYS) + ")")

    index = action.get("candidate_index")
    if not isinstance(index, int) or isinstance(index, bool):
        return False, "candidate_index must be a whole number"
    if not 0 <= index < len(allowed):
        return False, ("candidate_index " + str(index) + " is outside the "
                       + str(len(allowed)) + " candidate(s) that were offered")

    verdict, reason = classify(allowed[index], allow)
    if verdict != "allow":
        return False, "refused on re-check: " + reason

    # The name the caller was *told* it was choosing, against the name at that position in
    # the list the caller itself built.
    #
    # Only the driver can make this check, and only the driver needs it: the platform sets
    # `target_name` from the same list it indexed into, so server-side there is nothing to
    # disagree with. On the driver it is the one thing standing between two lists quietly
    # diverging and a click landing on the wrong element. The index is meaningful *only*
    # while both sides screen identical input with identical rules -- which held by
    # construction until the moment a per-project allow-list gave the two sides different
    # rules, and nothing else in this path would have noticed.
    if "target_name" in action:
        here = str(allowed[index].get("name", ""))
        told = str(action.get("target_name") or "")
        if here != told:
            return False, ("position " + str(index) + " holds " + repr(here) + ", not "
                           + repr(told) + " -- the two candidate lists do not agree, so "
                           "this index points somewhere nobody chose")

    if kind in ("fill", "select") and not str(action.get("value") or "").strip():
        return False, kind + " needs a value"

    return True, ""


def page_signature(url_path, candidates, texts=None):
    """A stable fingerprint for which page we are on, used to detect *no progress*.

    Deciding whether two views are the same page is genuinely hard -- one URL serves many
    states, modals do not change the address, a wizard's steps all look alike. This does not
    try to answer that. It answers a narrower question that is enough: **has anything
    changed?** The digest covers the path with numeric and uuid-ish segments generalised
    (so /orders/41 and /orders/93 are one page) plus the sorted set of role and name pairs
    on offer, which is what actually differs between a list and the dialog on top of it.

    A session that produces no new signature for several steps has stopped exploring, which
    is a cheap, deterministic stopping rule that needs no model call to evaluate.
    """
    parts = []
    for segment in str(url_path or "/").split("/"):
        if not segment:
            continue
        if _NUMERIC.fullmatch(segment):
            parts.append(":n")
        elif _IDISH.fullmatch(segment.lower()):
            parts.append(":id")
        else:
            parts.append(segment.lower())
    shape = "/" + "/".join(parts)

    # Role, name *and* current value. The value matters because filling in a form is
    # progress and changes nothing else: same controls, same words, same address. Without it
    # a form with more fields than `NO_PROGRESS_LIMIT` is stopped for making no progress
    # while it is being filled in correctly, and no session ever reaches the button at the
    # bottom of it. Including the value keeps the rule's real target -- typing the *same*
    # thing again still produces the same signature, which is what a stuck explorer does.
    pairs = sorted(set(
        normalise(c.get("role")) + "|" + normalise(c.get("name"))
        + "|" + normalise(c.get("value"))
        for c in (candidates or []) if isinstance(c, dict)
    ))
    # What the page says counts as part of "has anything changed?", and on a client-rendered
    # application it is often the *only* part that changes: pressing "One more Widget" leaves
    # every control exactly where it was and rewrites a total. Without this such a session
    # looks stalled after a few steps and is stopped while it is still making progress.
    #
    # The cost of including it is the opposite failure: a page with a clock or a live counter
    # never repeats a signature, so the no-progress rule stops firing for it. That is the
    # better failure to have, because `max_steps` still bounds the session and a premature
    # stop wastes the whole run rather than the tail of it.
    said = sorted(set(normalise(t) for t in (texts or []) if t))
    digest = hashlib.sha256("\n".join([shape] + pairs + said).encode("utf-8")).hexdigest()
    return shape + "#" + digest[:12]


def structural_signature(url_path, candidates):
    """The same fingerprint with what the page *says* left out. C-111's answer.

    `page_signature` folds page text in on purpose, and the comment above says why: on a
    client-rendered application the text is the only thing a click changes. It also states
    the cost -- "a page with a clock or a live counter never repeats a signature" -- and
    judges that the better failure to have. Measurement since then says otherwise. C-105 put
    the price of an undetected loop at **22 of 26 paid steps**, and the blind spot is not the
    rare case it sounds like: `TEXTS_JS` collects `[role=status]` and `[aria-live]`, so any
    toast, badge, "last updated" stamp or clock disables the rule outright. Verified on the
    real function -- four visits to one unchanged page with a single ticking status line give
    four distinct signatures; remove the line and they give one.

    The fix is deliberately **not** to drop live regions from `page_signature`. The
    `basket-total` eval case renders its total as
    `<strong id="total" role="status" aria-live="polite">`, so excluding live regions would
    blind the rule to the one value that case exists to measure -- the obvious fix, and a
    regression in the only case already living at two thirds.

    So this is a second, coarser fingerprint rather than a change to the first, and the two
    are asked separately (`app_session.stalled`). Nothing a page does on its own can move
    this one: it covers the address, the controls on offer and their current values, so
    filling in a form still reads as progress (D-413) while a clock, a toast and a
    re-selected dropdown do not.
    """
    return page_signature(url_path, candidates, None)


def describe_policy():
    """What the caller is bound by, so a driver and a screen can show it rather than guess."""
    return {
        "action_kinds": list(ACTION_KINDS),
        "press_keys": list(PRESS_KEYS),
        "destructive_groups": dict((k, list(v)) for k, v in DESTRUCTIVE_GROUPS.items()),
        "session_ending": list(SESSION_ENDING),
        "dismiss_exact": list(DISMISS_EXACT),
        "interactive_roles": list(INTERACTIVE_ROLES),
    }
