# 0011. Phase 1 plan: jobs, generation API, sign-in, first screens

- Status: **Proposed** (becomes Accepted once the open questions below are answered)
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
| `GET /jobs/{id}`, `GET /jobs/{id}/events`, `DELETE /jobs/{id}` | status, live progress (SSE), cancel |

- **Errors** use RFC 9457 `application/problem+json`.
- **Input validation:** permalinks must match Randovania's character set and a
  length limit before they reach the subprocess; seed numbers are bounded
  integers; uploaded presets are parsed as JSON and checked for the Dread game
  before being stored.
- `docs/openapi.json` is regenerated in each PR that changes the API (ADR 0007).

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

### 5. Pull requests, in order

1. **Storage and job runner.** Database, migrations, the job queue and the
   Randovania subprocess runner. No HTTP endpoints yet. Fully testable here.
2. **Sign-in.** Setup token, setup wizard API, password, sessions, CSRF, rate
   limiting. After this, every endpoint added later is protected by default.
3. **Generation API.** Presets, seeds and jobs endpoints from section 3, with SSE.
4. **Mobile UI.** The first screens: setup, sign-in, generate, progress, seed
   history and details, and spoiler reveal. The technology depends on open
   question 1.
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

## Open questions for the maintainer

1. **UI technology.** The plan (§4 of `docs/PLAN.md`) says "static PWA (Svelte or Preact)".
   - **A. Server-rendered pages (Jinja templates) with htmx.** *(Recommended.)*
     No Node.js toolchain or npm dependency tree at all, which is a large
     supply-chain reduction. htmx is one pinned, vendored file. It works well on
     mobile, supports SSE progress, and can still be installed as a PWA. Passkeys
     later need only a small hand-written script.
   - **B. A Svelte single-page app,** built in CI. Richer client-side
     interactivity, but it adds Node.js, npm, and a lockfile with hundreds of
     transitive packages to audit and keep updated.
2. **Spoilers by default.** Should a normal seed keep its spoiler log
   (hidden behind a reveal step, as planned), or should **race mode** (no
   spoiler at all) be the default?
3. **Seed retention.** Keep every seed until you delete it (proposed), or keep
   only the newest *N*?
4. **Order.** Is PR 5 (TLS and passkeys) fine after the UI, or do you want
   passkeys before you start using the UI?

## Consequences

- PRs 1–3 can be built and fully tested in Claude Code sessions; PR 4 benefits
  from you trying it on your phone.
- New runtime dependency: `argon2-cffi` (PR 2). Option A adds htmx as a
  vendored static file (PR 4); option B adds the Node.js build toolchain.
- If answer 1 is A, `docs/PLAN.md` §4's "static PWA (Svelte or Preact)" is
  superseded by this ADR.
