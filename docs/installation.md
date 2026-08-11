# Installation

The first setup takes about 20 minutes. macOS and iOS will stop for permission prompts, and you must create the Message automation on the iPhone.

## Before you start

You need:

- A Mac running macOS 14 or newer.
- Python 3.11 or newer.
- Xcode Command Line Tools for the Contacts helper.
- Messages signed into iMessage on the Mac.
- An iPhone that receives messages sent to your chosen iMessage address.
- iCloud sync enabled for Shortcuts.
- A domain whose DNS is managed by Cloudflare.

Install Xcode Command Line Tools if `swift --version` fails:

```bash
xcode-select --install
```

After an Xcode update, Apple may also require:

```bash
sudo xcodebuild -license
```

Install the ChatGPT desktop and mobile apps if you plan to use [Remote connections](https://learn.chatgpt.com/docs/remote-connections.md).

## 1. Install the project

```bash
git clone https://github.com/samin100/codex-ios-assistant.git
cd codex-ios-assistant
brew install cloudflared steipete/tap/imsg
./scripts/install
```

The install script:

- creates `.venv` and installs the Python package in editable mode;
- links `iphone` into `~/.local/bin`;
- builds the Contacts helper when Swift is available;
- links `skills/iphone-control` into `~/.agents/skills/iphone-control`;
- opens the configuration prompt when no config file exists.

Add `~/.local/bin` to your `PATH` if `command -v iphone` returns nothing:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Put that line in `~/.zshrc`, then open a new terminal or run `source ~/.zshrc`.

## 2. Configure the private values

Run the prompt again at any time with:

```bash
./scripts/configure
```

The config contains:

- `IPHONE_MSG_TARGET`, an iMessage email address or phone number that reaches the iPhone;
- `IPHONE_PUBLIC_URL`, a dedicated HTTPS origin with no path;
- `IPHONE_RECEIVER_TOKEN`, which the script generates without a prompt and does not print.

The script writes `~/.config/codex-ios-assistant/config.env` with mode `0600`. Rerun it to change the target or hostname. Add `--force-token` to replace the token, then rebuild the Shortcut so both sides use the new value.

For an unattended setup:

```bash
./scripts/configure \
  --target 'your-imessage-address@example.com' \
  --url 'https://iphone.example.com'
```

## 3. Create the tunnel

```bash
./scripts/setup-cloudflare
```

On the first run, `cloudflared` opens a browser for Cloudflare authorization. Choose the account that owns the DNS zone. The script creates a tunnel named `codex-ios-assistant`, adds the DNS route, and writes `~/.config/codex-ios-assistant/cloudflared.yml`.

The receiver remains on `127.0.0.1:8787`; Cloudflare sends requests for the configured hostname to that loopback address. A named tunnel keeps the same hostname across reboots. See [Cloudflare Tunnel](cloudflare.md) for the generated config and DNS behavior.

## 4. Start the background jobs

```bash
./scripts/install-services
```

The script installs these per-user LaunchAgents:

| Label | Process |
| --- | --- |
| `io.github.codex-ios-assistant.sender` | Sends `hola` commands through Messages outside the Codex sandbox |
| `io.github.codex-ios-assistant.receiver` | Receives phone responses on `127.0.0.1:8787` |
| `io.github.codex-ios-assistant.tunnel` | Runs the named Cloudflare Tunnel |

The jobs start at login and restart after a crash. Logs are under `~/Library/Logs/codex-ios-assistant/`.

The first `iphone` command may prompt you to let the sender's Python process control Messages. Grant access in System Settings > Privacy & Security > Automation.

## 5. Paste the Shortcut

```bash
./scripts/copy-shortcut
```

This command reads the private hostname and token, renders `build/ios-assistant-actions.plist` with mode `0600`, and writes 244 Shortcuts action items to the Mac pasteboard. The generated Shortcut includes the version-2 receipt branches; it is not interchangeable with an older 95-action export.

In Shortcuts on the Mac:

1. Create a blank shortcut.
2. Name it `iOS Assistant`.
3. Click the empty action canvas.
4. Press Command-V once.
5. Wait for all 244 actions to appear, then close the editor.

Do not copy plist text into Shortcuts. The Swift helper has placed native `com.apple.shortcuts.action` items on the pasteboard.

Confirm Shortcuts sync under System Settings > Apple Account > iCloud. The Shortcut should appear on the iPhone after iCloud sync runs.

## 6. Create the iPhone automation

Personal automations belong to one device, so create this part on the iPhone:

1. Open Shortcuts > Automation and tap the plus sign at the bottom.
2. Choose New Automation > Message.
3. Under `When I receive a message where`, set `Sender is` to your own iMessage contact when self-message matching works on your iOS version. Otherwise choose `Any Sender`; every current Shortcut branch independently requires the generated private command secret before it can mutate the phone.
4. Tap `Add Filter` and set `Message contains` to `hola`.
5. Choose `Run Immediately` and turn off any confirmation prompt.
6. Tap `Run Shortcut` and select `iOS Assistant`.
7. Save the automation.

You must create this automation by hand in the Shortcuts app on the iPhone. It does not sync from the Mac with the Shortcut.

Apple describes the relevant setting in [Enable or disable a personal automation](https://support.apple.com/guide/shortcuts/enable-or-disable-a-personal-automation-apd602971e63/ios).

Prefer the sender restriction when it works. Some iOS versions do not classify
self-addressed messages under the expected contact; in that case `Any Sender`
is supported with the current secret-gated Shortcut. Keep the `hola` content
condition, never share the rendered Shortcut, and do not use an older Shortcut
without the private-prefix branch conditions.

## 7. Grant iPhone permissions

Run the Shortcut by hand once. iOS may ask for access to the receiver hostname, on-screen content, screenshots, alarms, or the clipboard as it reaches each branch. Choose Allow for features you plan to use.

The installed Shortcut contains the receiver token and private command secret. Keep the Shortcut and `build/ios-assistant-actions.plist` private.

## 8. Test each connection

Check the Mac jobs:

```bash
iphone doctor
curl http://127.0.0.1:8787/health
curl "$(sed -n 's/^IPHONE_PUBLIC_URL="\(.*\)"$/\1/p' ~/.config/codex-ios-assistant/config.env)/health"
```

Both health requests should print:

```text
codex-ios-assistant receiver up
```

Test iMessage delivery without involving the tunnel:

```bash
iphone home
```

Messages should show a private hexadecimal prefix followed by `hola homescreen`, and the iPhone should open its Home Screen. Do not share or paste that wire message.

Test the response paths:

```bash
iphone screen read --timeout 30
iphone screen capture --timeout 45
iphone alarm list --timeout 30
iphone clipboard get --timeout 30
```

`screen capture` prints a path under `~/.local/share/codex-ios-assistant/inbox/`.

## Mac Contacts and Messages

The first Contacts search may prompt for Contacts access:

```bash
iphone contacts search 'Jane Appleseed'
```

`imsg` reads the local Messages database and supports message history plus existing-group draft resolution:

```bash
brew install steipete/tap/imsg
imsg chats
```

Grant Full Disk Access to the application that hosts Codex so `imsg` can read `~/Library/Messages/chat.db`. The [`imsg` README](https://github.com/openclaw/imsg) lists its macOS requirements.

Find My needs no helper. The CLI opens `findmy://` links and uses the Contacts helper when you pass a person's name.

## ChatGPT Remote

Remote lets the ChatGPT app on the iPhone control a Codex session running on the Mac:

1. Sign in to the ChatGPT desktop app on the Mac.
2. Open Settings > Connections > Control this Mac or PC.
3. Open Codex in the ChatGPT mobile app and scan the QR code.
4. Keep the Mac awake and signed into the same ChatGPT account or workspace.

The remote session uses the Mac's files, skills, tools, and permissions. OpenAI maintains the setup details in [Remote connections](https://learn.chatgpt.com/docs/remote-connections.md).

## Update or remove the jobs

After pulling an update:

```bash
git pull --ff-only
./scripts/install
./scripts/install-services
```

If `shortcut/actions.template.plist` changed, run `scripts/copy-shortcut` and paste the actions into a new blank Shortcut. Point the automation at the new copy after it passes a test.

Stop the background jobs with:

```bash
./scripts/uninstall-services
```

The uninstall script moves its LaunchAgent plists to the Trash. It leaves your config, logs, screenshots, Cloudflare tunnel, and iPhone Shortcut in place.
