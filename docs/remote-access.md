# Secure remote access

With **secure remote access** on, the server only answers browsers, phones and
Switches that present a **client certificate** it gave them. Without one, nothing
past the front door answers, so the server can be put on the Internet. The
decision and its details are in [ADR 0023](adr/0023-secure-remote-access.md).

It is off by default. You turn it on in the web UI, under **Account → Secure
remote access**, which walks you through the steps below and won't switch it on
until it can see that your own browser gets through.

**Before you start:** your server needs HTTPS at a hostname ([docs/https.md](https.md)),
and every account that uses the web UI needs a certificate for its browser. The
settings page names the accounts that don't have one yet.

## Who checks certificates

Pick the one that matches how HTTPS reaches your server:

| Your setup | Choose | Certificates from |
|---|---|---|
| The server serves HTTPS itself (its own certificate files) | **This server** | The server's own CA |
| A reverse proxy ends TLS (HAProxy, Caddy, nginx) | **A reverse proxy** | The server's own CA |
| The hostname is proxied by Cloudflare | **Cloudflare** | Cloudflare's CA, through its API |

Whichever you choose, these stay open without a certificate (the **front door**),
or nobody could get one:

- `POST /api/v1/devices/pair`: pairing a Switch (the pairing code is made on the
  Account page, which needs a certificate);
- `GET`/`POST /invite` and `POST /api/v1/invites/accept`: joining with an invite;
- `/invite/welcome/…` and `/invite/certificate/…`: a new member's certificate;
- `/static/…`: those pages' stylesheet, scripts and icons;
- `/healthz`: health checks.

The settings page shows the exact list for your version.

## Getting certificates

- **Browsers and phones:** **Account → Certificates → Add a certificate for this
  browser or phone**. It downloads a `.p12` once and shows its password once; the
  page has install notes for iPhone, Android, Windows, macOS and Firefox. Browser
  certificates last a year (`DREADOVANIA_BROWSER_CERTIFICATE_DAYS`); 30 days before,
  a banner offers the replacement in one click.
- **Friends you invite** get one for their browser as they join, while secure
  remote access is on.
- **Switches** get one when they're paired, and the app renews it by itself
  (every 47 days by default, `DREADOVANIA_DEVICE_CERTIFICATE_DAYS`). This needs an app
  version that presents the certificate.
- **Revoking:** remove a certificate on the Account page, unpair a Switch, or remove
  an account. It stops working at once.

**Locked out?** On the server, run:

```bash
docker compose exec dreadovania python -m dreadovania remote-access off
```

## 1. This server checks

For a server that serves HTTPS itself (`DREADOVANIA_TLS_CERT_FILE` and
`DREADOVANIA_TLS_KEY_FILE`).

The main port (8080) asks for a certificate. The front door has its own port,
**8081**, which never asks for one and serves only the front-door paths. Publish
both in `compose.yaml`:

```yaml
    ports:
      - "443:8080"
      - "8443:8081"
    environment:
      DREADOVANIA_FRONT_DOOR_URL: https://dread.example.com:8443
```

Set `DREADOVANIA_FRONT_DOOR_URL` when the front door isn't reachable as
`https://<your hostname>:8081`: invite links and the pairing address use it.

Then, under **Account → Secure remote access**: choose **This server**, add a
certificate for your browser and install it, restart the browser, and turn it on
once the page says your browser connected with your certificate.

## 2. A reverse proxy checks (HAProxy)

The proxy checks clients against the server's CA and revocation list, and leaves
the front door open. Both files are in the data folder, `data/remote-access/ca.pem`
and `data/remote-access/crl.pem`, and can be downloaded from the settings page.
Mount the folder into the proxy rather than copying the files: the revocation list
changes whenever a certificate is revoked, and is republished daily.

```haproxy
frontend dreadovania
    bind :443 ssl crt /etc/haproxy/certs/dread.example.com.pem ca-file /srv/dreadovania/remote-access/ca.pem crl-file /srv/dreadovania/remote-access/crl.pem verify optional

    # What works without a certificate (the settings page has the current list).
    acl front_door path /healthz /api/v1/devices/pair /invite /api/v1/invites/accept
    acl front_door path_beg /static/ /invite/welcome/ /invite/certificate/
    # With "verify optional", a certificate that doesn't check out fails the
    # handshake, so a connection that used one used a valid one.
    http-request deny deny_status 403 if !front_door !{ ssl_c_used }

    # Pass the certificate's fingerprint on, so the server can check it too.
    http-request set-header X-SSL-Client-SHA256 %[ssl_c_der,sha2(256),hex] if { ssl_c_used }
    http-request del-header X-SSL-Client-SHA256 if !{ ssl_c_used }

    default_backend dreadovania

backend dreadovania
    server dreadovania 172.18.0.10:8080
```

