"""What sign-up has collected so far. Standard library only.

This is the whole of the bug, and it is deliberately one function: `fixed/` replaces this
file and nothing else, so a suite that tells the two variants apart is telling them apart on
the defect rather than on incidental differences.
"""

DRAFTS = {}


def remember(sid, fields):
    """BUG: each step replaces the draft instead of adding to it.

    The kind of defect that only a completed journey can see. Every individual page works:
    step one accepts a name and an email and moves on, step two accepts an address and moves
    on, and each of them looks right on its own. What is wrong is only visible at step three,
    where the details from step one should still be there and are not.

    An explorer that opens each page and reads it finds nothing. An explorer that walks the
    whole flow through to the end finds it immediately.
    """
    DRAFTS[sid] = dict(fields)


def draft(sid):
    return dict(DRAFTS.get(sid) or {})


def clear(sid):
    DRAFTS.pop(sid, None)
