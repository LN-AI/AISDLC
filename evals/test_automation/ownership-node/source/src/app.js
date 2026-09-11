// A tiny task API, deliberately holding one real bug: PUT and DELETE check that the caller
// is *somebody*, and never that the caller is the *owner*. Any signed-in user can edit or
// delete anyone else's task.
//
// Node's http module and nothing else. No dependencies at all, so the eval that uses this
// app measures the test generator rather than an npm install.
const http = require('http');
const store = require('./store');

function send(res, status, body) {
  const payload = JSON.stringify(body === undefined ? {} : body);
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(payload);
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch (e) {
        resolve({});
      }
    });
  });
}

// Authentication, such as it is: the caller names themselves in a header. Enough to have
// an identity to check against, without dragging in a JWT library.
function userFrom(req) {
  const user = req.headers['x-user'];
  return typeof user === 'string' && user.trim() ? user.trim() : null;
}

async function handle(req, res) {
  const url = new URL(req.url, 'http://localhost');
  const parts = url.pathname.split('/').filter(Boolean);

  if (req.method === 'GET' && url.pathname === '/health') {
    return send(res, 200, { status: 'ok' });
  }

  if (parts[0] !== 'tasks') {
    return send(res, 404, { error: 'not found' });
  }

  const userId = userFrom(req);
  if (!userId) {
    return send(res, 401, { error: 'authentication required' });
  }

  // GET /tasks — everything the caller owns.
  if (req.method === 'GET' && parts.length === 1) {
    return send(res, 200, { tasks: store.all().filter((t) => t.ownerId === userId) });
  }

  // POST /tasks
  if (req.method === 'POST' && parts.length === 1) {
    const body = await readBody(req);
    if (!body.title) return send(res, 400, { error: 'title is required' });
    return send(res, 201, { task: store.create(userId, body.title) });
  }

  if (parts.length !== 2) {
    return send(res, 404, { error: 'not found' });
  }
  const task = store.get(parts[1]);
  if (!task) {
    return send(res, 404, { error: 'no such task' });
  }

  // GET /tasks/:id — this one does check ownership.
  if (req.method === 'GET') {
    if (task.ownerId !== userId) return send(res, 403, { error: 'not yours' });
    return send(res, 200, { task });
  }

  // PUT /tasks/:id — THE BUG. Authenticated is treated as authorised.
  if (req.method === 'PUT') {
    const body = await readBody(req);
    return send(res, 200, { task: store.update(task.id, body) });
  }

  // DELETE /tasks/:id — the same bug again.
  if (req.method === 'DELETE') {
    store.remove(task.id);
    return send(res, 204);
  }

  return send(res, 405, { error: 'method not allowed' });
}

function createServer() {
  return http.createServer((req, res) => {
    handle(req, res).catch(() => send(res, 500, { error: 'internal' }));
  });
}

module.exports = { createServer, handle };
