// The whole data layer. In memory on purpose: this app exists to be a subject for the
// test-automation eval, and a real database would make the eval measure Postgres.
const tasks = new Map();
let nextId = 1;

function reset() {
  tasks.clear();
  nextId = 1;
  create('alice', 'Alice first task');
  create('bob', 'Bob first task');
}

function create(ownerId, title) {
  const id = String(nextId++);
  const task = { id, ownerId, title, done: false };
  tasks.set(id, task);
  return task;
}

function get(id) {
  return tasks.get(String(id)) || null;
}

function update(id, fields) {
  const task = get(id);
  if (!task) return null;
  if (typeof fields.title === 'string') task.title = fields.title;
  if (typeof fields.done === 'boolean') task.done = fields.done;
  return task;
}

function remove(id) {
  return tasks.delete(String(id));
}

function all() {
  return [...tasks.values()];
}

module.exports = { reset, create, get, update, remove, all };
