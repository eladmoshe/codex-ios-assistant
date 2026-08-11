#!/usr/bin/env python3
"""Regenerate the sanitized Shortcut template with v2 receipt branches.

The native plist is difficult to edit safely by hand.  This script is kept in
the repository so future Shortcut maintenance can reproduce the exact action
shape instead of editing a rendered, secret-bearing pasteboard export.
"""

from __future__ import annotations

import plistlib
from pathlib import Path
import uuid


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "shortcut" / "actions.template.plist"
PUBLIC = "__IOS_ASSISTANT_PUBLIC_URL__"
TOKEN = "__IOS_ASSISTANT_RECEIVER_TOKEN__"
COMMAND_SECRET = "__IOS_ASSISTANT_COMMAND_SECRET__"
NORMALIZED_COMMAND_UUID = "7A4E12C1-5E7D-4EFA-9C15-0A2F663E1C21"
MESSAGE_CONTENT_AGGRANDIZEMENT = {
    "PropertyName": "Content",
    "Type": "WFPropertyVariableAggrandizement",
}
TIMER_DURATION_UUID = "E9A5A5F0-0001-4CCC-8CCC-000000000001"
TIMER_DURATION_PATTERN = r"(?i)[0-9]+(?=\s+--v=2\s+--request-id=[0-9a-f]{32}\s+--receipt=[0-9a-f]{32}\.[0-9a-f]{64}\s+--action=timer\.start\s*$)"
NAMESPACE = uuid.UUID("fae2f1d4-ae8e-55d4-bc9a-e4b0e6b1f5a8")


