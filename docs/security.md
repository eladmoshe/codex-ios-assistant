# Security

This project connects Codex, Messages, an iPhone Shortcut, and a public hostname. Screen text, screenshots, clipboard contents, alarms, Contacts, and Messages history may contain private data.

## Files that must stay private

- `~/.config/codex-ios-assistant/config.env` contains the iMessage target and receiver token.
- `~/.config/codex-ios-assistant/cloudflared.yml` names the tunnel credentials file.
- `~/.cloudflared/<tunnel-id>.json` authorizes the tunnel.
- `build/ios-assistant-actions.plist` contains the receiver hostname and token.
- `~/.local/share/codex-ios-assistant/inbox/` contains screenshots.

The installer keeps these files outside Git or under ignored paths. Config files and screenshots use mode `0600`; their parent directories use `0700`.

## Receiver

The receiver binds to `127.0.0.1`. Cloudflare is its public route. `/` and
`/health` expose a fixed status string. Hardened phone-data and receipt
requests require protocol version 2, a 32-lowercase-hex-character request ID,
and a random per-request capability in `X-Receipt-Capability`. The receiver compares the
capability hash in constant time and consumes a matching pending request
before returning success.

The receiver also exposes a separate mode-`0600` Unix registration socket for
the local CLI. It accepts only the current user's peer UID and supports
register, poll, and cancel operations. The public HTTP listener never accepts
registrations. Pending requests expire, are bounded, and are not replayable.

Receiver logs include byte counts, alarm counts, and request IDs. They do not include screen text, clipboard values, capabilities, or alarm details. Hardened read responses are bounded before being returned to the CLI. Text responses live in memory. Screenshots remain on disk until you remove them.

The static token remains in the rendered Shortcut for defense in depth and
legacy interactive paths, but it is not sufficient for a Nami request. An
attacker also needs the live, single-use capability registered for that exact
request, plus its request ID and expected action. A lost receiver process
therefore yields a timeout/inconclusive result rather than an inferred success.

## Messages sender

The sender listens on a mode-`0600` Unix socket. It checks the peer UID when macOS provides one, rejects newlines and requests over 4 KiB, and accepts commands beginning with `hola `. It runs fixed AppleScript through `/usr/bin/osascript`; clients cannot choose the program or script.

Restrict the iPhone Message automation to the expected sender and messages containing `hola`. Do not add a Shortcut branch that turns message text into arbitrary commands or runs an arbitrary Shortcut.

The CLI opens message drafts for review. It does not send them. Commerce and rideshare links open a page without placing an order or requesting a ride.

## Check a commit

Run these commands before pushing:

```bash
make test
git status --short
git diff --cached
```

Inspect the staged diff for personal domains, email addresses, phone numbers, `/Users/<name>` paths, tokens, tunnel UUIDs, and private keys. `.gitignore` does not protect a file after Git has staged it.

If a token or tunnel credential reaches a commit, rotate it and remove it from Git history before publishing the repository. Deleting it in a later commit leaves the original value in history.
