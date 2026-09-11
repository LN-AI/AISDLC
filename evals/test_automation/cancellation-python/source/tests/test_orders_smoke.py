"""The tests that already exist, and the reason they exist: they are the *idiom* a generated
suite has to match — pytest with plain functions and plain `assert`, the autouse `fresh`
fixture from `conftest.py`, and the outbox read straight off `notifier.sent` rather than
mocked. A generator shown this and still writing `unittest.TestCase` classes, its own
teardown, or a mock of the notifier has not read the project it was given.

None of these reaches a shipped order. That is deliberate: what the requirement says about
a shipped order is what the generated suite is being asked to find out.
"""
import pytest

from app import notifier, orders, store


def test_a_pending_order_can_be_cancelled():
    cancelled = orders.cancel("1001")
    assert cancelled["status"] == "cancelled"
    assert store.get("1001")["status"] == "cancelled"


def test_cancelling_notifies_the_owner_exactly_once():
    orders.cancel("1001")
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["user_id"] == "alice"
    assert "Blue mug" in notifier.sent[0]["order"]


def test_a_delivered_order_cannot_be_cancelled():
    with pytest.raises(orders.CannotCancel):
        orders.cancel("1003")
    assert store.get("1003")["status"] == "delivered"
    assert notifier.sent == []


def test_an_unknown_order_is_not_found():
    with pytest.raises(orders.NotFound):
        orders.cancel("9999")
    assert notifier.sent == []
