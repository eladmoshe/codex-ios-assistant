#!/usr/bin/env python3
"""Check the public Shortcut template's structure and secret-free placeholders."""

from __future__ import annotations

import plistlib
from pathlib import Path
import sys


PUBLIC_PLACEHOLDER = "__IOS_ASSISTANT_PUBLIC_URL__"
TOKEN_PLACEHOLDER = "__IOS_ASSISTANT_RECEIVER_TOKEN__"
COMMAND_SECRET_PLACEHOLDER = "__IOS_ASSISTANT_COMMAND_SECRET__"
FORBIDDEN = ("@gmail.com", "/Users/", "trycloudflare.com")
NORMALIZED_COMMAND_UUID = "7A4E12C1-5E7D-4EFA-9C15-0A2F663E1C21"
MESSAGE_CONTENT_AGGRANDIZEMENT = {
    "PropertyName": "Content",
    "Type": "WFPropertyVariableAggrandizement",
}
TIMER_DURATION_PATTERN = r"(?i)[0-9]+(?=\s+--v=2\s+--request-id=[0-9a-f]{32}\s+--receipt=[0-9a-f]{32}\.[0-9a-f]{64}\s+--action=timer\.start\s*$)"
EXPECTED_COMMAND_CONDITIONS = {
    f"{COMMAND_SECRET_PLACEHOLDER} hola timer start",
    f"{COMMAND_SECRET_PLACEHOLDER} hola timer pause",
    f"{COMMAND_SECRET_PLACEHOLDER} hola timer resume",
    f"{COMMAND_SECRET_PLACEHOLDER} hola timer cancel",
    f"{COMMAND_SECRET_PLACEHOLDER} hola flashlight on",
    f"{COMMAND_SECRET_PLACEHOLDER} hola flashlight off",
    f"{COMMAND_SECRET_PLACEHOLDER} hola call",
    f"{COMMAND_SECRET_PLACEHOLDER} hola lowpower on",
    f"{COMMAND_SECRET_PLACEHOLDER} hola lowpower off",
    f"{COMMAND_SECRET_PLACEHOLDER} hola copytoclipboard",
    f"{COMMAND_SECRET_PLACEHOLDER} hola getclipboard",
    f"{COMMAND_SECRET_PLACEHOLDER} hola controlcenter open",
    f"{COMMAND_SECRET_PLACEHOLDER} hola controlcenter close",
    f"{COMMAND_SECRET_PLACEHOLDER} hola openurl",
    f"{COMMAND_SECRET_PLACEHOLDER} hola screentext",
    f"{COMMAND_SECRET_PLACEHOLDER} hola screenshot",
    f"{COMMAND_SECRET_PLACEHOLDER} hola homescreen",
    f"{COMMAND_SECRET_PLACEHOLDER} hola alarm get",
    f"{COMMAND_SECRET_PLACEHOLDER} hola alarm set",
    f"{COMMAND_SECRET_PLACEHOLDER} hola alarm off",
}


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
    if not isinstance(actions, list) or len(actions) != 244:
        raise SystemExit(f"expected 244 Shortcut actions, found {len(actions) if isinstance(actions, list) else 'non-list'}")
    normalization = actions[0]
    normalization_parameters = normalization.get("WFWorkflowActionParameters", {})
    normalization_input = normalization_parameters.get("WFInput", {})
    normalization_value = normalization_input.get("Value", {})
    if (
        normalization.get("WFWorkflowActionIdentifier") != "is.workflow.actions.detect.text"
        or normalization_parameters.get("UUID") != NORMALIZED_COMMAND_UUID
        or normalization_input.get("WFSerializationType") != "WFTextTokenAttachment"
        or normalization_value.get("Type") != "ExtensionInput"
        or normalization_value.get("Aggrandizements") != [MESSAGE_CONTENT_AGGRANDIZEMENT]
    ):
        raise SystemExit("first action must extract the Message Content property before text coercion")
    leaked_extension_inputs = [
        index
        for index, action in enumerate(actions[1:], start=1)
        if any(
            isinstance(value, dict) and value.get("Type") == "ExtensionInput"
            for value in walk(action)
        )
    ]
    if leaked_extension_inputs:
        raise SystemExit(
            f"actions still consume the raw Message automation input: {leaked_extension_inputs}"
        )

    strings = [value for value in walk(actions) if isinstance(value, str)]
    public_count = sum(value.count(PUBLIC_PLACEHOLDER) for value in strings)
    token_count = sum(value.count(TOKEN_PLACEHOLDER) for value in strings)
    command_secret_count = sum(value.count(COMMAND_SECRET_PLACEHOLDER) for value in strings)
    if public_count != 20 or token_count != 20:
        raise SystemExit("expected twenty public URL and twenty receiver-token placeholders")
    if command_secret_count != 28:
        raise SystemExit("expected twenty-eight private command-secret placeholders")
    timer_duration_patterns = [
        action.get("WFWorkflowActionParameters", {}).get("WFMatchTextPattern")
        for action in actions
        if action.get("WFWorkflowActionIdentifier") == "is.workflow.actions.text.match"
        and action.get("WFWorkflowActionParameters", {}).get("UUID")
        == "E9A5A5F0-0001-4CCC-8CCC-000000000001"
    ]
    if timer_duration_patterns != [TIMER_DURATION_PATTERN]:
        raise SystemExit("timer duration parser must bind digits to the timer.start trailer")
    folded = "\n".join(strings).casefold()
    for forbidden in FORBIDDEN:
        if forbidden.casefold() in folded:
            raise SystemExit(f"private-looking value remains in template: {forbidden}")
    if "hola say" in folded or "is.workflow.actions.speaktext" in folded:
        raise SystemExit("unsupported legacy speech branch remains in the Shortcut template")
    identifiers = {
        action.get("WFWorkflowActionIdentifier")
        for action in actions
        if isinstance(action, dict)
    }
    unsupported_screen_actions = {
        "is.workflow.actions.getonscreencontext",
        "is.workflow.actions.getselectedtext",
    }
    present_unsupported = sorted(unsupported_screen_actions.intersection(identifiers))
    if present_unsupported:
        raise SystemExit(f"unsupported iPhone screen actions present: {', '.join(present_unsupported)}")
    if "is.workflow.actions.getonscreencontent" not in identifiers:
        raise SystemExit("iPhone-compatible on-screen content action is missing")

    command_conditions = []
    for index, action in enumerate(actions):
        if action.get("WFWorkflowActionIdentifier") != "is.workflow.actions.conditional":
            continue
        parameters = action.get("WFWorkflowActionParameters", {})
        command = parameters.get("WFConditionalActionString")
        if isinstance(command, str) and command.startswith(f"{COMMAND_SECRET_PLACEHOLDER} hola "):
            if not any(
                isinstance(value, dict)
                and value.get("Type") == "ActionOutput"
                and value.get("OutputUUID") == NORMALIZED_COMMAND_UUID
                for value in walk(parameters.get("WFInput", {}))
            ):
                raise SystemExit(f"action {index} does not compare normalized command text")
            command_conditions.append((index, command, parameters.get("WFCondition")))
    command_names = [command for _, command, _ in command_conditions]
    if len(command_names) != len(EXPECTED_COMMAND_CONDITIONS) or set(command_names) != EXPECTED_COMMAND_CONDITIONS:
        missing = sorted(EXPECTED_COMMAND_CONDITIONS.difference(command_names))
        unexpected = sorted(set(command_names).difference(EXPECTED_COMMAND_CONDITIONS))
        duplicates = sorted({command for command in command_names if command_names.count(command) > 1})
        raise SystemExit(
            "Shortcut command conditions must contain the exact command set once each: "
            f"missing={missing}, unexpected={unexpected}, duplicates={duplicates}"
        )
    prefix_overlaps = sorted(
        (left, right)
        for left in command_names
        for right in command_names
        if left != right and right.startswith(left)
    )
    if prefix_overlaps:
        raise SystemExit(f"Shortcut begins-with command conditions overlap: {prefix_overlaps}")
    incompatible_conditions = [
        f"action {index} ({command}) uses {condition!r}"
        for index, command, condition in command_conditions
        if condition != 8
    ]
    if incompatible_conditions:
        raise SystemExit(
            "iPhone command conditions must use begins-with mode 8: "
            + "; ".join(incompatible_conditions)
        )
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
        receipt_action = body_values.get("receipt_action", {})
        if not isinstance(body_uuid, str) or request_uuid != body_uuid or not receipt_action:
            raise SystemExit("receipt branch is missing canonical action or matching request id")
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
