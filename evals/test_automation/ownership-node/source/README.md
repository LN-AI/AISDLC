# task-api

A very small task API. Tasks belong to the user who created them; the caller names
themselves in an `x-user` header.

| Route | What it does |
|---|---|
| `GET /health` | liveness |
| `GET /tasks` | the caller's own tasks |
| `POST /tasks` | create a task owned by the caller |
| `GET /tasks/:id` | one task, if the caller owns it |
| `PUT /tasks/:id` | update a task |
| `DELETE /tasks/:id` | delete a task |

Tests use Node's built-in test runner: `npm test`. No dependencies.
