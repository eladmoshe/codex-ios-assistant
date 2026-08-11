# Architecture and protocol

The Mac sends commands through iMessage. The iPhone returns data through HTTPS.

## Components

1. The `iphone` CLI validates arguments and builds either an Apple URL or a Shortcut command.
2. The `iphone-control` skill tells Codex which CLI command to use and how to interpret its result.
3. A per-user LaunchAgent accepts commands on a Unix socket and automates Messages in the macOS GUI session.
4. A Message automation on the iPhone runs the 244-action Shortcut for messages that match `hola`; every native branch additionally requires the private command prefix.
5. The Shortcut runs an iOS action. Mutating branches post a versioned receipt to `/receipt`; branches that produce data post a bounded response to `/text`, `/photo`, `/clipboard`, or `/get-alarm` with the same receipt capability.
6. A named Cloudflare Tunnel sends requests for the public hostname to the receiver on `127.0.0.1:8787`.
7. The CLI polls the local receiver for text data or watches the private inbox for a screenshot until it gets a response or reaches the timeout.

## Command format

The sender accepts one newline-delimited JSON request whose command begins
with the configured 64-lowercase-hex private secret and `hola `. The Shortcut
extracts the newly received Message's `Content` property in its first native
action, then coerces that value to text. Every later branch consumes that
action output, and requires the exact private prefix before any native action.
The hardened CLI appends
machine-owned fields to every phone command:

```text
--v=2 --request-id=<32 lowercase hex> \
--receipt=<request id>.<64 lowercase hex> --action=<canonical action>
```

The full wire form is `<private-command-secret> hola ... <metadata>`. The
secret is generated into the mode-`0600` private config and rendered into the
private Shortcut; it is never accepted from model arguments. These fields are generated after the receiver registers the pending request;
they are not supplied by the model or by user command arguments. The current
finite command forms are:

```text
hola openurl <url>
hola homescreen
hola screenshot <id>
hola screentext <id>
hola getclipboard <id>
hola copytoclipboard <text>
hola alarm get <id>
hola alarm set <HH:MM> <label>
hola alarm off <HH:MM>
hola timer start <seconds>
hola timer pause
hola timer resume
hola timer cancel
hola flashlight on|off
hola lowpower on|off
hola controlcenter open|close
hola call <phone>
```

The typed CLI policy keeps the canonical receipt name separate from the legacy
wire command. For example, `camera.open`, `messages.compose`, and `url.open`
all use `hola openurl`, but the Shortcut echoes the trailer's `--action` value
so the receiver returns the exact typed name that Nami requested.

Use the CLI rather than writing or forwarding these messages yourself. The CLI checks times, phone numbers, URLs, and durations. The sender enforces the exact configured command prefix, command size, and line format.

## Response format

The hardened endpoints require `X-Protocol-Version: 2`,
`X-Request-Id`, and `X-Receipt-Capability`. The static `X-Auth` header remains
on the Shortcut as defense in depth and for backward-compatible interactive
paths, but it is not sufficient to authorize a Nami request. The old
`X-Screenshot-Id` header is accepted only by legacy static-token routes.

| Command | Shortcut request | CLI wait |
| --- | --- | --- |
| `hola screentext <id> ...` | Bounded JSON to `POST /text` with one-time capability | Poll the private registration socket for up to 30 seconds |
| `hola screenshot ...` | Bounded PNG/JPEG to `POST /photo` with one-time capability | Poll the private registration socket for up to 45 seconds |
| `hola getclipboard <id> ...` | Bounded text/JSON to `POST /clipboard` with one-time capability | Poll the private registration socket for up to 30 seconds |
| `hola alarm get <id> ...` | Bounded alarm data to `POST /get-alarm` with one-time capability | Poll the private registration socket for up to 30 seconds |
| any mutating command | `POST /receipt` with request ID, canonical action, and completed/failed status | Poll the private registration socket for the correlated receipt |

Legacy static-token screen, clipboard, and alarm responses remain in bounded
memory and are cleared by a receiver restart. Hardened protocol-v2 completions,
including bounded private text/data, are persisted for at most ten minutes in
the operator-owned mode-`0600` receiver state file so a restart cannot lose a
correlated result. The receiver saves screenshots under
`~/.local/share/codex-ios-assistant/inbox/` and purges receiver-owned screenshot
artifacts after the same ten-minute TTL; copy any screenshot needed for a
longer-lived log before then.

## CLI status values

- `dry-run`: the CLI printed the command without sending it.
- `completed`: the phone returned a matching data response or a versioned phone-side receipt.
- `failed`: a required service is missing or a helper returned an error.
- `timeout`: the receiver did not receive a matching receipt before the request expired.

There is no successful `requested` state in the hardened CLI. Messages accepting
the command is only transport progress; Nami reports success only after the
receiver consumes the matching, single-use phone-side receipt.

## Registration socket

Before sending an iMessage, the CLI registers `(request_id, hash(capability),
expected_action, expires_at)` over a separate mode-0600 Unix socket owned by
the receiver. The socket supports register, poll, and cancel operations and
checks the peer UID. A public receipt must match all of the following before it
is consumed: protocol version, request ID, capability hash, canonical action,
and an unexpired pending record. Pending capability hashes and bounded
completions are atomically persisted in an operator-owned mode-0600 state file,
so a receiver restart preserves the correlation contract. Missing state after
possible dispatch is still timeout/inconclusive, never inferred success or a
definitive failure that invites an unsafe retry.

## Messages sender

Sandboxed AppleScript can fail with `Unable to find application named 'Messages'` or an `LSCopyApplicationURLsForBundleIdentifier()` error. The sender LaunchAgent runs in the user's GUI domain, so it can resolve and automate Messages. Codex reaches it through a local socket.

The sender accepts a narrow input:

- The socket has mode `0600`.
- The sender checks the peer UID when macOS exposes it.
- Each request contains one UTF-8 JSON line with a 4 KiB encoded-byte limit;
  JSON escaping permits multiline and multibyte opaque values while the
  command rejects carriage returns and NUL bytes.
- The sender calls a fixed `/usr/bin/osascript` program. The client cannot supply an executable or script.
