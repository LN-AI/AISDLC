"""The whole data layer. In memory on purpose: this app exists to be a subject for the
test-automation eval, and a real database would make the eval measure Postgres."""

_orders = {}


def reset():
    """Back to the three orders every test starts from, one in each interesting state."""
    _orders.clear()
    for order in (
        {"id": "1001", "owner_id": "alice", "item": "Blue mug", "total_cents": 1299,
         "status": "pending"},
        {"id": "1002", "owner_id": "bob", "item": "Desk lamp", "total_cents": 4999,
         "status": "shipped"},
        {"id": "1003", "owner_id": "carol", "item": "Notebook", "total_cents": 799,
         "status": "delivered"},
    ):
        _orders[order["id"]] = dict(order)


def get(order_id):
    return _orders.get(str(order_id))


def all():
    return list(_orders.values())


def set_status(order_id, status):
    order = get(order_id)
    if order is None:
        return None
    order["status"] = status
    return order
