"""Outbound notices.

Recorded rather than sent: `send` appends to `sent`, so "was the owner told" is a plain
assertion about a list and no test has to mock anything.

This module imports `app.orders` at module scope, which is the half of the cycle that
works — `orders` does not import this one at the top of its file for exactly that reason.
"""
from app import orders

# Every notice this process has produced, oldest first. Cleared by `reset()` between tests.
sent = []


def send(user_id, subject, order_id):
    sent.append({
        "user_id": user_id,
        "subject": subject,
        "order": orders.describe(order_id),
    })
    return sent[-1]


def reset():
    sent.clear()
