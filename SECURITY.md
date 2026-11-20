# Security

## Reporting a vulnerability

Open a [private security advisory](../../security/advisories/new) rather than a
public issue. Please include what an attacker could do and how to reproduce it.

## What clauxy handles

clauxy holds long-lived OAuth tokens for your Claude subscriptions. Anyone who
can reach the proxy port can spend them.

- **Upstream HTTPS sockets are bound to a verified VPN tunnel.** Destination
  routes and active tunnel interfaces are checked on macOS and Linux. Direct
  routes, unverified state, and failed interface binding block requests; there
  is no direct-network fallback. IPv4 and IPv6 are checked separately. Proxies
  and redirects cannot bypass the transport; TLS verification stays enabled.
  This verifies a tunnel route, not the tunnel's provider or encryption.
  DNS uses the system resolver. External Claude CLI/browser processes have a
  route preflight but are not socket-bound by Python; use your VPN's system
  protection for those processes and do not exclude Python from VPN policy.
- **The port binds `127.0.0.1`.** Publishing it on `0.0.0.0`, or through a
  tunnel, hands your subscriptions to whoever finds it — the proxy itself asks
  for no credential, by design.
- **Tokens are stored `chmod 600`** under `~/.clauxy/tokens/`, in a `0700`
  directory. On macOS, `clauxy add --keychain` uses the Keychain instead.
- **Tokens never leave the machine** except as the `Authorization` header on
  requests to `api.anthropic.com`.
- **The bearer your client sends is discarded.** The proxy substitutes the
  account's own, so a placeholder is fine in your client config.

## If a token leaks

Revoke it at [claude.ai](https://claude.ai) under your account settings, then
`clauxy remove <alias>` and `clauxy add <alias>` to issue a new one.

## Known limitations

- There is **no authentication** on the proxy or the panel. The loopback bind
  is the whole boundary. Do not change it.
- The panel is served over plain HTTP, which is fine on loopback and not fine
  anywhere else.
- Request logs record models, token counts and timings — never prompts or
  responses.
