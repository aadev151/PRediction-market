# Notes service: public specification

> FIXTURE. This service exists so PRediction Market can be developed and tested end to end. This spec is public: traders may read
> it, and the hidden acceptance tests are written from this file alone. Anything not stated here is unspecified.

## 1. Packaging and construction

- The package is `notes_app`, at the root of this repository. With the repository root on `PYTHONPATH`:
  `from notes_app import create_app`.
- `create_app(admin_token: str, clock: Callable[[], float] | None = None) -> fastapi.FastAPI`
  - `admin_token`: the secret that authorizes admin requests. An empty string raises `ValueError`.
  - `clock`: a zero-argument callable returning the current time in **seconds** as a number. It is used only by the rate limit
    (section 4) and is called once per `POST /notes` request that passes validation. Default: `time.monotonic`.
    Tests may pass a fake clock whose value they control.
- Every call to `create_app` returns an independent app with its own empty, in-memory store and its own rate-limit state.
  Nothing persists across apps or restarts.
- The app is exercised with `fastapi.testclient.TestClient(app)` (or any ASGI client). All request and response bodies are JSON.

## 2. Data

A **note** is a JSON object with exactly these keys:

| Key | Type | Notes |
|---|---|---|
| `id` | integer | Assigned by the service. The first note created by an app gets `1`, and each later note gets the previous id + 1. Ids are never reused, even after a delete. |
| `title` | string | 1 to 100 characters inclusive. |
| `body` | string | 0 to 2000 characters inclusive. Defaults to `""`. |

Lengths count Unicode characters. Strings are stored and returned exactly as sent (no trimming or normalization).

## 3. Endpoints

Error responses are JSON objects with a `detail` key. Where this spec gives a `detail` string, it is exact. For other 422
responses, only the status code is specified.

### `GET /health`
Always `200` with `{"status": "ok"}`. No auth.

### `POST /notes`: create a note
- Request body: a JSON object with
  - `title`: required, a JSON string of 1 to 100 characters;
  - `body`: optional, a JSON string of at most 2000 characters (default `""`).
- Any of the following gets `422` and creates nothing: a missing `title`; a `title` or `body` that is not a JSON string (numbers
  and `null` included); a `title` of 0 or more than 100 characters; a `body` of more than 2000 characters; **any key other than
  `title` and `body`**; a request body that is not a JSON object or not valid JSON.
- Rate limit (section 4): a valid request from a client that is over the limit gets `429` with
  `{"detail": "rate limit exceeded"}` and creates nothing.
- Success: `201` with the created note, e.g. `{"id": 1, "title": "Groceries", "body": ""}`.
- No auth required.

### `GET /notes/{id}`: read a note
- `200` with the note.
- `404` with `{"detail": "note not found"}` if no note has that id (never created, or deleted). This includes `0` and negative ids.
- `422` if `{id}` is not an integer.
- No auth required.

### `GET /notes?limit=N`: list notes
- Returns `200` with a JSON array of notes in **ascending id order**: the first `N` existing notes (all of them if fewer than `N` exist).
  Deleted notes are not listed.
- `limit` is optional, default `20`. It must be an integer from 1 to 100 inclusive. `0`, negative values, values above 100, and
  non-integers get `422`.
- No auth required.

### `DELETE /notes/{id}`: delete a note (admin)
- Requires admin auth (section 5). **Auth is checked first**: a request without valid admin auth gets `401` with
  `{"detail": "unauthorized"}`, whether or not the note exists and whether or not `{id}` is a valid integer.
- With valid admin auth:
  - `204` with an empty body if the note existed. It is removed: a later `GET /notes/{id}` returns `404`, and a second `DELETE` returns `404`.
  - `404` with `{"detail": "note not found"}` if no note has that id.
  - `422` if `{id}` is not an integer.

### `GET /admin/stats`: counters (admin)
- Requires admin auth (section 5). Without it: `401` with `{"detail": "unauthorized"}`.
- With it: `200` with `{"note_count": <notes that exist now>, "total_created": <notes ever created>, "total_deleted": <notes ever deleted>}`.
  Requests rejected with 4xx don't change these counters.

Any other method on these paths (for example `PUT /notes/1`) gets `405`. Unknown paths get `404`.

## 4. Rate limit on `POST /notes`

- Each client may have at most **10 accepted** `POST /notes` requests in any **60-second** sliding window.
- A client is identified by its connection's host (`request.client.host`). With `TestClient(app)` every request comes from the
  same client; `TestClient(app, client=("10.0.0.2", 50000))` simulates a different one. Clients are limited independently.
- Only **accepted** requests (`201`) count. Requests rejected with `422` or `429` don't count.
- Order of checks: body validation first, then the rate limit. An invalid body gets `422` even when the client is over the limit.
- Precisely: let `t` be the clock value read for a valid request. Count that client's earlier accepted requests whose clock
  value `s` satisfies `t - s < 60`. If the count is 10 or more, respond `429`; otherwise accept and record `t`.
  - Example: with the clock fixed at `0`, requests 1 to 10 get `201` and request 11 gets `429`. At clock `59.999` it's still `429`.
    At clock `60` the ten requests from time `0` no longer count, so the next request gets `201`.
- The rate limit applies only to `POST /notes`.

## 5. Admin auth

- Header: `Authorization: Bearer <admin_token>`, where `<admin_token>` is exactly the string passed to `create_app`.
- The scheme word `Bearer` is case-insensitive (`bearer` works). It is followed by exactly one space and then the token, which is
  compared exactly (case-sensitive, no trimming).
- `401` with `{"detail": "unauthorized"}` when the header is missing, uses another scheme (e.g. `Basic`), has no token, or has the wrong token.
- Only `DELETE /notes/{id}` and `GET /admin/stats` require admin auth. The other endpoints ignore the `Authorization` header.
