"""A shop whose basket lives entirely in the browser. Standard library only.

The second case of `evals/app_testing`, and it exists to ask a different question from
`task-ownership`. That one is ordinary server-rendered HTML: every action is a form post or
a link, and every result is a new page. This one never navigates. The item list, the basket
and the total are drawn by JavaScript into a page that loads once, and a click changes the
DOM in place.

Most applications a customer would point this product at behave like this one, so a corpus
of server-rendered pages would measure the friendly half of the world. What it puts under
test specifically:

* whether the collector still reads a control that JavaScript created after load,
* whether the explorer notices a change when the URL never changes -- the page signature has
  no new path to work from, only new controls and values,
* whether the suite waits for an asynchronous update rather than reading the old value.

The bug is in `pricing.js` and nowhere else, so `fixed/` overlays one file. It is deliberately
invisible at a quantity of one: a line total that ignores quantity is correct until somebody
asks for two. An explorer that adds an item and stops writes a suite that passes on both
builds, which is the honest failure this case is built to catch.

    python app.py --port 8123
"""
import argparse
import http.server
import json
import os
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))

ITEMS = [
    {"id": "widget", "name": "Widget", "price": 4.00},
    {"id": "gadget", "name": "Gadget", "price": 2.50},
    {"id": "sprocket", "name": "Sprocket", "price": 9.00},
]

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Corner Shop</title></head>
<body>
<h1>Corner Shop</h1>
<main>
  <h2>Items for sale</h2>
  <ul id="items"></ul>

  <h2>Your basket</h2>
  <ul id="basket"></ul>
  <p>Total: <strong id="total" role="status" aria-live="polite">GBP 0.00</strong></p>
  <p><button id="checkout" type="button">Checkout</button></p>
</main>
<script src="/pricing.js"></script>
<script src="/app.js"></script>
</body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # the eval reads stdout; a request log per click would bury it

    def _send(self, body, content_type="text/html; charset=utf-8", status=200):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        # No caching: the fixed and broken variants serve the same paths, and a browser that
        # kept `pricing.js` from the previous run would score the wrong build.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, name, content_type):
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            return self._send("not found", status=404)
        with open(path, encoding="utf-8") as fh:
            return self._send(fh.read(), content_type=content_type)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(PAGE)
        if path == "/app.js":
            return self._file("app.js", "application/javascript; charset=utf-8")
        if path == "/pricing.js":
            return self._file("pricing.js", "application/javascript; charset=utf-8")
        if path == "/api/items":
            return self._send(json.dumps(ITEMS), "application/json; charset=utf-8")
        return self._send("<p>No such page.</p>", status=404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("listening on http://127.0.0.1:%d" % args.port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
