# 0028. Publish arm64 images: build py-tegra-swizzle from source, each platform natively

- Status: Proposed
- Date: 2026-11-19
- Supersedes: [ADR 0003](0003-x86-first-arm64-ready.md)'s first decision ("Publish
  `linux/amd64` only for now"), and settles [ADR 0010](0010-phase0-randovania-findings.md)
  decision 5 (arm64 deferred) with its option 2. ADR 0003's other decisions stand.

## Context

ADR 0003 kept the project arm64-ready, and the lockfile resolves for both
platforms. One dependency stood in the way of an arm64 image, as ADR 0010 found:
`py-tegra-swizzle` (needed by `mercury-engine-data-structures`, the Dread
file-format library) has never published a Linux arm64 wheel. Its source package
is Rust, and the image's build stage had no Rust compiler. The weekly arm64 test
build failed for exactly that reason on 2026-11-09 and 2026-11-16, while the
tests themselves passed on the arm64 runner, which happens to have Rust.

The source package (0.4.3, hash-pinned in `uv.lock`) carries its own `Cargo.lock`
with checksums for its 35 crates. It compiled and round-tripped data correctly in
a test build on x86-64, with the build tools pinned below.

## Decision

1. **Publish `linux/amd64` and `linux/arm64`,** as one multi-arch image under the
   same tags.
2. **Build `py-tegra-swizzle` from source on arm64 only** (ADR 0010, option 2):
   - **Rust:** the build stage installs `cargo` and `rustc` from Debian's signed
     archive, when `TARGETARCH=arm64`. The runtime stage never has them.
   - **Crates:** pinned by the source package's `Cargo.lock`. Cargo checks each
     crate's checksum.
   - **Python build tools** (`setuptools`, `setuptools-scm`, `setuptools-rust`,
     `semantic-version`): pinned in `pyproject.toml` with uv's
     `build-constraint-dependencies`, since the lockfile doesn't cover build
     tools.
   - **amd64** keeps using the published wheel.
3. **Each platform is built natively.** Releases build amd64 on an x86 runner and
   arm64 on GitHub's arm64 runner, in parallel, not arm64 under emulation, where
   compiling Rust would take many times longer. Each platform image is pushed
   untagged, scanned and given its own SBOM. Only then are they combined into one
   index, tagged, signed (`cosign sign --recursive`, so the index and each
   platform image) and attested (build provenance on the index, each SBOM on its
   platform image). One approval, on a first job, still comes before anything is
   pushed.
4. **Caching, inside the trust boundary:**
   - **In the build:** uv's cache keeps each compiled wheel, keyed by the source
     package's hash, and Cargo's registry cache keeps downloaded crates, so a
     rebuild compiles nothing it compiled before. These caches are local to the
     machine that builds.
   - **In CI:** PR and main builds keep a GitHub Actions layer cache, one scope
     per platform, so an unchanged dependency layer takes seconds.
   - **In releases: no cache at all,** as before, so nothing from a PR run can
     reach a published image. Native builds keep releases fast without it.
5. **CI builds and smoke-tests arm64 on every PR,** as "Container image (arm64)",
   next to the required "Container image". The smoke test also loads the compiled
   module and round-trips data through it. The weekly arm64 test build stays, to
   catch upstream drift between PRs, and only runs on `main` report to its
   tracking issue.

## Consequences

- People on arm64 (Raspberry Pi, ARM NAS) can run the published image.
- `py-tegra-swizzle`'s Rust code is old (`pyo3` 0.13.2, `memmap` 0.7). The same
  code already runs on amd64, from upstream's wheel. If upstream ever publishes
  arm64 wheels (ADR 0010, option 1, still worth asking for), the source build and
  Rust can go.
- The build tools are pinned by version but not by hash: uv's build constraints
  don't take hashes. A compromised release of one of those four versions would
  reach the arm64 build stage, and only there.
- "Container image (arm64)" isn't a required check. Adding it to
  `.github/rulesets/protect-main.json` is the maintainer's decision.
- Releases need GitHub's arm64 runners, which are free for public repositories.
