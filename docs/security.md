# Security

This project connects Codex, Messages, an iPhone Shortcut, and a public hostname. Screen text, screenshots, clipboard contents, alarms, Contacts, and Messages history may contain private data.

## Files that must stay private

- `~/.config/codex-ios-assistant/config.env` contains the iMessage target, receiver token, and private command secret.
- `~/.config/codex-ios-assistant/cloudflared.yml` names the tunnel credentials file.
- `~/.cloudflared/<tunnel-id>.json` authorizes the tunnel.
- `build/ios-assistant-actions.plist` contains the receiver hostname and token.
- `~/.local/share/codex-ios-assistant/inbox/` contains screenshots.

The installer keeps these files outside Git or under ignored paths. Config files and screenshots use mode `0600`; their parent directories use `0700`.
`iphone doctor` and the runtime config loader accept the private config only
when it is an operator-owned regular file with mode `0600` under an
operator-owned mode `0700` directory; symlinks and shared permissions are
rejected on every read.

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
Their capability hashes and bounded completions are atomically persisted in an
operator-owned mode-`0600` state file under the private data directory; raw
capabilities are never written to disk.

Receiver logs include byte counts, alarm counts, and request IDs. They do not
include screen text, clipboard values, capabilities, or alarm details. Hardened
read responses are bounded before being returned to the CLI. Their correlated
completion data, including private text, is retained for at most ten minutes in
the operator-owned mode-`0600` state file so it survives a receiver restart;
legacy static-token values remain memory-only. Receiver-owned screenshots are
retained only for the same bounded ten-minute TTL, after which the inbox sweeper
removes them; copy any screenshot needed for a longer-lived Nami log before
that deadline.

The static token remains in the rendered Shortcut for defense in depth and
legacy interactive paths, but it is not sufficient for a Nami request. An
attacker also needs the live, single-use capability registered for that exact
request, plus its request ID and expected action. A lost receiver process
therefore yields a timeout/inconclusive result rather than an inferred success.

## Messages sender

The sender listens on a mode-`0600` Unix socket under the private
`~/.config/codex-ios-assistant/` directory (never `/tmp`). It checks the peer
UID when macOS provides one, validates the parent/socket owner and mode, and
accepts newline-delimited UTF-8 JSON requests up to 4 KiB. JSON escaping keeps
embedded newlines and multibyte opaque values representable; carriage returns
and NUL bytes are rejected. Wire commands must begin with the configured
64-lowercase-hex private command secret followed by `hola `. The sender runs
fixed AppleScript through `/usr/bin/osascript`; clients cannot choose the
program or script.

The current Shortcut requires that same private secret in every branch condition
before it performs a native action. This is the pre-mutation authorization
boundary: a plain `hola` message cannot match a branch, even though iOS starts
the automation. The rendered Shortcut contains the secret and must remain
private. Receipt capabilities authenticate the result after execution; they do
not replace this command gate.

Manual no-input execution enters a dedicated permission-bootstrap branch. It
invokes only read-only iOS actions for clipboard, current/on-screen content,
screenshot, and alarms, immediately discards each result locally, and makes a
body-free request to the public `/health` endpoint. The bootstrap has no
mutation actions, receipt capabilities, authenticated data endpoints, or
private-data uploads. Authenticated Message input takes the command branch and
cannot fall through into the bootstrap. The read actions are private even when
their outputs are discarded from the network path; iOS may retain action
outputs in local Shortcut run history according to its own retention behavior.

Restrict the iPhone Message automation to the expected sender when self-message
matching works reliably. `Any Sender` is supported only with the current
secret-gated Shortcut and a private self-iMessage target; the `hola` content
filter should still remain as defense in depth. Do not add a branch that omits
the private prefix, turns arbitrary message text into commands, or runs an
arbitrary Shortcut.

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
