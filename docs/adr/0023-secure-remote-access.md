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

## Amendments, 2026-11-11 (decided with the maintainer before any code)

These refine the decisions above; where they differ, these apply.

- **Decision 2, issuers: three, not two.**
  - **Built-in CA**, **Cloudflare**, and **External**: certificates made by the
    owner's own CA (for example step-ca), uploaded per device as a `.p12`, which
    the server then hands out like its own.
  - Let's Encrypt can't be an issuer: it stopped issuing certificates for TLS
    client authentication in 2026 (default profile from 11 February, the last
    `tlsclient` profile retired on 8 July). It stays the *server* certificate.
- **Cloudflare requires authenticated origin pulls.** With the Cloudflare issuer,
  the proxy behind Cloudflare must accept only Cloudflare's own client
  certificate (zone-level authenticated origin pulls); otherwise the origin can
  be reached around the edge's check. The settings page and the recipe say so.
- **The server's own check uses a second port.** When Dreadovania serves HTTPS
  itself, uvicorn can't tell the application whether a client presented a
  certificate, so it can't exempt some paths. The main port (8080) then requires
  a valid client certificate for every connection, and a **front-door port,
  8081**, serves only the front-door paths without one and answers 404 to
  everything else. Both can be remapped in Docker. Behind a proxy or Cloudflare,
  the front door is exempted by path instead, and 8081 isn't used.
- **Decision 4, the front door, made precise:**
  - `POST /api/v1/devices/pair` (the Switch app calls nothing else before it is
    paired; the pairing code itself is made on the Account page, which needs a
    certificate);
  - the invite page and `POST /api/v1/invites/accept`, which now also hands the
    new member's browser its certificate, as pairing does for a Switch, so a
    friend isn't locked out right after joining;
  - the static files those pages use.
  - First-run setup is **closed** while secure remote access is on: it can only
    be turned on once setup is done. `/api/v1/info`, sign-in, passkeys and app
    updates all need a certificate.
