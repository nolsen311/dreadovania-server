# 0023. Secure remote access with client certificates

- Status: Proposed
- Date: 2026-11-11

## Context

A Dreadovania server is meant for a home network. The maintainer wants to reach
theirs from anywhere, safely, so that only devices they (or the friends they
invite) have set up can reach it at all. Sign-in, passkeys and device tokens
protect the data, but the whole application is still exposed to anyone who
finds the address.

Client certificates (mutual TLS) close that: the TLS connection is refused
unless the device presents a certificate the server's owner issued, before any
request reaches the application. Both kinds of client can present one:

- Browsers on phones and computers can install a certificate as a `.p12` file.
- The Switch app uses the console's TLS service through libnx; its curl loads a
  client certificate from a PKCS#12 file (`sslContextImportClientPki`; checked in
  the pinned devkitPro image).

**Who checks the certificate** depends on who ends the TLS connection:

1. **The server itself**, when it serves HTTPS directly (ADR 0005).
2. **A reverse proxy that ends TLS** (HAProxy, Caddy, nginx), which can check
   against a CA file and a revocation list, and pass the certificate's
   fingerprint on in a header.
3. **Cloudflare**, when the hostname is proxied by it: TLS ends at Cloudflare's
   edge, so neither the proxy behind it nor the server ever sees the client's
   certificate. On all Cloudflare plans, client certificates come from a
   Cloudflare-managed CA (bringing your own CA is Enterprise-only); they are
   created through Cloudflare's API from a certificate signing request (CSR),
   at most 100 active ones per zone outside Enterprise, and revoked through it.
   A WAF custom rule on `cf.tls_client_auth.cert_verified` blocks requests
   without a valid one, and can leave chosen paths open. Authenticated origin
   pulls (all plans) let the proxy behind Cloudflare accept only Cloudflare.

The maintainer's own setup is the third: one hostname, proxied by Cloudflare,
with HAProxy ending Cloudflare's connection, and all traffic (home included)
going through Cloudflare. Most other users will have the first or second.

## Decision

1. **Off by default.** A new server works as now. The owner turns on **secure
   remote access** in the admin settings; from then on every connection must
   present a client certificate, except the front-door paths in (4).
2. **The server issues and revokes the certificates, through an issuer the
   owner chooses:**
   - **Built-in CA** (for checks by the server itself or by a proxy that ends
     TLS): the server keeps its own small CA in the data folder, and writes
     `ca.pem` and `crl.pem` there for the server's own TLS (reloaded like the
     certificate files, ADR 0005) or for the proxy to use.
   - **Cloudflare** (for a proxied hostname): the server makes the key and a CSR
     and asks Cloudflare's API to sign it, with an API token that has only the
     "SSL and Certificates Write" permission for that zone, given as a secret
     file. Revoking calls the same API. The 100-certificate limit is shown, and
     the server refuses to issue past it.
   In both cases the private key is made on the server, put into a `.p12` with a
   one-time password, handed out once, and not kept.
3. **Certificates belong to devices and accounts.**
   - **The Switch:** pairing hands the app its certificate with its token, so
     getting a certificate is part of pairing, as the maintainer suggested.
     The app keeps it next to that server's entry (ADR 0010 in the app) and
     presents it on every connection to that server.
   - **Browsers:** each account's Account page has **Add a certificate for this
     browser or phone**, which downloads a `.p12` and shows its password once,
     with short instructions for iOS, Android, Windows and macOS.
   - Unpairing a Switch, removing a browser certificate, or removing an account
     revokes its certificates at once (a new CRL, or Cloudflare's API).
4. **The front door stays open, guarded by its one-time secret.** These paths
   must work without a certificate, or nobody could get one:
   `POST /api/v1/devices/pair` (8-character code, 5 minutes, rate-limited),
   the invite page and `POST /api/v1/invites/accept` (7-day single-use link,
   rate-limited), the first-run setup, and the static files those pages need.
   Everything else needs a certificate. The server prints the exact list, and
   for Cloudflare the WAF rule expression to paste, on the settings page.
5. **Turning it on safely.** Enabling it walks the owner through: create a
   certificate for the browser in use and install it; for a proxy, put the CA
   and CRL files in place or create the WAF rule; check that this browser now
   connects with its certificate; only then switch it on. The server won't
   enforce it while the owner's own browser has no working certificate.
6. **Defence in depth when a proxy checks.** With a trusted proxy that passes
   the certificate's fingerprint (`X-SSL-Client-SHA1` from HAProxy, or
   `Client-Cert` from a Cloudflare transform rule), the server also refuses a
   request whose certificate it didn't issue or has revoked. Those headers are
   only read from the trusted proxies already configured
   (`DREADOVANIA_TRUSTED_PROXIES`); any other source's copies are ignored.
7. **A new dependency:** `cryptography`, already installed through Randovania
   and `sigstore`, becomes a direct dependency for making keys, CSRs, the CA,
   CRLs and `.p12` files.

## Pull requests

1. This ADR.
2. Server: the built-in CA, certificates per device and browser, revocation,
   the server's own TLS check, the front-door exemptions and the settings page.
3. Server: the Cloudflare issuer, and the fingerprint check behind a proxy.
4. App: certificates from pairing, stored per server and presented on every
   connection; a hardware check against a Cloudflare-proxied server.
5. Docs: recipes for HAProxy (`verify required ca-file … crl-file …`,
   authenticated origin pulls) and Cloudflare (the mTLS hostname, the WAF rule,
   the API token).

## Consequences

- With it on, the server can be put on the Internet: without a certificate,
  nothing past the front door answers.
- Every device needs a certificate. Pairing does it for a Switch; a browser
  needs a one-time install, which differs by platform and is the least smooth
  part.
- With Cloudflare, at most 100 certificates per zone (outside Enterprise),
  plus an API token stored on the server; a leaked token lets someone issue
  certificates for that zone, so it is scoped to the one permission and zone.
- The front-door paths stay reachable by anyone; they are guarded by one-time
  secrets and rate limits, as now.
- More to set up for the owner; the settings page and docs carry the steps.