On the server, set `DREADOVANIA_TRUSTED_PROXIES` to the proxy's address or network
(for example `172.18.0.0/16`). The server only believes the fingerprint header from
those addresses, and with **Also check the certificate it passes on** ticked (the
default), it refuses certificates it didn't issue or has revoked, even if the proxy
lets them through.

**The revocation list needs reloading.** HAProxy reads `crl-file` when it starts.
After a revocation, reload HAProxy, or update it in place through the runtime API
(`set ssl crl-file` then `commit ssl crl-file`). A systemd path unit or a cron job
that reloads when `crl.pem` changes does it automatically. Until then, the server's
own fingerprint check still refuses the revoked certificate.

Then, under **Account → Secure remote access**: choose **A reverse proxy**, add a
certificate for your browser and install it, and turn it on once the page says your
browser connected with your certificate. (With the fingerprint check off, the page
asks you to confirm that you checked instead.)

### Certificates from your own CA

If your proxy checks against your own CA (for example step-ca), as well as or
instead of the server's, the server can still check those certificates:

1. Under **Account → Secure remote access**, with **A reverse proxy** chosen, give
   your CA's certificate (optional, but then only certificates it issued are
   accepted).
2. On the **Account** page, **Add a certificate from your own CA**: upload the
   certificate on its own (`.pem`, `.crt`, `.cer`), or its `.p12` with the password.
   The server keeps only the certificate's fingerprint and dates. A `.p12`'s private
   key and password are used once, in memory, and never stored; upload the
   certificate alone if you'd rather the key never reaches the server at all.
3. Install the `.p12` on the browser or phone yourself. For a Switch, choose it under
   **For**, and put the `.p12` on its SD card as
   `/config/dreadovania/certificates/<host>.p12`, with its password in
   `<host>.password` (for `https://dread.example.com:8443`, the host is
   `dread.example.com_8443`). The app uses it instead of the server's own.

Revoke them at your CA as usual (your proxy's `crl-file`); revoking one on the
Account page stops the server accepting it. The server's own check (section 1) and
Cloudflare (section 3) only accept their own CAs' certificates.

## 3. Cloudflare checks

Cloudflare ends TLS at its edge, so neither your proxy nor the server sees the
client's certificate: Cloudflare checks it. On most plans Cloudflare only accepts
certificates from its own CA, so the server asks Cloudflare to sign them.

1. **An API token for the server.** In Cloudflare, go to
   [My Profile → API Tokens](https://dash.cloudflare.com/profile/api-tokens) and
   create a custom token with one permission, **Zone → SSL and Certificates →
   Edit**, for your zone only. Save it in a file on the server, readable only by the
   server's user, with nothing in it but the token (a byte-order mark, which some
   Windows editors add, is ignored), and set:

   ```yaml
       environment:
         DREADOVANIA_CLOUDFLARE_ZONE_ID: 0123456789abcdef0123456789abcdef
         DREADOVANIA_CLOUDFLARE_API_TOKEN_FILE: /run/secrets/cloudflare_api_token
   ```

   The zone ID is on the zone's **Overview** page in the
   [Cloudflare dashboard](https://dash.cloudflare.com/). Anyone with the token can
   issue certificates for the zone, so it's scoped to that one permission and zone.
   Cloudflare allows 100 active client certificates per zone outside Enterprise; the
   server refuses to issue past that.
2. **mTLS for the hostname.** In the zone, under **SSL/TLS → Client Certificates**,
   add your hostname to the hosts that ask for a client certificate.
3. **A WAF rule.** Under **Security → WAF → Custom rules**, create a rule with the
   action **Block** and the expression from the settings page. It blocks requests
   without a valid, unrevoked certificate, except the front door.
4. **Authenticated origin pulls.** Under **SSL/TLS → Origin Server**, turn on
   **Authenticated Origin Pulls**, and have the proxy behind Cloudflare accept only
   Cloudflare's own client certificate (in HAProxy, `ca-file` with Cloudflare's
   origin-pull CA and `verify required` on the `bind` Cloudflare connects to).
   Without this, someone who finds your origin's address can go around Cloudflare's
   check.
5. **Certificates you already made in Cloudflare's dashboard** can be added on the
   Account page under **Add a certificate from your own CA** (the certificate, or its
   `.p12` with the password). The server finds it among the zone's client
   certificates by its fingerprint and records it as Cloudflare's, so revoking it on
   the Account page revokes it at Cloudflare too. While Cloudflare checks, a
   certificate that isn't one of the zone's is refused, since Cloudflare wouldn't let
   it in.
6. **In the server**, under **Account → Secure remote access**: choose **Cloudflare**.
   From then on new certificates come from Cloudflare. Certificates from the
   server's own CA don't get through Cloudflare, so add a new one for each browser
   and phone, and let each Switch renew (the app does it the next time it connects).
   Then turn it on.

The dashboard's menu names change from time to time; if one isn't where this says,
search Cloudflare's docs for "mTLS" and "Authenticated Origin Pulls".