def uid(branch: str, name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{branch}:{name}")).upper()


def attachment(output_name: str, output_uuid: str) -> dict[str, object]:
    return {
        "Value": {
            "attachmentsByRange": {
                "{0, 1}": {
                    "OutputName": output_name,
                    "OutputUUID": output_uuid,
                    "Type": "ActionOutput",
                }
            },
            "string": "￼",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def dictionary_value(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "Value": {"WFDictionaryFieldValueItems": items},
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def static_text(value: str) -> dict[str, object]:
    return {"Value": {"string": value}, "WFSerializationType": "WFTextTokenString"}


def field(key: str, value: dict[str, object]) -> dict[str, object]:
    return {"WFItemType": 0, "WFKey": static_text(key), "WFValue": value}


def extension_input() -> dict[str, object]:
    return {
        "Value": {
            "attachmentsByRange": {"{0, 1}": {"Type": "ExtensionInput"}},
            "string": "￼",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def message_content_input() -> dict[str, object]:
    """Extract the Message automation's Content property before text coercion."""
    return {
        "Value": {
            "Aggrandizements": [MESSAGE_CONTENT_AGGRANDIZEMENT.copy()],
            "Type": "ExtensionInput",
        },
        "WFSerializationType": "WFTextTokenAttachment",
    }


def harden_command_input(actions: list[dict[str, object]]) -> bool:
    """Extract Message Content and require the private prefix before any branch."""
    changed = False
    if not (
        actions
        and actions[0].get("WFWorkflowActionIdentifier") == "is.workflow.actions.detect.text"
        and actions[0].get("WFWorkflowActionParameters", {}).get("UUID")
        == NORMALIZED_COMMAND_UUID
    ):
        actions.insert(
            0,
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.detect.text",
                "WFWorkflowActionParameters": {
                    "UUID": NORMALIZED_COMMAND_UUID,
                    "WFInput": message_content_input(),
                },
            },
        )
        changed = True

    if actions and actions[0].get("WFWorkflowActionIdentifier") == "is.workflow.actions.detect.text":
        parameters = actions[0].setdefault("WFWorkflowActionParameters", {})
        if parameters.get("UUID") == NORMALIZED_COMMAND_UUID:
            current_input = parameters.get("WFInput")
            expected_input = message_content_input()
            if current_input != expected_input:
                parameters["WFInput"] = expected_input
                changed = True

    def visit(value: object, *, keep_extension_input: bool = False) -> None:
        nonlocal changed
        if isinstance(value, dict):
            if value.get("Type") == "ExtensionInput" and not keep_extension_input:
                value.clear()
                value.update(
                    {
                        "OutputName": "Text",
                        "OutputUUID": NORMALIZED_COMMAND_UUID,
                        "Type": "ActionOutput",
                    }
                )
                changed = True
                return
            for child in value.values():
                visit(child, keep_extension_input=keep_extension_input)
        elif isinstance(value, list):
            for child in value:
                visit(child, keep_extension_input=keep_extension_input)

    for index, item in enumerate(actions):
        visit(item, keep_extension_input=index == 0)
        parameters = item.get("WFWorkflowActionParameters", {})
        condition = parameters.get("WFConditionalActionString")
        if isinstance(condition, str) and condition.startswith("hola "):
            parameters["WFConditionalActionString"] = f"{COMMAND_SECRET} {condition}"
            changed = True
        pattern = parameters.get("WFReplaceTextFind")
        if isinstance(pattern, str) and r"^\s*hola" in pattern:
            parameters["WFReplaceTextFind"] = pattern.replace(
                r"^\s*hola", rf"^\s*{COMMAND_SECRET}\s+hola"
            )
            changed = True
        if (
            item.get("WFWorkflowActionIdentifier") == "is.workflow.actions.text.match"
            and parameters.get("UUID") == TIMER_DURATION_UUID
            and parameters.get("WFMatchTextPattern") != TIMER_DURATION_PATTERN
        ):
            parameters["WFMatchTextPattern"] = TIMER_DURATION_PATTERN
            changed = True
    return changed


def receipt_action_parser(branch: str) -> tuple[list[dict[str, object]], str]:
    match_uuid = uid(branch, "receipt-action-match")
    action_uuid = uid(branch, "receipt-action")
    return [
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.text.match",
            "WFWorkflowActionParameters": {
                "UUID": match_uuid,
                "WFMatchTextCaseSensitive": False,
                "WFMatchTextPattern": r"(?<=--action=)[a-z0-9._-]+",
                "text": extension_input(),
            },
        },
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.getitemfromlist",
            "WFWorkflowActionParameters": {
                "UUID": action_uuid,
                "WFInput": {
                    "Value": {
                        "OutputName": "Matches",
                        "OutputUUID": match_uuid,
                        "Type": "ActionOutput",
                    },
                    "WFSerializationType": "WFTextTokenAttachment",
                },
                # Opaque command arguments may themselves contain a marker;
                # the machine-owned trailer is always the final match.
                "WFItemSpecifier": "Last Item",
            },
        },
    ], action_uuid


def receipt_parser(branch: str) -> tuple[list[dict[str, object]], tuple[str, str, str]]:
    match_uuid = uid(branch, "receipt-match")
    token_uuid = uid(branch, "receipt-token")
    split_uuid = uid(branch, "receipt-split")
    capability_uuid = uid(branch, "receipt-capability")
    request_uuid = uid(branch, "receipt-request")
    actions: list[dict[str, object]] = [
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.text.match",
            "WFWorkflowActionParameters": {
                "UUID": match_uuid,
                "WFMatchTextCaseSensitive": False,
                "WFMatchTextPattern": r"(?<=--receipt=)[0-9a-f]{32}\.[0-9a-f]{64}",
                "text": extension_input(),
            },
        },
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.getitemfromlist",
            "WFWorkflowActionParameters": {
                "UUID": token_uuid,
                "WFInput": {
                    "Value": {
                        "OutputName": "Matches",
                        "OutputUUID": match_uuid,
                        "Type": "ActionOutput",
                    },
                    "WFSerializationType": "WFTextTokenAttachment",
                },
                # Use the final receipt token so an opaque value containing
                # ``--receipt=`` cannot hijack capability extraction.
                "WFItemSpecifier": "Last Item",
            },
        },
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.text.split",
            "WFWorkflowActionParameters": {
                "UUID": split_uuid,
                "WFTextCustomSeparator": ".",
                "WFTextSeparator": "Custom",
                "text": {
                    "Value": {
                        "OutputName": "Item from List",
                        "OutputUUID": token_uuid,
                        "Type": "ActionOutput",
                    },
                    "WFSerializationType": "WFTextTokenAttachment",
                },
            },
        },
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.getitemfromlist",
            "WFWorkflowActionParameters": {
                "UUID": request_uuid,
                "WFInput": {
                    "Value": {
                        "OutputName": "Split Text",
                        "OutputUUID": split_uuid,
                        "Type": "ActionOutput",
                    },
                    "WFSerializationType": "WFTextTokenAttachment",
                },
                # The first split component is the request id; the final is
                # the capability.  This split is machine-owned, not opaque
                # command text.
                "WFItemSpecifier": "First Item",
            },
        },
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.getitemfromlist",
            "WFWorkflowActionParameters": {
                "UUID": capability_uuid,
                "WFInput": {
                    "Value": {
                        "OutputName": "Split Text",
                        "OutputUUID": split_uuid,
                        "Type": "ActionOutput",
                    },
                    "WFSerializationType": "WFTextTokenAttachment",
                },
                "WFItemSpecifier": "Last Item",
            },
        },
    ]
    action_actions, action_uuid = receipt_action_parser(branch)
    return [*actions, *action_actions], (request_uuid, capability_uuid, action_uuid)


