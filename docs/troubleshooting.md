# Troubleshooting

## Messages cannot be found

Errors may include:

```text
Unable to find application named 'Messages'
LSCopyApplicationURLsForBundleIdentifier() failed ... com.apple.MobileSMS
```

These errors occur when sandboxed AppleScript cannot resolve Messages. Use the per-user sender instead of a direct AppleScript transport:

```bash
./scripts/install
./scripts/install-services
iphone doctor
```

`iphone doctor` should report the Messages sender socket as available. Check `~/Library/Logs/codex-ios-assistant/sender.log` if it does not.

## Messages automation is denied

Open System Settings > Privacy & Security > Automation. Allow the Python process used by `io.github.codex-ios-assistant.sender` to control Messages. Confirm that Messages opens and has an enabled iMessage account.

Restart the sender after changing the permission:

```bash
launchctl kickstart -k "gui/$(id -u)/io.github.codex-ios-assistant.sender"
```

## The iMessage arrives but the iPhone does nothing

- Confirm that the message reached the intended iPhone.
- Check Shortcuts > Automation and make sure the Message automation runs without confirmation.
- Check that `Message Contains` is `hola` and that the automation runs `iOS Assistant`.
- Run the Shortcut by hand to expose pending permission prompts.

Start with `iphone home`. That command tests Messages and the automation without using the tunnel.

## Shortcuts reports a lost network connection

Test the receiver first, then the tunnel:

```bash
curl http://127.0.0.1:8787/health
curl https://iphone.example.com/health
launchctl print "gui/$(id -u)/io.github.codex-ios-assistant.receiver"
launchctl print "gui/$(id -u)/io.github.codex-ios-assistant.tunnel"
tail -n 100 ~/Library/Logs/codex-ios-assistant/receiver.log
tail -n 100 ~/Library/Logs/codex-ios-assistant/tunnel.log
```

Use your configured hostname in the second command. If local health works and public health fails, rerun `scripts/setup-cloudflare` and `scripts/install-services`. If both work, rebuild the Shortcut so it contains the current hostname and token.

On the iPhone, run the Shortcut by hand and grant access to the hostname when iOS asks.

## A response command times out

The CLI polls every 0.5 seconds. Screen text, alarms, and clipboard commands wait 30 seconds by default; screenshots wait 45 seconds.

Check the path in order:

1. Look for the command in Messages. If it is missing, inspect the sender log.
2. Watch the iPhone automation. If it does not start, check its Message conditions.
3. Run the matching Shortcut branch by hand to expose an iOS error or permission prompt.
4. Check the receiver log for a POST with the request ID from the message.
5. Confirm that the Shortcut passed the same ID in `X-Request-Id` and the
   one-time capability in `X-Receipt-Capability`. The old `X-Screenshot-Id`
   header is only for legacy static-token requests.

Increase the timeout for a slow phone without changing the default:

```bash
iphone screen read --timeout 60
```

## `imsg` cannot find PhoneNumberKit

An old standalone `imsg` binary may be missing its Swift resource bundle. Find every copy and install the Homebrew package:

```bash
which -a imsg
brew install steipete/tap/imsg
hash -r
imsg --version
```

The project invokes `imsg` from `PATH`; it does not install a wrapper under that name.

## `imsg` cannot read Messages

Grant Full Disk Access to the application running Codex, then restart that application. Check that `~/Library/Messages/chat.db` exists and that Messages has finished syncing.

## Contacts search fails

Run the helper to trigger the macOS permission prompt:

```bash
contacts search 'Jane'
```

If `contacts` is missing, install Xcode Command Line Tools, accept the Xcode license, and rerun `scripts/install`.

## The pasted Shortcut is incomplete

- For a fresh install, paste into a new blank Shortcut. For an update, open the
  existing stable `iOS Assistant` Shortcut, select and delete all current
  actions, then paste the full list in place so the Message automation target
  stays unchanged.
- Click the canvas before pressing Command-V.
- Press Command-V once.
- Rerun `scripts/copy-shortcut` if the pasteboard contains something else.
- Check for `Copied and verified 244 Shortcuts actions` in the terminal.
- Run `make test` to validate the committed plist.

Do not create a new versioned Shortcut or repoint the automation; save the
existing Shortcut only after all 244 actions appear and validation passes.
