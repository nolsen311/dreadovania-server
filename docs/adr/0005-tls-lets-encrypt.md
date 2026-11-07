# 0005. TLS via user-supplied certificates and Let's Encrypt DNS-01

- Status: Accepted
- Date: 2026-11-07

## Context

Passkeys need HTTPS. Users often already use Let's Encrypt. Many instances will
be LAN-only, where the HTTP-01 challenge can't work because Let's Encrypt can't
reach the server.

## Decision

- **Bring your own cert files (core feature):** mount `fullchain.pem` and `privkey.pem`.
  The server serves HTTPS directly and **reloads automatically when the files change**.
  This covers certbot, acme.sh and certificates exported by an existing proxy.
- **Documented recipe:** a Caddy sidecar in `docker-compose` using **DNS-01**, with
  Cloudflare as the worked example and notes for other providers.
- **Documented recipe:** running behind an existing reverse proxy (Traefik, NPM), with
  example labels and config.
- **No ACME client in the app.** The DNS API token can change DNS records, so it stays
  in a separate container or the user's own tooling. Recommend a token scoped to a
  single zone, stored as a Docker secret.
- **Proxy headers:** the app trusts `X-Forwarded-*` only from a configured
  trusted-proxy allowlist. The setup wizard checks that the origin matches the
  configured URL.
- **Hostname only:** requests by IP address redirect to the configured hostname.

## Consequences

- LAN-only users can get a real certificate with no exposure to the internet.
- The Switch app can verify TLS properly using bundled root certificates; there is
  no "ignore certificate errors" option.
- Built-in ACME can be reconsidered later in a new ADR.
