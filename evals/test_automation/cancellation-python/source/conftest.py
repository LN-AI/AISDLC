"""Shared pytest setup for this project.

Two jobs, and both are conventions a test written here is expected to rely on.
"""
import os
import sys

# `app` lives beside this file rather than being installed, so the project root has to be
# importable. pytest puts a conftest's own directory on `sys.path`, but saying it here means
# the suite also runs when it is invoked from somewhere else.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

from app import notifier, store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    """Every test starts from the same three orders and an empty outbox.

    `autouse`, so no test has to ask for it — and so no test can forget, which is the only
    way state from one test stops leaking into the next in an in-memory store.
    """
    store.reset()
    notifier.reset()
    yield
