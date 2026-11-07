# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** via
[GitHub private vulnerability reporting](https://github.com/nolsen311/dreadovania-server/security/advisories/new).
Don't open a public issue for security problems.

You can expect an acknowledgement within a week. Fixes are released as a new
image version, and the advisory is published once a fix is available.

## Supported versions

Only the latest release receives security fixes.

## Verifying a release image

Every release image on GHCR is built from a version tag by
[`.github/workflows/release.yml`](.github/workflows/release.yml), scanned before
it is tagged, signed with Sigstore keyless signing, and published with a SLSA
build-provenance attestation and an SPDX SBOM.

Pin images by digest, and verify a digest before you deploy it:

```bash
gh attestation verify oci://ghcr.io/nolsen311/dreadovania-server@sha256:<digest> --repo nolsen311/dreadovania-server
```

```bash
cosign verify ghcr.io/nolsen311/dreadovania-server@sha256:<digest> \
  --certificate-identity-regexp '^https://github.com/nolsen311/dreadovania-server/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Deployment guidance

- Run the container with the hardening in [`compose.yaml`](compose.yaml).
- Don't expose the server directly to the internet. Use it on your LAN, or behind
  a TLS reverse proxy (see docs/adr/0005-tls-lets-encrypt.md).
- The image contains no game content. Never share your game files or keys.
