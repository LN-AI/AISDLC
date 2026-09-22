"""Who may edit a task.

This is the whole of the bug, and it is deliberately one function: the `fixed/` overlay
replaces this file and nothing else, so a suite that discriminates between the two variants
is discriminating on exactly one behaviour rather than on incidental differences.
"""


def may_edit(task, username):
    """BUG: any signed-in user may edit any task.

    The check that exists is "is somebody signed in", which is authentication. The check the
    requirement asks for is "is this *their* task", which is authorisation. Conflating the
    two is one of the most common real defects there is, and from the outside it is invisible
    until somebody looks at a record that is not theirs.
    """
    return bool(username)
