"""Versioned, correlated command and receipt protocol helpers.

The iMessage payload is intentionally still a single line because the phone
automation receives plain text.  The fields after the human-readable command
are machine-owned and are never taken from the model's arguments.
"""

from __future__ import annotations

import re
import secrets

from .errors import IPhoneError


PROTOCOL_VERSION = 2
REQUEST_ID_BYTES = 16
RECEIPT_CAPABILITY_BYTES = 32
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
RECEIPT_CAPABILITY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ACTION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
RECEIPT_TOKEN_PATTERN = re.compile(
    r"--receipt=([0-9a-f]{32})\.([0-9a-f]{64})(?:\s|$)"
)


def new_request_id() -> str:
    return secrets.token_hex(REQUEST_ID_BYTES)


def new_receipt_capability() -> str:
    return secrets.token_hex(RECEIPT_CAPABILITY_BYTES)


def validate_request_id(value: str) -> str:
    if not REQUEST_ID_PATTERN.fullmatch(value):
        raise IPhoneError("request id must be exactly 32 lowercase hexadecimal characters")
    return value


def validate_receipt_capability(value: str) -> str:
    if not RECEIPT_CAPABILITY_PATTERN.fullmatch(value):
        raise IPhoneError(
            "receipt capability must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def validate_action(value: str) -> str:
    if not ACTION_PATTERN.fullmatch(value):
        raise IPhoneError("action must be a lowercase dotted identifier")
    return value


def receipt_token(request_id: str, capability: str) -> str:
    validate_request_id(request_id)
    validate_receipt_capability(capability)
    return f"{request_id}.{capability}"


def protocol_command(
    command: str,
    *,
    action: str,
    request_id: str,
    capability: str,
) -> str:
    """Append protocol metadata to a validated one-line hola command."""

    if not command.startswith("hola ") or "\n" in command or "\r" in command:
        raise IPhoneError("The sender accepts one single-line 'hola' command.")
    validate_action(action)
    validate_request_id(request_id)
    validate_receipt_capability(capability)
    if any(marker in command for marker in ("--v=", "--request-id=", "--receipt=", "--action=")):
        raise IPhoneError("command arguments contain reserved protocol metadata")
    return (
        f"{command} --v={PROTOCOL_VERSION} --request-id={request_id} "
        f"--receipt={receipt_token(request_id, capability)} --action={action}"
    )


def parse_receipt_token(command: str) -> tuple[str, str]:
    match = RECEIPT_TOKEN_PATTERN.search(command)
    if not match:
        raise IPhoneError("command does not contain a valid receipt token")
    return validate_request_id(match.group(1)), validate_receipt_capability(match.group(2))

