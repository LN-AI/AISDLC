"""A tiny task tracker, served from the standard library and nothing else.

The subject of `evals/app_testing/task-ownership`. Dependency-free on purpose, for the reason
D-403 gives about the code-based corpus: a red result has to mean the tests failed, never
that an install did.

It is deliberately ordinary HTML — real labels, real buttons, real form posts — because the
thing under test is whether an explorer can read a page the way a screen reader does and act
on what it finds. A single-page app driven entirely by JavaScript would be measuring
something else.

    python app.py --port 8123
"""
import argparse
import html
import http.server
import urllib.parse
import uuid

from auth import may_edit

USERS = {"alice": "alice123", "bob": "bob123"}

TASKS = {
    "1": {"id": "1", "title": "Write the quarterly report", "owner": "alice",
          "notes": "Due Friday."},
    "2": {"id": "2", "title": "Book the venue", "owner": "bob",
          "notes": "Needs to seat forty."},
}

SESSIONS = {}


def page(title, body, user=None):
    nav = ('<nav><a href="/tasks">All tasks</a>'
           + (f' <span>Signed in as {html.escape(user)}</span>' if user else "")
           + "</nav>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body>
<h1>Task Tracker</h1>
{nav}
<main>{body}</main>
</body></html>"""


LOGIN_FORM = """
<h2>Sign in</h2>
<form method="post" action="/login">
  <p><label for="username">Username</label>
     <input id="username" name="username" type="text"></p>
  <p><label for="password">Password</label>
     <input id="password" name="password" type="password"></p>
  <p><button type="submit">Sign in</button></p>
</form>
<p>Try alice / alice123, or bob / bob123.</p>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ── plumbing ──────────────────────────────────────────────────────────
    def log_message(self, *_args):
        pass  # the eval reads stdout; a request log per click would bury it

    def _user(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sid":
                return SESSIONS.get(value)
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

    # ── pages ─────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        user = self._user()

        if path == "/":
            if not user:
                return self._send(page("Sign in", LOGIN_FORM))
            return self._redirect("/tasks")

        if path == "/tasks":
            if not user:
                return self._send(page("Sign in", LOGIN_FORM), status=200)
            rows = "".join(
                f'<li><a href="/tasks/{t["id"]}">{html.escape(t["title"])}</a>'
                f' — owned by {html.escape(t["owner"])}</li>'
                for t in TASKS.values()
            )
            return self._send(page("All tasks", f"<h2>All tasks</h2><ul>{rows}</ul>", user))

        if path.startswith("/tasks/"):
            task_id = path.split("/")[2]
            task = TASKS.get(task_id)
            if not task:
                return self._send(page("Not found", "<p>No such task.</p>", user), status=404)
            if not user:
                return self._send(page("Sign in", LOGIN_FORM))
            body = [
                f"<h2>{html.escape(task['title'])}</h2>",
                f"<p>Owned by {html.escape(task['owner'])}</p>",
                f"<p>{html.escape(task['notes'])}</p>",
            ]
            if may_edit(task, user):
                body.append(f"""
<h3>Edit this task</h3>
<form method="post" action="/tasks/{task['id']}/edit">
  <p><label for="title">Title</label>
     <input id="title" name="title" type="text" value="{html.escape(task['title'])}"></p>
  <p><button type="submit">Save changes</button></p>
</form>""")
            else:
                body.append('<p id="readonly-note">This task belongs to '
                            f'{html.escape(task["owner"])}. You can read it but not change it.</p>')
            return self._send(page(task["title"], "".join(body), user))

        return self._send(page("Not found", "<p>No such page.</p>", user), status=404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        user = self._user()

        if path == "/login":
            form = self._form()
            name, password = form.get("username", ""), form.get("password", "")
            if USERS.get(name) == password:
                sid = uuid.uuid4().hex
                SESSIONS[sid] = name
                return self._redirect("/tasks", {"Set-Cookie": f"sid={sid}; Path=/"})
            return self._send(page("Sign in", "<p>Those details were not recognised.</p>"
                                   + LOGIN_FORM), status=200)

        if path.startswith("/tasks/") and path.endswith("/edit"):
            task_id = path.split("/")[2]
            task = TASKS.get(task_id)
            if not task:
                return self._send(page("Not found", "<p>No such task.</p>", user), status=404)
            if not user:
                return self._send(page("Sign in", LOGIN_FORM))
            if not may_edit(task, user):
                return self._send(
                    page("Not allowed",
                         '<p id="refused">You cannot change a task you do not own.</p>', user),
                    status=403)
            form = self._form()
            title = (form.get("title") or "").strip()
            if title:
                task["title"] = title
            return self._redirect(f"/tasks/{task_id}")

        return self._send(page("Not found", "<p>No such page.</p>", user), status=404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"listening on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
