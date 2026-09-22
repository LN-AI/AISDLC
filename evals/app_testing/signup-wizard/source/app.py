"""A three-step sign-up, served from the standard library and nothing else.

The third case of `evals/app_testing`. `task-ownership` measures whether assertions are
honest and `basket-total` measures whether a client-rendered page can be read at all; this
one measures **exploration depth**, which neither of the others does.

Its defect cannot be seen from any single page. Step one accepts a name and an email and
moves on. Step two accepts an address and moves on. Both look correct in isolation, and an
explorer that visits each page, reads it and moves to the next finds nothing wrong. The
details from step one are missing at step three, and only a session that walks the whole
flow through to the end ever reaches step three with something to compare.

That is the honest hard case for this product. The easy failure of an exploring agent is not
writing bad assertions -- it is wandering: opening every page once, never completing
anything, and producing a suite of shallow tests about pages that load. A case whose bug is
invisible until a journey is finished is the only way to measure the difference.

    python app.py --port 8123
"""
import argparse
import html
import http.server
import urllib.parse
import uuid

from store import clear, draft, remember

SESSIONS = set()


def page(title, body):
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>%s</title></head>
<body>
<h1>Northwind Books</h1>
<nav><a href="/">Start again</a></nav>
<main>%s</main>
</body></html>""" % (html.escape(title), body)


STEP_ONE = """
<h2>Step 1 of 3: about you</h2>
<form method="post" action="/signup/details">
  <p><label for="full_name">Full name</label>
     <input id="full_name" name="full_name" type="text" value="%s"></p>
  <p><label for="email">Email address</label>
     <input id="email" name="email" type="text" value="%s"></p>
  <p><button type="submit">Continue</button></p>
</form>
"""

STEP_TWO = """
<h2>Step 2 of 3: where you live</h2>
<form method="post" action="/signup/address">
  <p><label for="street">Street</label>
     <input id="street" name="street" type="text" value="%s"></p>
  <p><label for="city">Town or city</label>
     <input id="city" name="city" type="text" value="%s"></p>
  <p><button type="submit">Continue</button></p>
</form>
"""


def step_three(d):
    return """
<h2>Step 3 of 3: check your details</h2>
<p>Name: <span id="review-name">%s</span></p>
<p>Email: <span id="review-email">%s</span></p>
<p>Street: <span id="review-street">%s</span></p>
<p>Town or city: <span id="review-city">%s</span></p>
<form method="post" action="/signup/create">
  <p><button type="submit">Create account</button></p>
</form>
""" % (html.escape(d.get("full_name", "")), html.escape(d.get("email", "")),
       html.escape(d.get("street", "")), html.escape(d.get("city", "")))


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # the eval reads stdout; a request log per click would bury it

    # ── plumbing ──────────────────────────────────────────────────────────
    def _sid(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sid" and value in SESSIONS:
                return value
        return None

    def _send(self, body, status=200, headers=None):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location, headers=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _ensure_sid(self):
        """A draft needs somewhere to live, so the first page issues the cookie."""
        sid = self._sid()
        if sid:
            return sid, {}
        sid = uuid.uuid4().hex
        SESSIONS.add(sid)
        return sid, {"Set-Cookie": "sid=%s; Path=/" % sid}

    # ── pages ─────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/":
            sid, extra = self._ensure_sid()
            clear(sid)
            return self._send(page("Sign up", STEP_ONE % ("", "")), headers=extra)

        sid = self._sid()
        if not sid:
            return self._redirect("/")
        d = draft(sid)

        if path == "/signup/address":
            return self._send(page("Your address",
                                   STEP_TWO % (html.escape(d.get("street", "")),
                                               html.escape(d.get("city", "")))))
        if path == "/signup/review":
            return self._send(page("Check your details", step_three(d)))
        if path == "/signup/done":
            return self._send(page("Welcome", """
<h2>Account created</h2>
<p id="welcome">Account created for <span id="done-name">%s</span>
 (<span id="done-email">%s</span>).</p>
""" % (html.escape(d.get("full_name", "")), html.escape(d.get("email", "")))))

        return self._send(page("Not found", "<p>No such page.</p>"), status=404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        sid, extra = self._ensure_sid()
        form = self._form()

        if path == "/signup/details":
            remember(sid, {"full_name": (form.get("full_name") or "").strip(),
                           "email": (form.get("email") or "").strip()})
            return self._redirect("/signup/address", extra)

        if path == "/signup/address":
            remember(sid, {"street": (form.get("street") or "").strip(),
                           "city": (form.get("city") or "").strip()})
            return self._redirect("/signup/review", extra)

        if path == "/signup/create":
            # Nothing is actually created; the draft is what the journey is about.
            return self._redirect("/signup/done", extra)

        return self._send(page("Not found", "<p>No such page.</p>"), status=404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("listening on http://127.0.0.1:%d" % args.port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
