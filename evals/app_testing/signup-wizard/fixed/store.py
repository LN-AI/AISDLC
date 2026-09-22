"""What sign-up has collected so far. The fix: each step adds to the draft, it does not
replace it."""

DRAFTS = {}


def remember(sid, fields):
    """Keep what earlier steps collected and add this step's fields to it."""
    DRAFTS.setdefault(sid, {}).update(fields)


def draft(sid):
    return dict(DRAFTS.get(sid) or {})


def clear(sid):
    DRAFTS.pop(sid, None)
