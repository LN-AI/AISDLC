"""Who may edit a task. The fix: ownership, not merely being signed in."""


def may_edit(task, username):
    """A task may be edited only by the user who created it."""
    return bool(username) and task["owner"] == username
