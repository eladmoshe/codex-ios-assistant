#!/usr/bin/env python3
"""Check the public Shortcut template's structure and secret-free placeholders."""

from __future__ import annotations

import plistlib
from pathlib import Path
import sys


PUBLIC_PLACEHOLDER = "__IOS_ASSISTANT_PUBLIC_URL__"
TOKEN_PLACEHOLDER = "__IOS_ASSISTANT_RECEIVER_TOKEN__"
FORBIDDEN = ("@gmail.com", "/Users/", "trycloudflare.com")


def walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("shortcut/actions.template.plist")
    with path.open("rb") as source:
        actions = plistlib.load(source)
    if not isinstance(actions, list) or len(actions) != 243:
        raise SystemExit(f"expected 243 Shortcut actions, found {len(actions) if isinstance(actions, list) else 'non-list'}")

    strings = [value for value in walk(actions) if isinstance(value, str)]
    public_count = sum(value.count(PUBLIC_PLACEHOLDER) for value in strings)
    token_count = sum(value.count(TOKEN_PLACEHOLDER) for value in strings)
    if public_count != 20 or token_count != 20:
        raise SystemExit("expected twenty public URL and twenty receiver-token placeholders")
    folded = "\n".join(strings).casefold()
    for forbidden in FORBIDDEN:
        if forbidden.casefold() in folded:
            raise SystemExit(f"private-looking value remains in template: {forbidden}")
    if "hola say" in folded or "is.workflow.actions.speaktext" in folded:
        raise SystemExit("unsupported legacy speech branch remains in the Shortcut template")
    literal_web_origins = [
        value
        for value in strings
        if value.startswith("https://") and PUBLIC_PLACEHOLDER not in value
    ]
    if literal_web_origins:
        raise SystemExit("literal HTTPS origin remains in the Shortcut template")

    receipt_action_uuids: list[str] = []
    for action in actions:
        parameters = action.get("WFWorkflowActionParameters", {})
        if parameters.get("WFURL") != f"{PUBLIC_PLACEHOLDER}/receipt":
            continue
        headers = parameters.get("WFHTTPHeaders", {}).get("Value", {}).get(
            "WFDictionaryFieldValueItems", []
        )
        body = parameters.get("WFJSONValues", {}).get("Value", {}).get(
            "WFDictionaryFieldValueItems", []
        )
        header_names = {
            item.get("WFKey", {}).get("Value", {}).get("string") for item in headers
        }
        body_values = {
            item.get("WFKey", {}).get("Value", {}).get("string"): item.get("WFValue", {})
            for item in body
        }
        if header_names != {
            "X-Auth",
            "X-Protocol-Version",
            "X-Request-Id",
            "X-Receipt-Capability",
        } or body_values.get("protocol_version", {}).get("Value", {}).get("string") != "2":
            raise SystemExit("receipt branch is missing hardened headers or protocol version")
        request_header = next(
            item for item in headers
            if item.get("WFKey", {}).get("Value", {}).get("string") == "X-Request-Id"
        )
        request_uuid = (
            request_header.get("WFValue", {})
            .get("Value", {})
            .get("attachmentsByRange", {})
            .get("{0, 1}", {})
            .get("OutputUUID")
        )
        request_body = body_values.get("request_id", {})
        body_uuid = (
            request_body.get("Value", {})
            .get("attachmentsByRange", {})
            .get("{0, 1}", {})
            .get("OutputUUID")
        )
        if not isinstance(body_uuid, str) or request_uuid != body_uuid:
            raise SystemExit("receipt branch is missing dynamic action or matching request id")
        receipt_action_uuids.append(body_uuid)
    action_match_count = sum(
        value == r"(?<=--action=)[a-z0-9._-]+"
        for value in strings
    )
    if len(receipt_action_uuids) != 16 or len(set(receipt_action_uuids)) != 16 or action_match_count != 16:
        raise SystemExit("receipt branches do not expose one unique canonical action parser each")

    seen: set[str] = set()
    groups: list[str] = []
    for index, action in enumerate(actions):
        parameters = action.get("WFWorkflowActionParameters", {})
        for value in walk(parameters):
            if isinstance(value, dict) and value.get("Type") == "ActionOutput":
                output_uuid = value.get("OutputUUID")
                if output_uuid not in seen:
                    raise SystemExit(f"action {index} has unresolved output reference {output_uuid}")
        mode = parameters.get("WFControlFlowMode")
        group = parameters.get("GroupingIdentifier")
        if mode == 0:
            groups.append(group)
        elif mode == 2:
            if not groups or groups.pop() != group:
                raise SystemExit(f"unbalanced control-flow group at action {index}")
        action_uuid = parameters.get("UUID")
        if action_uuid:
            seen.add(action_uuid)
    if groups:
        raise SystemExit("unclosed Shortcut control-flow groups")
    print(f"validated {len(actions)} sanitized Shortcut actions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