def receipt_token_parser(branch: str) -> tuple[list[dict[str, object]], tuple[str, str]]:
    """Return only request/capability extraction for data branches."""
    actions, outputs = receipt_parser(branch)
    return actions[:-2], outputs[:2]


def receipt_post(
    branch: str,
    request_uuid: str,
    capability_uuid: str,
    action_uuid: str,
) -> dict[str, object]:
    headers = dictionary_value(
        [
            field("X-Auth", static_text(TOKEN)),
            field("X-Protocol-Version", static_text("2")),
            field("X-Request-Id", attachment("Request ID", request_uuid)),
            field("X-Receipt-Capability", attachment("Receipt Capability", capability_uuid)),
        ]
    )
    body = dictionary_value(
        [
            field("protocol_version", static_text("2")),
            field("request_id", attachment("Request ID", request_uuid)),
            # Echo the canonical receipt action from the machine-owned command
            # trailer. The same Shortcut openurl branch serves many typed
            # actions (camera.open, messages.compose, url.open, ...).
            field("receipt_action", attachment("Receipt Action", action_uuid)),
            field("status", static_text("completed")),
        ]
    )
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "ShowHeaders": False,
            "UUID": uid(branch, "receipt-post"),
            "WFHTTPBodyType": "JSON",
            "WFHTTPHeaders": headers,
            "WFHTTPMethod": "POST",
            "WFJSONValues": body,
            "WFURL": f"{PUBLIC}/receipt",
        },
    }


def outer_close(actions: list[dict[str, object]], start: int) -> int:
    depth = 0
    for index in range(start, len(actions)):
        parameters = actions[index].get("WFWorkflowActionParameters", {})
        mode = parameters.get("WFControlFlowMode")
        if mode == 0:
            depth += 1
        elif mode == 2:
            depth -= 1
            if depth == 0:
                return index
    raise RuntimeError(f"could not find closing group for action {start}")


def add_branch_receipt(actions: list[dict[str, object]], command: str, action: str) -> None:
    for index, item in enumerate(actions):
        parameters = item.get("WFWorkflowActionParameters", {})
        if parameters.get("WFConditionalActionString") != command:
            continue
        parser, outputs = receipt_parser(command)
        close = outer_close(actions, index)
        actions[close:close] = [*parser, receipt_post(command, *outputs)]
        return
    raise RuntimeError(f"missing branch {command}")


