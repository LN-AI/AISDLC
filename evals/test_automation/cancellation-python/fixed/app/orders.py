"""The order lifecycle, with the bug fixed.

The only difference from `source/app/orders.py` is in `cancel`: it now asks whether the
order *is pending* instead of listing the states it considers finished. Stated that way,
"shipped" is refused without anyone having to remember to add it — which is why the fix is
the condition rather than an extra entry in `TERMINAL_STATUSES`.

The standard library and nothing else, so the eval that uses this app measures the test
generator rather than a package manager.
"""
from app import store

# Kept because `ship` and `deliver` read as a state machine beside it, and because the fix
# is deliberately the smallest one that makes the requirement hold: the condition below no
# longer consults this list, so nothing depends on it being complete.
TERMINAL_STATUSES = ("delivered", "cancelled")


class NotFound(Exception):
    """No order with that id."""


class CannotCancel(Exception):
    """The order exists but is not in a state that may be cancelled."""


def get(order_id):
    order = store.get(order_id)
    if order is None:
        raise NotFound(f"no order {order_id}")
    return order


def describe(order_id):
    order = get(order_id)
    return f"{order['item']} (#{order['id']}, {order['status']})"


def ship(order_id):
    order = get(order_id)
    if order["status"] != "pending":
        raise CannotCancel(f"order {order_id} is {order['status']}, not pending")
    return store.set_status(order_id, "shipped")


def deliver(order_id):
    order = get(order_id)
    if order["status"] != "shipped":
        raise CannotCancel(f"order {order_id} is {order['status']}, not shipped")
    return store.set_status(order_id, "delivered")


def cancel(order_id):
    """Cancel an order and tell its owner. Raises NotFound or CannotCancel."""
    order = get(order_id)
    if order["status"] != "pending":
        raise CannotCancel(f"order {order_id} is {order['status']} and cannot be cancelled")

    cancelled = store.set_status(order_id, "cancelled")

    # `notifier` is imported here rather than at the top of the file because `notifier`
    # imports this module: at module scope it would be a circular import and the package
    # would fail to load.
    from app import notifier
    notifier.send(order["owner_id"], "Your order was cancelled", order_id)
    return cancelled