- **Lifetimes and rotation.**
  - **Switch:** 47 days by default (configurable). The app renews its own
    certificate once a third of its lifetime is left, over a connection that
    already uses its current certificate and token; the old one is revoked once
    the new one has been used, so the count of active certificates (and
    Cloudflare's 100-per-zone limit) doesn't grow.
  - **Browsers:** 1 year by default (configurable), because a browser can't
    install a certificate by itself; the web UI shows a banner 30 days before
    expiry with a one-click download of the replacement.
- **Limits:** 100 active Cloudflare certificates per zone is enough for the
  maintainer's devices and friends.

## Implementation notes, 2026-11-11 (parts 1 and 2, #61 and #62)

What the code does where it differs from, or adds to, the text above. Where they
differ, these apply.

- **The front door also has `/healthz`** (GET and HEAD), on port 8081 and, behind
  a proxy, as an exempt path. Docker's health check and a proxy's need it once the
  main port asks for a certificate. It answers `{"status": "ok"}` and nothing
  else. `/api/v1/info` needs a certificate, as the amendment says.
- **The server can see the client's certificate after all.** The amendment's
  reason for port 8081 still holds (uvicorn alone doesn't pass the certificate
  on), but a small wrapper around uvicorn's HTTP protocol (`connection.py`) now
  puts the verified certificate and the connection's real address into each
  request. The second port stays, so that a client without a certificate is
  refused by TLS itself on 8080; the app additionally refuses a revoked or
  unknown certificate on every request, so a revocation takes effect at once,
  even on a connection opened before it.
- **The main port asks for a certificate in three steps:** not at all until
  "this server" is chosen (as before this feature); optionally once it's chosen
  but still off, so the owner can check their browser's certificate first; and
  required once it's on.
- **Decision 6, the headers from a proxy:** `X-SSL-Client-SHA256` (hex, as
  HAProxy writes `%[ssl_c_der,sha2(256),hex]`) or RFC 9440 `Client-Cert`
  (`:base64 DER:`), instead of `X-SSL-Client-SHA1`: the server records SHA-256
  fingerprints only. Read only from `DREADOVANIA_TRUSTED_PROXIES`.
- **The proxy fingerprint check came in part 2,** not part 3, and is on by
  default behind a proxy. With it off, the proxy alone checks, and turning secure
  remote access on needs the owner to confirm they checked.
- **Turning it on (decision 5):** when the server sees certificates (its own
  check, or a proxy passing one on), the request turning it on must come with one
  of the admin's own certificates. Otherwise the admin confirms.
- **Cloudflare can be chosen but not turned on** until the Cloudflare issuer
  exists (part 3): on most plans Cloudflare only accepts certificates from its own
  CA, so built-in certificates wouldn't get through it.
- **A locked-out owner** runs `python -m dreadovania remote-access off` on the
  server. It is picked up within seconds.
- **A browser certificate's `.p12` is downloaded once,** within 10 minutes, from
  memory; the server never writes it to disk. The expiry banner's one-click
  replacement keeps the old certificate working until the browser connects with
  the new one, and then revokes the old one.
- **Settings:** `DREADOVANIA_FRONT_DOOR_PORT` (8081, 0 turns it off; it listens
  only when the server serves HTTPS), `DREADOVANIA_FRONT_DOOR_URL` (where devices
  reach it when the port is remapped; invite links and the pairing address use
  it while secure remote access is on), `DREADOVANIA_DEVICE_CERTIFICATE_DAYS`
  (47) and `DREADOVANIA_BROWSER_CERTIFICATE_DAYS` (365).
- **Part 3 (pairing and invites):**
  - **Pairing always hands the Switch a certificate,** whether secure remote access
    is on or not, so a Switch paired today is ready the day it's turned on. The
    response also carries the server's main address (`server_url`), since pairing
    may have been through the front door.
  - **The app renews it** with `POST /api/v1/devices/me/certificate` once
    `renew_after` (a third of the lifetime left) has passed. The same call gives a
    Switch paired before certificates its first one. The old certificate is revoked
    when the Switch first connects with the new one.
  - **Accepting an invite while it's on** hands the new member's browser its
    certificate: in the API's response, or on a welcome page on the front door with a
    one-time download and the password shown once (`/invite/welcome/…` and
    `/invite/certificate/…`, added to the front door). While it's off, members add
    their own from the Account page.
  - **The settings page names the accounts without a browser certificate** before
    it's turned on, since they'd be locked out of the web UI.
- **The Cloudflare issuer:**
  - Configured with `DREADOVANIA_CLOUDFLARE_ZONE_ID` and
    `DREADOVANIA_CLOUDFLARE_API_TOKEN_FILE` (a secret file; the token needs only "SSL
    and Certificates Write" on that zone). The server refuses to start if only one is
    set, or the file is missing or empty.
  - While Cloudflare is the chosen check, new certificates come from Cloudflare: the
    server makes the key and a CSR, `POST /zones/{zone}/client_certificates` signs it,
    and the key stays only in the one-time `.p12`. Past 100 active certificates for
    the zone, issuing is refused. Cloudflare must sign the key that was asked for.
  - Revoking calls `DELETE /zones/{zone}/client_certificates/{id}` at once. A sweep
    every 5 minutes passes on revocations made by removing a device or an account, and
    retries any Cloudflare couldn't take.
  - Switching to Cloudflare makes `GET /devices/me` tell each Switch to renew now, so it
    gets a Cloudflare certificate before secure remote access is turned on. Browsers
    need a new certificate from the Account page.
  - Cloudflare mode can be turned on once the issuer is configured.
- **The External issuer** (the owner's own CA) is still to come.
- **Until part 4:** a paired Switch can't connect while it's on, until the app
  presents its certificate.

## Pull requests

1. This ADR.
2. Server: the built-in CA, certificates per device and browser, revocation,
   rotation, the server's own TLS check with the front-door port, the
   fingerprint check behind a proxy, and the settings page. Done in two pull
   requests: #61 (the CA and certificates) and #62 (the rest).
3. Server: pairing and invites hand out certificates; the Cloudflare and
   External issuers.
4. App: certificates from pairing, stored per server, presented on every
   connection and renewed automatically; first, a hardware check against a
   Cloudflare-proxied server.
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
