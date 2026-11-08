# 0011. Phase 1 plan: jobs, generation API, sign-in, first screens

- Status: Accepted (open questions answered by the maintainer on 2026-11-07; see the end of this ADR)
- Date: 2026-11-07
- Builds on ADR 0004 (auth), 0005 (TLS), 0007 (API contract), 0010 (Phase 0).

## Context

Phase 0 showed that Randovania generates Dread seeds headless through its CLI
in about 17–26 s and about 150 MB, CPU-bound (ADR 0010). Phase 1 turns that into a
usable server: generate seeds from a phone, see progress, keep a history, sign
in safely. No game files are involved; patching is Phase 2.

This ADR fixes the shapes that later work depends on (storage, jobs, API,
sign-in, security limits) and how the work is split into pull requests.

## Decision

### 1. Storage

- **SQLite** at `/data/dreadovania.db` (WAL mode), using the standard library
  `sqlite3` module and plain SQL. One container and one user need no database
  server, and no ORM keeps the dependency list short.
- **Migrations:** numbered SQL files in `src/dreadovania/migrations/`, applied
  in order at startup inside a transaction; the applied version is stored
  in the database. Migrations only ever move forward.
- **Files:** generated seeds live under `/data/seeds/<owner_id>/<seed_id>/`
  (`seed.rdvgame`, plus the permalink and metadata in the database). Paths are
  built only from server-generated IDs, never from user input.
- **IDs:** random UUIDv4 for everything visible in URLs, never sequential.
- **Every row carries `owner_id`** (ADR 0004), and every query filters on it.

| Table | Purpose |
|---|---|
| `users` | the single account now; more later (ADR 0004) |
| `credentials` | password hash (Argon2id), passkeys later |
| `sessions` | hashed session tokens, expiry, last use |
| `setup_tokens` | one-time first-run token, hashed |
| `presets` | user-imported `.rdvpreset` files |
| `seeds` | permalink, hash, preset name, Randovania version, race flag, status |
| `jobs` | queued/running/finished work, progress, error, timestamps |

### 2. Job runner

- **One worker, in-process.** Generation is CPU-bound, so jobs run one at a time.
  The queue is the `jobs` table, so it survives restarts. On startup, a job left
  `running` is marked `failed` (interrupted); it is never silently re-run.
- **Randovania runs as a subprocess** (ADR 0010):
  `python -m randovania layout generate-from-presets --status-update …` or
  `generate-from-permalink`. It's called with an argument list (no shell), a
  timeout (default 10 minutes), and a lowered priority (`nice`) so the
  web server stays responsive. The output file is written into a
  per-job temporary directory, validated, and only then moved into place.
- **Progress:** `--status-update` lines are parsed into a progress message and
  sent over **Server-Sent Events** (`GET /api/v1/jobs/{id}/events`). Polling
  `GET /api/v1/jobs/{id}` works too, for clients without SSE.
- **Cancel:** `DELETE /api/v1/jobs/{id}` kills the subprocess and cleans up.
- **Limits:** at most 5 queued jobs per user (configurable), so a stuck client
  can't fill the queue.

### 3. API (`/api/v1`, all JSON, all behind sign-in)

| Method and path | Purpose |
|---|---|
| `GET /presets` | Randovania's bundled Dread presets plus the user's imported ones |
| `POST /presets` | import a `.rdvpreset` (max 1 MB; must be valid JSON and a Dread preset) |
| `GET /presets/{id}`, `DELETE /presets/{id}` | read or delete an imported preset |
| `POST /seeds` | start generation from `{preset_id}` or `{permalink}`, optional `seed_number` and `race`. Returns **202** with `seed_id` and `job_id` |
| `GET /seeds`, `GET /seeds/{id}` | history and details: permalink, hash, preset, Randovania version, status |
| `GET /seeds/{id}/rdvgame` | download the `.rdvgame` |
| `GET /seeds/{id}/spoiler` | the spoiler log: a **separate request**, so the UI can only show it after an explicit reveal; refused for race seeds |
| `DELETE /seeds/{id}` | delete a seed and its files |
| `POST /seeds/purge` | bulk delete, with an **explicit scope** in the body: `{"scope": "all"}` or `{"scope": "older_than", "days": N}`. Returns the count deleted. There is deliberately no bare `DELETE /seeds`, so a mistaken request can't wipe everything |
| `GET /jobs/{id}`, `GET /jobs/{id}/events`, `DELETE /jobs/{id}` | status, live progress (SSE), cancel |

