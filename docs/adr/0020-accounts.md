# 0020. Accounts: friends on the same server, by invitation

- Status: Proposed
- Date: 2026-11-11

## Context

The server has one account, created at first-run setup. The maintainer wants a
friend to use the same server: their own seeds, presets, Switches and paired
Switch app, and to race or share seeds with them.

Most of the data is already per account: seeds, jobs, presets, saved Switches,
passkeys and paired devices all carry an `owner_id` or `user_id`, and every route
filters by the signed-in account. Two things are server-wide today:

- **Game files.** One RomFS folder (`DREADOVANIA_ROMFS_DIR`, or `data/romfs`),
  uploaded or mounted by the maintainer, patches every seed.
- **Who may sign in.** There is no way to add a second account.

The project's line (PLAN.md section 3): game files come from the user's own
dump, and patched output is for personal use. A patched seed contains modified
copies of game files, so handing one account's patched files to another would be
the server distributing game content.

## Decision

1. **Invite links, no sign-up.** The admin creates a one-time invite on the
   Account page: a random token (hashed in the database, like pairing codes),
   valid for 7 days, shown once as a link. The friend opens it and chooses a
   username and password, then can add a passkey as now. An invite can be
   revoked before it is used. There is never an open sign-up form.
2. **Two roles: admin and member.** The account created at setup is the admin.
   Invited accounts are members: they have everything the admin has for their
   own data (seeds, presets, the editor, game files, Switches, the Switch app),
   but only the admin can create or revoke invites, list accounts, and remove an
   account (which deletes its seeds, presets, game files, Switches, passkeys and
   devices). Server settings stay in environment variables, as now.
3. **Each account uploads its own game files.** Game files move from one
   server-wide folder to one per account, under `data/accounts/<account id>/romfs/`.
   Patching a seed uses only its owner's files. The upload page, the version
   check and the "game files aren't ready" messages work per account.
   - The existing folder becomes the admin's: on upgrade, `data/romfs` (or the
     mounted `DREADOVANIA_ROMFS_DIR`) is the admin's game files, unchanged. A
     mounted folder can only be the admin's; members upload theirs.
   - No account can read, patch with, or download from another account's files.
     Tests check this, as they do for seeds.
   - Disk use: a few GB per account that patches seeds. The Game files page
     shows each account its own size; the admin's account list shows each
     account's total.
   - Amended while implementing it (accounts part 2): this said
     `data/romfs/<account id>/`, but `data/romfs` is the admin's RomFS itself.
     Folders inside it would sit among the admin's game files, and installing a
     new upload replaces that whole folder. Members' folders are therefore under
     `data/accounts/`, with their uploads beside them.
4. **Seeds stay private; share by permalink.** Nothing about another account is
   visible. To play the same seed, one person copies a seed's permalink and the
   other generates from it; each patches it with their own game files. The server
   already stores every seed's permalink; the seed page will show it with a
   "Copy permalink" button.
5. **Limits are per account.** Queue limits, rate limits and storage limits apply
   to each account separately, so one account can't fill the queue for another.
   Generation and patching still run one job at a time server-wide (the runner's
   existing concurrency), in order.
6. **The Switch app needs no change.** A device token already belongs to one
   account; a friend pairs their own Switch from their own Account page.

## Pull requests

1. This ADR.
2. Roles and invites: the admin role (migration), invite create/revoke/accept,
   the accounts list and removal, with the API routes and tests that members
   can't reach any of it.
3. Per-account game files: the per-account RomFS folder and upload, the
   upgrade path for the existing folder, patching with the owner's files, and
   the isolation tests.
4. Sharing: the seed's permalink and a "Copy permalink" button on the seed
   page, and a short guide in the docs for racing a friend.

## Consequences

- Upgrading keeps everything working for the existing single account: it
  becomes the admin, and its game files stay where they are.
- Each member needs their own Dread dump to patch seeds. Without one, a member
  can still generate seeds (and export presets), but not patch or install them.
- Several accounts patching at once queue behind each other; patching is CPU-
  and disk-heavy, so this is deliberate.
- Removing an account deletes its data, including its game files, after a
  confirmation step.
