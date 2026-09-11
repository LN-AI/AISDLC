# orderdesk

A very small order service. Orders move `pending` -> `shipped` -> `delivered`, and the
owner may cancel one while it is still pending. There is no web layer and no database:
this is the service layer on its own.

| Function | What it does |
|---|---|
| `orders.get(order_id)` | one order, or `NotFound` |
| `orders.describe(order_id)` | a one-line summary, used in notices |
| `orders.ship(order_id)` | `pending` -> `shipped` |
| `orders.deliver(order_id)` | `shipped` -> `delivered` |
| `orders.cancel(order_id)` | cancel, and notify the owner |

## Running the tests

```
python -m pytest tests/
```

pytest and the standard library, nothing else. `conftest.py` sits at the project root so
`app` is importable and so every test starts from the same three orders with an empty
outbox — the `fresh` fixture is `autouse`, so no test has to ask for it.

## Two conventions worth knowing before you write a test here

**The outbox is a list, not a mock.** `notifier.send` appends to `notifier.sent` instead of
sending anything, so "did this notify the owner" is a plain assertion about a list.

**`app.notifier` imports `app.orders`.** That is why `orders.cancel` imports `notifier`
inside the function body rather than at the top of the file: at module scope it would be a
circular import and the package would not load at all.
