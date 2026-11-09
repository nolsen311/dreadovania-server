# HTTPS and your server's hostname

Passkeys only work over HTTPS, at a hostname such as `dread.home.example.com`,
not at an IP address (ADR 0004 and 0005). The Switch app also needs HTTPS. You
can keep using plain HTTP at an address on your LAN, but then passkeys are
unavailable and you sign in with your password.

There are three ways to get there. In all of them:

- **The hostname must resolve to the server on your LAN.** If it's a public
  domain you own, either add a DNS record with the server's LAN address, or add a
  host override in your router or local DNS (pfSense: *Services → DNS Resolver →
  Host Overrides*). Nothing needs to be reachable from the internet.
- **Tell the server its address:** set `DREADOVANIA_PUBLIC_URL` to it, for
  example `https://dread.home.example.com`. A new server can also take it from
  the setup wizard. Once it's set, a browser that opens the server by IP address
  is redirected to the hostname.
- **Changing the hostname later invalidates passkeys**, because each passkey
  belongs to one hostname. Password sign-in keeps working.

## 1. Your own certificate files (the server speaks HTTPS)

Use this if you already get certificates from certbot, acme.sh, your router's
ACME package or another proxy. Mount the folder and point the server at the files:

```yaml
    environment:
      DREADOVANIA_PUBLIC_URL: https://dread.home.example.com
      DREADOVANIA_TLS_CERT_FILE: /certs/fullchain.pem
      DREADOVANIA_TLS_KEY_FILE: /certs/privkey.pem
    volumes:
      - /path/to/certs:/certs:ro
```

- **Files:** use the full chain (your certificate followed by the intermediate),
  not the certificate alone. Both files must be readable by UID 10001. The key
  must not have a passphrase.
- **Renewal:** when the files change, new connections get the new certificate
  within a few seconds, with no restart. If the renewal tool writes the
  certificate before its key, the server keeps the old pair until both match.
- **Problems** stop the server at startup with the reason in its log, for example
  a missing file, or a key that doesn't belong to the certificate.
- **One port:** the server then serves HTTPS only, on its usual port. Change the
  port mapping if you want `https://dread.home.example.com` without a port, for
  example `"443:8080"`.

Let's Encrypt can issue a certificate for a LAN-only server with the **DNS-01**
challenge: it proves you own the domain through a DNS record instead of
connecting to the server. Option 2 does that automatically; certbot and acme.sh
have DNS plugins for most DNS providers.

## 2. A Caddy container in front, with Let's Encrypt over DNS-01

Caddy gets and renews the certificate itself and forwards to Dreadovania over
the private Docker network. Caddy needs the module for your DNS provider; this
example builds it with Cloudflare's. See [caddy-dns](https://github.com/caddy-dns)
for others. `{file.…}` needs Caddy 2.8 or newer.

`Dockerfile.caddy` (pin both images by digest for production):

```dockerfile
FROM caddy:2-builder AS build
RUN xcaddy build --with github.com/caddy-dns/cloudflare
FROM caddy:2
COPY --from=build /usr/bin/caddy /usr/bin/caddy
```

`Caddyfile`:

```
dread.home.example.com {
	tls {
		dns cloudflare {file./run/secrets/cloudflare_token}
	}
	reverse_proxy dreadovania:8080
}
```

Added to `compose.yaml`:

```yaml
services:
  caddy:
    build: { context: ., dockerfile: Dockerfile.caddy }
    restart: unless-stopped
    ports: ["443:443"]
    secrets: [cloudflare_token]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    networks: [dread]
  dreadovania:
    # ...as in compose.yaml, but with no "ports:" (only Caddy is published), and:
    environment:
      DREADOVANIA_PUBLIC_URL: https://dread.home.example.com
      DREADOVANIA_TRUSTED_PROXIES: 172.30.0.0/24
    networks: [dread]
networks:
  dread:
    ipam: { config: [{ subnet: 172.30.0.0/24 }] }
volumes:
  caddy-data:
secrets:
  cloudflare_token:
    file: ./cloudflare_token.txt
```

- **The API token can change your DNS,** which is why it stays in Caddy and
  never reaches Dreadovania. In Cloudflare, create a token with **Zone → DNS →
  Edit** on that one zone only, and keep `cloudflare_token.txt` readable only
  by you.
- **Trusted proxies:** `DREADOVANIA_TRUSTED_PROXIES` is the Docker network Caddy
  connects from, so the server believes Caddy's `X-Forwarded-For` and
  `X-Forwarded-Proto` headers, and nobody else's.

## 3. An existing reverse proxy

Nginx Proxy Manager, Traefik, HAProxy (including pfSense's package) and others
work the same way as Caddy above:

- The proxy terminates HTTPS for your hostname and forwards to the server's
  port over HTTP.
- Set `DREADOVANIA_PUBLIC_URL` to the HTTPS address, and
  `DREADOVANIA_TRUSTED_PROXIES` to the proxy's address as the server sees it:
  - a Docker network, e.g. `172.18.0.0/16`, when they share one;
  - the proxy host's LAN address when it runs on another machine.
- The proxy must send `X-Forwarded-Proto: https`. Most do by default; in
  HAProxy add `http-request set-header X-Forwarded-Proto https`.
- **Allow large requests** for game-file uploads: each upload chunk is up to
  `DREADOVANIA_ROMFS_UPLOAD_CHUNK_BYTES` (8 MB by default). Nginx's default
  limit is 1 MB: set `client_max_body_size 16m;`. In Nginx Proxy Manager, add
  it under the proxy host's *Advanced* tab.
- **Allow long requests:** patching can take several minutes, but the page
  polls for progress, so the usual 60-second timeouts are fine.

## Moving an existing server to a hostname

1. Get the certificate working with one of the options above, and check that
   `https://dread.home.example.com/healthz` answers `{"status":"ok"}`.
2. Set `DREADOVANIA_PUBLIC_URL` to the HTTPS address and restart the container.
3. Open the old address: it redirects to the hostname. Sign in again there, since
   the old address's sign-in cookie doesn't carry over.

Your seeds, Switches and settings stay as they are.
