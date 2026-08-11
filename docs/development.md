# Development

## Tests

The Python package has no runtime dependencies. Run the checkout without installing it:

```bash
./iphone --help
make test
```

`make test` runs the Python suite, compiles Python sources, and validates the Shortcut plist. Build the Contacts helper on macOS with:

```bash
swift build --package-path contacts --configuration release
```

## Project rules

- Keep required user config to the three values in `.env.example`.
- Bind the receiver to loopback and authenticate every data endpoint.
- Keep phone, Contacts, and Messages content out of logs.
- Treat Messages delivery as transport progress only. Hardened phone actions
  become `completed` only after the matching version-2 receipt is consumed.
- Validate input before building a `hola` command or app URL.
- Keep message sending, purchases, ride requests, app installation, and alarm deletion out of the CLI.
- Document Apple UI steps that scripts cannot perform.
- Add tests for parser, URL, and response-format changes.

## Shortcut changes

Read [Shortcut maintenance](shortcut.md) before editing the plist. Output UUIDs and control-flow groups can break when actions move. Reproduce unfamiliar actions in a scratch Shortcut, inspect their pasteboard plist, and test the rebuilt Shortcut on an iPhone.

## Release check

1. Run `make test` and build Contacts.
2. Install the rendered Shortcut as a new copy.
3. Test one-way commands, the registration socket, receipt replay rejection,
   and all four hardened response endpoints.
4. Test unlabeled alarms and multiple enabled alarms at one time.
5. Inspect the staged diff for private data.
6. Update the version in `pyproject.toml` and `src/iphone_cli/__init__.py`.
7. Tag the release after the macOS CI run passes.
