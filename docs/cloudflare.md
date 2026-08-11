# Cloudflare Tunnel

The iPhone needs an HTTPS address for the Mac receiver. A named Cloudflare Tunnel gives the Shortcut a hostname that survives Mac reboots.

## Setup

Install `cloudflared`, configure the project, then create the tunnel:

```bash
brew install cloudflared
./scripts/configure
./scripts/setup-cloudflare
./scripts/install-services
```

The setup script runs the equivalent of:

```bash
cloudflared tunnel login
cloudflared tunnel create codex-ios-assistant
cloudflared tunnel route dns codex-ios-assistant iphone.example.com
```

It writes the tunnel UUID and credentials path to `~/.config/codex-ios-assistant/cloudflared.yml`. Cloudflare keeps the credentials JSON under `~/.cloudflared/`. Do not copy either file into the repository.

The script refuses to replace an existing DNS record. Use a free hostname, remove the old record in Cloudflare, or run the printed `--overwrite-dns` command after checking the record yourself.

Cloudflare documents this flow in [Create a locally-managed tunnel](https://developers.cloudflare.com/tunnel/advanced/local-management/create-local-tunnel/). This project runs `cloudflared` as a per-user LaunchAgent beside the sender and receiver.

## Stable hostname

Quick tunnels assign a new `trycloudflare.com` hostname at startup. A named tunnel keeps its UUID, and the DNS record continues to point at that tunnel after a reboot.

## Network exposure

The receiver listens on `127.0.0.1`, so other machines on the local network cannot connect to port 8787. Cloudflare provides the public route.

`/` and `/health` return a generic status without authentication. Hardened
phone-data and receipt endpoints require protocol version 2 plus the
per-request `X-Request-Id` and `X-Receipt-Capability` headers. The static
`X-Auth` token remains as defense in depth and for legacy paths; it cannot
authorize a new request by itself. The final tunnel ingress rule returns 404
for unknown hostnames. Receiver logs include response sizes and request IDs,
but no screen, clipboard, capabilities, or alarm contents.

## Change the hostname

1. Run `scripts/configure --url https://new-host.example.com`.
2. Run `scripts/setup-cloudflare` to create the DNS route and replace the private tunnel config.
3. Run `scripts/install-services`.
4. Run `scripts/copy-shortcut`, then replace all actions in the existing stable
   `iOS Assistant` Shortcut in place.
5. Keep the iPhone Message automation pointed at that same Shortcut identity.

Run `/health` and one response command through the new hostname before relying
on the updated Shortcut.
