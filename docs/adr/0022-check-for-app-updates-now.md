# 0022. The Switch app can ask for an app update check now

- Status: Proposed
- Date: 2026-11-11
- Amends: [0021](0021-app-updates.md) decision 1 (how often GitHub is asked)

## Context

ADR 0021 has the server ask GitHub for the latest app release at most every 6
hours, and answer the app from that. After a release, an app could wait up to 6
hours to see it, and the only way to hurry it was restarting the server. The
maintainer asked for a way to check for updates from the app.

GitHub allows 60 unauthenticated API requests an hour from one address, and a
check uses a few of them (the release, and for a new release its checksum,
attestation and file).

## Decision

1. `GET /api/v1/app/latest` takes `check=true` to ask GitHub now rather than use
   the last answer. The server still asks GitHub **at most every 5 minutes**,
   whoever asks; a request within that time gets the last answer. Without
   `check`, nothing changes: the answer is at most 6 hours old.
2. The app asks with `check=true` only when the user presses its **Check for
   updates** button, never on its own. It says what it found: the newer version,
   or that it has the latest.
3. Everything else in ADR 0021 stands: only a release whose checksum and
   build-provenance attestation verify is offered, and nothing downloads until
   the user presses Update.

## Consequences

- A new release reaches the Switch within minutes, at the user's request.
- At most 12 checks an hour from the server, well inside GitHub's limit even with
  several accounts' Switches pressing the button.
- An older server ignores `check` and answers from its last check.
