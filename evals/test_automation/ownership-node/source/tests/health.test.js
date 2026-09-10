// The one test that already exists, and the reason it exists: it is the *idiom* a generated
// suite has to match — Node's own test runner, a server on an ephemeral port, `fetch`, and
// the store reset between tests. A generator shown this and still inventing jest, supertest
// and a `request(app)` helper has not read the project it was given.
const test = require('node:test');
const assert = require('node:assert');
const { createServer } = require('../src/app');
const store = require('../src/store');

// Start the app on a free port, run the body against it, always shut it down.
async function withServer(fn) {
  store.reset();
  const server = createServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await fn(base);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test('health check answers', async () => {
  await withServer(async (base) => {
    const res = await fetch(`${base}/health`);
    assert.strictEqual(res.status, 200);
    assert.deepStrictEqual(await res.json(), { status: 'ok' });
  });
});

test('a task can be created and read back by its owner', async () => {
  await withServer(async (base) => {
    const created = await fetch(`${base}/tasks`, {
      method: 'POST',
      headers: { 'x-user': 'alice', 'content-type': 'application/json' },
      body: JSON.stringify({ title: 'write the tests' }),
    });
    assert.strictEqual(created.status, 201);
    const { task } = await created.json();

    const read = await fetch(`${base}/tasks/${task.id}`, { headers: { 'x-user': 'alice' } });
    assert.strictEqual(read.status, 200);
    assert.strictEqual((await read.json()).task.title, 'write the tests');
  });
});

module.exports = { withServer };
