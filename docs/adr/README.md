# Architecture Decision Records

Short records of significant decisions. Each one has a status, the context, the
decision itself, and its consequences. To change a decision, add a new ADR that
supersedes the old one; don't rewrite history.

| # | Title | Status |
|---|---|---|
| [0001](0001-dread-only.md) | Support Metroid Dread only | Accepted |
| [0002](0002-randovania-integration.md) | Integrate Randovania via its CLI, with one adapter module | Accepted |
| [0003](0003-x86-first-arm64-ready.md) | x86-64 first, arm64-ready | Accepted |
| [0004](0004-auth.md) | Single user now, multi-user-ready authentication | Accepted |
| [0005](0005-tls-lets-encrypt.md) | TLS via user-supplied certificates and Let's Encrypt DNS-01 | Accepted |
| [0006](0006-switch-delivery.md) | Switch delivery: zip, FTP push, SimpleModManager-compatible layout | Accepted |
| [0007](0007-separate-nro-repo.md) | Switch app lives in a separate repository | Accepted |
| [0008](0008-ci-supply-chain.md) | CI/CD supply-chain baseline | Accepted |
| [0009](0009-claude-authorship-and-dco.md) | Claude authorship, maintainer sign-off (DCO) | Accepted |
| [0010](0010-phase0-randovania-findings.md) | Phase 0: Randovania runs headless through its CLI | Accepted |
| [0011](0011-phase1-plan.md) | Phase 1 plan: jobs, generation API, sign-in, first screens | Accepted |
| [0012](0012-signoff-by-comment.md) | Sign off and merge with a `/signoff` comment | Accepted |
| [0013](0013-phase2-patching.md) | Phase 2 plan: patching seeds with the user's own game files | Accepted |
| [0014](0014-release-by-comment.md) | Release with a `/release` comment | Accepted (amended by 0015) |
| [0015](0015-release-app.md) | Release tags are created by the maintainer's release app | Accepted |
| [0016](0016-phase3-send-to-switch.md) | Phase 3 plan: sending a patched seed to the Switch over FTP | Accepted |
| [0017](0017-device-pairing.md) | Pairing the Switch app: codes, device tokens and what they may do | Accepted |
| [0018](0018-switch-app-download.md) | The Switch app downloads a patched seed as a manifest and one tar | Accepted |
