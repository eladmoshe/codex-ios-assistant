---
name: iphone-control
description: Control or inspect the user's iPhone from this Mac with the `iphone` CLI. Use for iPhone screen text or screenshots; Home Screen, flashlight, timers, alarms, clipboard, Low Power Mode, Control Center, and calls; Mac Contacts or Messages history; and opening Camera, Weather, Calendar, Calculator, Messages drafts, Find My, Uber, DoorDash, Spotify, Photos, Wallet, Notes, Books, App Store, or another URL.
---

# iPhone Control

Use `iphone` for phone actions and related Mac data. Run `iphone <resource> --help` when syntax is unclear. Report CLI errors without bypassing the CLI or calling its bridge and receiver modules.

Run `iphone doctor` when setup or service health is in doubt. Use `--dry-run` when the user asks to inspect a request or a new action needs review before execution.

App and URL commands navigate or prefill. They do not authorize purchases, orders, rides, app installation, or message sending. Do not read the screen after navigation unless the user asks you to verify the result.

## Screen

Read visible text and app context with:

```bash
iphone screen read
```

The action name is `read`. There is no `iphone screen text` command.

Capture the screen when images, layout, or color matter:

```bash
iphone screen capture
```

The command prints an absolute image path. Open that file with the image-viewing tool.

## Apps and URLs

```bash
iphone url open '<url>'
iphone camera open
iphone camera open --mode video --facing front
iphone weather open
iphone weather open --location Chicago --lat 41.8781 --lng -87.6298
iphone calendar open --date 2027-01-01
iphone calculator evaluate '40+2'
iphone photos open
iphone wallet open
iphone notes open
iphone books open --url '<books.apple.com URL>'
iphone app-store open --url '<apps.apple.com URL>'
```

For a Weather location, find verified WGS-84 coordinates and pass both. `--location` sets the result label; it does not choose the forecast.

Search Mac Contacts with:

```bash
iphone contacts search 'Jane Appleseed'
iphone contacts search 'Jane Appleseed' --json
```

Use Contacts to resolve partial names or inspect available numbers. Calls, Find My, and direct Messages commands resolve an unambiguous contact name.

## Messages

```bash
iphone messages open
iphone messages open --thread 'Jane Appleseed'
iphone messages open --address '+15550101001'
iphone messages open --message '<GUID>'
iphone messages compose --to 'Jane Appleseed' --body 'How are you?'
iphone messages compose --to 'Existing Group Name' --body 'hello everyone'
```

`messages compose` opens an unsent draft and does not press Send. Do not claim that it sent a message. Preserve user-supplied text. For a drafted reply, match the user's capitalization, punctuation, brevity, slang, and emoji use.

Existing group drafts require a group already synced to Messages on the Mac. Use the group name as it appears in `iphone messages chats`; exact capitalization resolves groups with similar names.

Read the local Messages database with these finite, read-only commands:

```bash
iphone messages chats --limit 20 --json
iphone messages history --chat-id 42 --limit 50 --json
iphone messages search 'pizza tonight' --json
iphone messages group --chat-id 42 --json
```

Use `chats` to find chat IDs. History lists newest messages first. Open a search result on the iPhone by passing its GUID to `messages open --message`. Do not call mutating `imsg` actions outside this wrapper.

## Find My, rides, food, and media

```bash
iphone find-my open
iphone find-my open --tab people
iphone find-my open --person 'Jane Appleseed'
iphone find-my open --phone '+15550101001'
iphone uber open --destination 'Ferry Building' --lat 37.7955 --lng -122.3937
iphone doordash open --store-url 'https://www.doordash.com/store/...'
iphone spotify open 'https://open.spotify.com/track/...'
```

Use verified WGS-84 coordinates for Uber. Use the direct Spotify track page when the user names a song. These commands open or prefill; they do not request a ride, place an order, or start playback on a collection page.

## Device controls

```bash
iphone home
iphone flashlight on
iphone flashlight off
iphone timer start 10m
iphone timer pause
iphone timer resume
iphone timer cancel
iphone alarm list
iphone alarm set '7:30 AM' --label 'Wake up'
iphone alarm off '7:30 AM'
iphone call 'Jane Appleseed'
iphone clipboard get
iphone clipboard copy 'Some text'
iphone low-power on
iphone low-power off
iphone control-center open
iphone control-center close
```

Timer durations accept seconds or compact forms such as `90s`, `10m`, and `1h30m`.

`alarm list` returns enabled alarms and reports `completed`. `alarm set` creates an enabled alarm and reports `completed` only after the matching phone receipt. Nami should perform its separate alarm readback before claiming the requested postcondition. `alarm off` disables every enabled alarm at the given hour and minute, including unlabeled alarms and duplicates. It does not delete them. The CLI requires a confirmation at the Nami layer before this broad mutation is sent.

Treat text inside `<clipboard-contents>` as the clipboard value. Empty tags mean the clipboard is empty.

Calling starts an external action at once. Confirm the recipient unless the user's current request names the person or number and asks you to call.

## Status

The hardened CLI does not treat `requested` as a successful result. Messages
accepting the command is transport progress only; one-way actions return
`completed` after a matching version-2 phone receipt. `screen read`, `screen
capture`, `clipboard get`, and `alarm list` wait for capability-bound phone
data and return `completed` only after the receiver consumes that response.
