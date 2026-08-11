#!/usr/bin/env python3
"""Render the private Shortcut action list from the committed public template."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import sys


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from iphone_cli.config import command_secret, public_url, receiver_token  # noqa: E402


PUBLIC_PLACEHOLDER = "__IOS_ASSISTANT_PUBLIC_URL__"
TOKEN_PLACEHOLDER = "__IOS_ASSISTANT_RECEIVER_TOKEN__"
COMMAND_SECRET_PLACEHOLDER = "__IOS_ASSISTANT_COMMAND_SECRET__"


def replace(value: object, substitutions: dict[str, str], counts: dict[str, int]) -> object:
    if isinstance(value, dict):
        return {key: replace(item, substitutions, counts) for key, item in value.items()}
    if isinstance(value, list):
        return [replace(item, substitutions, counts) for item in value]
    if isinstance(value, str):
        result = value
        for placeholder, replacement in substitutions.items():
            found = result.count(placeholder)
            if found:
                counts[placeholder] += found
                result = result.replace(placeholder, replacement)
        return result
    return value


def main() -> int:
    template = REPO / "shortcut" / "actions.template.plist"
    destination = REPO / "build" / "ios-assistant-actions.plist"
    with template.open("rb") as source:
        actions = plistlib.load(source)
    counts = {
        PUBLIC_PLACEHOLDER: 0,
        TOKEN_PLACEHOLDER: 0,
        COMMAND_SECRET_PLACEHOLDER: 0,
    }
    rendered = replace(
        actions,
        {
            PUBLIC_PLACEHOLDER: public_url(),
            TOKEN_PLACEHOLDER: receiver_token(),
            COMMAND_SECRET_PLACEHOLDER: command_secret(),
        },
        counts,
    )
    if not isinstance(rendered, list) or not rendered:
        raise SystemExit("Shortcut template must contain a non-empty action array.")
    if any(count == 0 for count in counts.values()):
        raise SystemExit(f"Shortcut template did not contain all placeholders: {counts}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    temporary = destination.with_suffix(".plist.tmp")
    with temporary.open("wb") as output:
        plistlib.dump(rendered, output, fmt=plistlib.FMT_XML, sort_keys=False)
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