def add_data_parser(actions: list[dict[str, object]], branch: str, url_suffix: str) -> None:
    target = f"{PUBLIC}{url_suffix}"
    for index, item in enumerate(actions):
        parameters = item.get("WFWorkflowActionParameters", {})
        if (
            item.get("WFWorkflowActionIdentifier") == "is.workflow.actions.downloadurl"
            and parameters.get("WFURL") == target
        ):
            parser, outputs = receipt_token_parser(branch)
            # The data branch already extracts the request id into its
            # "Updated Text" output. Only add the capability header here.
            capability_uuid = outputs[1]
            headers = parameters["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
            for header in headers:
                key = header["WFKey"]["Value"].get("string")
                if key == "X-Screenshot-Id":
                    header["WFKey"]["Value"]["string"] = "X-Request-Id"
            headers.append(field("X-Protocol-Version", static_text("2")))
            headers.append(field("X-Receipt-Capability", attachment("Receipt Capability", capability_uuid)))
            close = index
            actions[close:close] = parser
            return
    raise RuntimeError(f"missing data branch {url_suffix}")


def upgrade_existing_receipts(actions: list[dict[str, object]]) -> bool:
    """Upgrade the first version-2 template to echo dynamic receipt actions."""
    branches = (
        "hola timer start",
        "hola timer pause",
        "hola timer resume",
        "hola timer cancel",
        "hola flashlight on",
        "hola flashlight off",
        "hola call",
        "hola lowpower on",
        "hola lowpower off",
        "hola copytoclipboard",
        "hola controlcenter open",
        "hola controlcenter close",
        "hola openurl",
        "hola homescreen",
        "hola alarm set",
        "hola alarm off",
    )
    changed = False
    for branch in branches:
        # Locate by the branch's existing static action body instead of the
        # receipt status field; duplicate actions (flashlight/control-center)
        # are handled by upgrading the first still-static body each time.
        expected_action = {
            "hola timer start": "timer.start",
            "hola timer pause": "timer.pause",
            "hola timer resume": "timer.resume",
            "hola timer cancel": "timer.cancel",
            "hola flashlight on": "flashlight.set",
            "hola flashlight off": "flashlight.set",
            "hola call": "call.start",
            "hola lowpower on": "low_power.set",
            "hola lowpower off": "low_power.set",
            "hola copytoclipboard": "clipboard.copy",
            "hola controlcenter open": "control_center.set",
            "hola controlcenter close": "control_center.set",
            "hola openurl": "url.open",
            "hola homescreen": "home.open",
            "hola alarm set": "alarm.set",
            "hola alarm off": "alarm.disable_at",
        }[branch]
        receipt_index = next(
            (
                index
                for index, item in enumerate(actions)
                if item.get("WFWorkflowActionParameters", {}).get("WFURL")
                == f"{PUBLIC}/receipt"
                and any(
                    field_item.get("WFKey", {}).get("Value", {}).get("string") == "action"
                    and field_item.get("WFValue", {}).get("Value", {}).get("string")
                    == expected_action
                    for field_item in item.get("WFWorkflowActionParameters", {})
                    .get("WFJSONValues", {})
                    .get("Value", {})
                    .get("WFDictionaryFieldValueItems", [])
                )
            ),
            None,
        )
        if receipt_index is None:
            raise RuntimeError(f"missing static receipt body for {branch}")
        parser, action_uuid = receipt_action_parser(branch)
        actions[receipt_index:receipt_index] = parser
        receipt_index += len(parser)
        body_items = actions[receipt_index].get("WFWorkflowActionParameters", {}).get(
            "WFJSONValues", {}
        ).get("Value", {}).get("WFDictionaryFieldValueItems", [])
        action_field = next(
            field_item
            for field_item in body_items
            if field_item.get("WFKey", {}).get("Value", {}).get("string") == "action"
        )
        action_field["WFValue"] = attachment("Receipt Action", action_uuid)
        changed = True
    return changed


def migrate_existing_receipt_contract(actions: list[dict[str, object]]) -> bool:
    """Upgrade an already-augmented template without touching native actions.

    Older v2 exports called the canonical field ``action`` and selected the
    first marker match.  The current contract names the field
    ``receipt_action`` and selects the final machine-owned trailer, allowing
    opaque command text to contain marker-like substrings safely.
    """

    changed = False
    marker_match_uuids = {
        item.get("WFWorkflowActionParameters", {}).get("UUID")
        for item in actions
        if item.get("WFWorkflowActionIdentifier") == "is.workflow.actions.text.match"
        and item.get("WFWorkflowActionParameters", {}).get("WFMatchTextPattern")
        in {
            r"(?<=--receipt=)[0-9a-f]{32}\.[0-9a-f]{64}",
            r"(?<=--action=)[a-z0-9._-]+",
        }
    }
    for item in actions:
        parameters = item.get("WFWorkflowActionParameters", {})
        if parameters.get("WFURL") == f"{PUBLIC}/receipt":
            body_items = parameters.get("WFJSONValues", {}).get("Value", {}).get(
                "WFDictionaryFieldValueItems", []
            )
            for body_item in body_items:
                key = body_item.get("WFKey", {}).get("Value", {}).get("string")
                if key == "action":
                    body_item["WFKey"]["Value"]["string"] = "receipt_action"
                    changed = True
        if (
            item.get("WFWorkflowActionIdentifier")
            == "is.workflow.actions.getitemfromlist"
            and item.get("WFWorkflowActionParameters", {}).get("WFItemSpecifier")
            == "First Item"
        ):
            # This output UUID is only selected by one of the receipt marker
            # parsers.  Native Shortcut list operations use different input
            # names and are intentionally left untouched.
            input_value = item.get("WFWorkflowActionParameters", {}).get("WFInput", {})
            value = input_value.get("Value", {}) if isinstance(input_value, dict) else {}
            output_name = value.get("OutputName") if isinstance(value, dict) else None
            output_uuid = value.get("OutputUUID") if isinstance(value, dict) else None
            if output_name == "Matches" and output_uuid in marker_match_uuids:
                changed = True
                item["WFWorkflowActionParameters"]["WFItemSpecifier"] = "Last Item"
    return changed


def main() -> int:
    with TEMPLATE.open("rb") as source:
        actions = plistlib.load(source)
    if not isinstance(actions, list):
        raise SystemExit("template must contain an action array")
    receipt_count = sum(
        item.get("WFWorkflowActionParameters", {}).get("WFURL") == f"{PUBLIC}/receipt"
        for item in actions
    )
    if len(actions) in {211, 243, 244} and receipt_count == 16:
        changed = False
        if len(actions) == 211 and upgrade_existing_receipts(actions):
            changed = True
        if migrate_existing_receipt_contract(actions):
            changed = True
        if harden_command_input(actions):
            changed = True
        for item in actions:
            parameters = item.get("WFWorkflowActionParameters", {})
            if parameters.get("WFURL") != f"{PUBLIC}/receipt":
                continue
            headers = parameters.get("WFHTTPHeaders", {}).get("Value", {}).get(
                "WFDictionaryFieldValueItems", []
            )
            request_uuid = next(
                (
                    header.get("WFValue", {})
                    .get("Value", {})
                    .get("attachmentsByRange", {})
                    .get("{0, 1}", {})
                    .get("OutputUUID")
                    for header in headers
                    if header.get("WFKey", {}).get("Value", {}).get("string") == "X-Request-Id"
                ),
                None,
            )
            body_items = parameters.get("WFJSONValues", {}).get("Value", {}).get(
                "WFDictionaryFieldValueItems", []
            )
            if request_uuid and not any(
                field_item.get("WFKey", {}).get("Value", {}).get("string") == "request_id"
                for field_item in body_items
            ):
                body_items.insert(1, field("request_id", attachment("Request ID", request_uuid)))
                changed = True
        if changed:
            with TEMPLATE.open("wb") as output:
                plistlib.dump(actions, output, fmt=plistlib.FMT_XML, sort_keys=False)
            print("updated existing version-2 receipt branches")
        else:
            print("Shortcut template already contains the version-2 receipt branches")
        return 0
    if len(actions) != 95 or receipt_count:
        raise SystemExit(
            "expected the unmodified 95-action template before receipt augmentation"
        )

    # Strip protocol trailers from arguments before native actions consume
    # them. The trailer itself remains available to the receipt parsers.
    suffix = r"\s+--v=2\s+--request-id=[0-9a-f]{32}\s+--receipt=[0-9a-f]{32}\.[0-9a-f]{64}\s+--action=[a-z0-9._-]+\s*$"
    replacements = {
        "hola call": rf"(?i)(?:^\s*hola\s*(?:call\s+)?|{suffix})",
        "hola copytoclipboard": rf"(?i)(?:^\s*hola\s*(?:copytoclipboard\s+)?|{suffix})",
        "hola openurl": rf"(?i)(?:^\s*hola\s*(?:openurl\s+)?|{suffix})",
        "hola getclipboard": rf"(?i)(?:^\s*hola\s*(?:getclipboard\s+)?|{suffix})",
        "hola screentext": rf"(?i)(?:^\s*hola\s*(?:screentext\s+)?|{suffix})",
        "hola screenshot": rf"(?i)(?:^\s*hola\s*(?:screenshot\s+)?|{suffix})",
        "hola alarm get": rf"(?i)(?:^\s*hola\s+alarm\s+get\s+|{suffix})",
        "hola alarm set": rf"(?i)(?:^\s*hola\s+alarm\s+set\s+(?:[01]\d|2[0-3]):[0-5]\d\s*|{suffix})",
    }
    marker_replacements = (
        ("call\\s+", replacements["hola call"]),
        ("copytoclipboard\\s+", replacements["hola copytoclipboard"]),
        ("getclipboard\\s+", replacements["hola getclipboard"]),
        ("openurl\\s+", replacements["hola openurl"]),
        ("screentext\\s+", replacements["hola screentext"]),
        ("screenshot\\s+", replacements["hola screenshot"]),
        ("alarm\\s+get\\s+", replacements["hola alarm get"]),
        ("alarm\\s+set\\s+", replacements["hola alarm set"]),
    )
    for item in actions:
        parameters = item.get("WFWorkflowActionParameters", {})
        value = parameters.get("WFReplaceTextFind")
        if not isinstance(value, str):
            continue
        for marker, replacement in marker_replacements:
            if marker in value:
                parameters["WFReplaceTextFind"] = replacement
                break

    for command, action in (
        ("hola timer start", "timer.start"),
        ("hola timer pause", "timer.pause"),
        ("hola timer resume", "timer.resume"),
        ("hola timer cancel", "timer.cancel"),
        ("hola flashlight on", "flashlight.set"),
        ("hola flashlight off", "flashlight.set"),
        ("hola call", "call.start"),
        ("hola lowpower on", "low_power.set"),
        ("hola lowpower off", "low_power.set"),
        ("hola copytoclipboard", "clipboard.copy"),
        ("hola controlcenter open", "control_center.set"),
        ("hola controlcenter close", "control_center.set"),
        ("hola openurl", "url.open"),
        ("hola homescreen", "home.open"),
        ("hola alarm set", "alarm.set"),
        ("hola alarm off", "alarm.disable_at"),
    ):
        add_branch_receipt(actions, command, action)

    for branch, suffix_path in (
        ("hola getclipboard", "/clipboard"),
        ("hola screentext", "/text"),
        ("hola screenshot", "/photo"),
        ("hola alarm get", "/get-alarm"),
    ):
        add_data_parser(actions, branch, suffix_path)

    harden_command_input(actions)

    with TEMPLATE.open("wb") as output:
        plistlib.dump(actions, output, fmt=plistlib.FMT_XML, sort_keys=False)
    print(f"wrote {len(actions)} actions to {TEMPLATE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