- **Errors** use RFC 9457 `application/problem+json`.
- **Input validation:** permalinks must match Randovania's character set and a
  length limit before they reach the subprocess; seed numbers are bounded
  integers; uploaded presets are parsed as JSON and checked for the Dread game
  before being stored.
- **Permalinks are passed as one argument, `--permalink=VALUE`.** `-` is part of
  the permalink alphabet, so a value such as `--race` passes validation; as a
  single argument it can only ever be read as data, never as a flag (PR 1,
  covered by tests including one against real Randovania).
- `docs/openapi.json` is regenerated in each PR that changes the API (ADR 0007).
- **As built in PR 3:**
  - Imported presets are stored in the database (max 1 MB each) and have IDs
    `user:<uuid>`; bundled presets keep their file names (`starter_preset`).
    `POST /presets` takes the `.rdvpreset` JSON as the request body, read with a
    hard size limit, so no multipart parser is needed. A preset deleted before
    its job runs fails that job cleanly.
  - A normal seed's `.rdvgame` **is** the full spoiler, while a race seed's has
    none (ADR 0010). `GET /seeds/{id}/spoiler` returns pickup locations and the
    starting state for the UI's reveal step, and 409 for race seeds.
    `GET /seeds/{id}/rdvgame` is an explicit download and says so.
  - Seeds record the preset's display name at generation time, so history stays
    readable after a preset is deleted.
  - Purge skips seeds still being generated and reports how many it skipped.
  - SSE sends one `job` event per change and a keep-alive comment every 15 s,
    and ends when the job does.

### 4. Sign-in (implements ADR 0004)

- **First run:** if no user exists, the server creates a one-time setup token,
  stores only its hash, and prints the setup link to the container log. It
  expires after 1 hour, and restarting prints a fresh one. Until setup is done,
  every API call except setup returns 503.
- **Setup wizard** collects: username, public URL (ADR 0005), and a password.
- **Password:** Argon2id (`argon2-cffi`, a new dependency), with a minimum
  length and no other composition rules.
- **Sessions:** a random 256-bit token in a cookie (`HttpOnly`, `SameSite=Lax`,
  `Secure` whenever the public URL is HTTPS). Only its hash is stored. Lifetime
  90 days, renewed as you use it; sign-out deletes it on the server.
- **CSRF:** every state-changing request must carry an `Origin` header that
  matches the configured public URL, on top of `SameSite=Lax`.
- **Rate limiting:** login and setup attempts are limited per client IP and
  overall, in memory, with increasing delays.
- **Passkeys and device tokens** are designed into the tables now but built later:
  passkeys with the TLS work (step 5 below), device tokens in Phase 4.
