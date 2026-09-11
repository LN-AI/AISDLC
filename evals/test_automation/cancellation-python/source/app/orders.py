"""The order lifecycle, deliberately holding one real bug.

`cancel` decides what it will refuse by listing the states it considers finished, and
"shipped" is not on that list — so an order already on its way to the customer can still be
cancelled, its status overwritten and a cancellation notice sent for goods that are moving.

The standard library and nothing else, so the eval that uses this app measures the test
generator rather than a package manager.
"""
from app import store

# The states `cancel` treats as finished. A shipped order is not finished, and that is the
# whole of the defect: not being finished is not the same as being cancellable.
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
    if order["status"] in TERMINAL_STATUSES:
        raise CannotCancel(f"order {order_id} is {order['status']} and cannot be cancelled")

    cancelled = store.set_status(order_id, "cancelled")

    # `notifier` is imported here rather than at the top of the file because `notifier`
    # imports this module: at module scope it would be a circular import and the package
    # would fail to load.
    from app import notifier
    notifier.send(order["owner_id"], "Your order was cancelled", order_id)
    return cancelled