- **As built in PR 2:**
  - The setup token goes in the link's URL fragment (`/setup#token=…`), which
    browsers never send to the server, so it can't reach access or proxy logs.
  - Passwords: 12–1024 characters; Argon2id with the library defaults, rehashed
    on login if the defaults change. Unknown usernames are checked against a dummy
    hash, so failures take the same time and return the same message.
  - The `Origin` rule: cookie-authenticated changes **must** send a matching
    `Origin`. Login and setup allow a missing `Origin` (a non-browser client
    can't be tricked into a cross-site request) but refuse a mismatched one;
    setup must come from the public URL being saved.
  - A public URL set in `DREADOVANIA_PUBLIC_URL` overrides the one saved by setup.
  - Limits per 15 minutes: login 10 failures per client and 100 overall; setup
    10 attempts per client and 30 overall. Over the limit: 429 with `Retry-After`.
  - Every `/api/v1` route is on a router that requires a session, except the
    public setup, login and info routes. A test enumerates the OpenAPI document
    so a new route can't be left unprotected by accident.

### 5. Pull requests, in order

1. **Storage and job runner.** Database, migrations, the job queue and the
   Randovania subprocess runner. No HTTP endpoints yet. Fully testable here.
2. **Sign-in.** Setup token, setup wizard API, password, sessions, CSRF, rate
   limiting. After this, every endpoint added later is protected by default.
3. **Generation API.** Presets, seeds and jobs endpoints from section 3, with SSE.
4. **Mobile UI.** Server-rendered with htmx (answer 1). The first screens:
   setup, sign-in, generate, progress, seed history and details, spoiler reveal,
   and purge (with a confirmation step).
   **As built:** Jinja templates and htmx 2.0.11, vendored after checking npm's
   published integrity and pinned by SHA-256 (`static/vendor/VENDOR.md`, checked
   by a test). htmx runs with `allowEval`, `allowScriptTags` and indicator styles
   off and `selfRequestsOnly` on, so the CSP stays `default-src 'self'`, and a
   test rejects inline scripts, styles and handlers in templates. Forms are plain
   URL-encoded, read with the standard library under a 16 KB limit (no multipart
   parser); preset upload is a small script posting JSON to the API. Progress is
   a 1-second poll of a status fragment that stops when the seed is done (SSE
   stays in the API for the Switch app). UI routes are excluded from OpenAPI and
   reuse the API's sign-in flows, stores and rules; a test checks that every
   state-changing UI route needs a session and a same-origin `Origin`. Purge
   shows how many seeds would be deleted and needs an explicit confirmation.
5. **TLS and passkeys.** Bring-your-own certificates with hot reload, a trusted-proxy
   allowlist and hostname redirect (ADR 0005), and then passkeys.

**No release tag is created until PR 4 is merged**, so no published image has
an API without a UI or sign-in.

### 6. Testing

- API and storage tests use a **fake runner** for speed and determinism.
- A small number of **integration tests run real Randovania** (the existing
  ADR 0010 tests plus one end-to-end "POST /seeds → job finishes → download"),
  so CI time stays reasonable.
- Security tests: unauthenticated access to every endpoint, cross-owner access,
  CSRF rejection, oversized and malformed uploads, permalink injection attempts.

## Maintainer decisions (2026-11-07)

1. **UI technology: A, server-rendered pages (Jinja templates) with htmx.** No
   Node.js toolchain or npm dependency tree. htmx is one pinned, vendored file
   checked against a recorded hash, served from our own origin so the strict
   CSP (`default-src 'self'`) is unchanged. The UI can still be installed as a
   PWA. This **supersedes** "static PWA (Svelte or Preact)" in `docs/PLAN.md` §4.
2. **Spoilers require a reveal step.** Normal seeds keep their spoiler log; the
   UI shows it only after an explicit reveal, and the API serves it only on its
   own endpoint. Race seeds (opt-in) have no spoiler.
3. **Keep all seeds until the user deletes them.** The user can delete one seed,
   or purge **all** seeds or **all seeds older than *N* days** (`POST /seeds/purge`
   with an explicit scope; the UI asks for confirmation and shows how many
   seeds will be deleted).
4. **Passkeys come after the UI** (PR 5), together with the TLS work.

## Consequences

- PRs 1–3 can be built and fully tested in Claude Code sessions; PR 4 benefits
  from you trying it on your phone.
- New dependencies: `argon2-cffi` (PR 2), `jinja2` (PR 4), and htmx as a vendored,
  hash-checked static file (PR 4). No Node.js toolchain.
