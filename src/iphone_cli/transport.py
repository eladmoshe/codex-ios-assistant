"""Private transport adapter for the existing iMessage → Shortcuts bridge."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from urllib.error import URLError
from urllib.request import urlopen
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .errors import IPhoneError
from .config import (
    DATA_DIR,
    CONFIG_FILE,
    file_values,
    private_config_ready,
    receiver_url,
    registration_socket,
    sender_socket,
)
from .protocol import PROTOCOL_VERSION, REQUEST_ID_PATTERN, validate_request_id


OperationKind = Literal[
    "hola",
    "screen-read",
    "screen-capture",
    "clipboard-read",
    "alarm-read",
]
BRIDGE_MODULE = "iphone_cli.bridge"


@dataclass(frozen=True)
class Operation:
    resource: str
    action: str
    kind: OperationKind
    arguments: tuple[str, ...] = ()
    summary: str = ""
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Canonical contract consumed by the Nami executor. Most actions use
    # ``resource.action``; state setters and UI launchers intentionally share
    # one typed action while their state remains in argv/metadata.
    receipt_action: str | None = None


@dataclass(frozen=True)
class Result:
    resource: str
    action: str
    status: str
    summary: str
    data: dict[str, Any]
    receipt_action: str | None = None


def bridge_command(action: str) -> list[str]:
    return [sys.executable, "-m", BRIDGE_MODULE, action]


def command_from_environment(name: str, default: str | list[str]) -> list[str]:
    configured = os.environ.get(name)
    if configured:
        return shlex.split(configured)
    return list(default) if isinstance(default, list) else shlex.split(default)


def command_for(operation: Operation, *, request_id: str | None = None) -> list[str]:
    if request_id is not None:
        validate_request_id(request_id)
    if operation.kind == "hola":
        command = [
            *command_from_environment("IPHONE_IMSG_COMMAND", bridge_command("send")),
            "hola",
            *operation.arguments,
        ]
        # The wire command alone cannot distinguish typed actions that all
        # use the Shortcut's openurl branch (camera.open, messages.compose,
        # url.open, and so on). Pass the policy-owned canonical receipt name
        # as a separate bridge option; it is never part of the phone command
        # consumed by the Shortcut.
        command.extend(
            [
                "--receipt-action",
                operation.receipt_action or operation.resource + "." + operation.action,
            ]
        )
        if request_id is not None:
            command.extend(["--request-id", request_id])
        return command
    if operation.kind == "screen-read":
        command = command_from_environment(
            "IPHONE_READ_SCREEN_COMMAND", bridge_command("read-screen")
        )
        return [*command, "--request-id", request_id] if request_id is not None else command
    if operation.kind == "screen-capture":
        command = command_from_environment(
            "IPHONE_SCREENSHOT_COMMAND", bridge_command("screenshot")
        )
        return [*command, "--request-id", request_id] if request_id is not None else command
    if operation.kind == "clipboard-read":
        command = command_from_environment(
            "IPHONE_CLIPBOARD_COMMAND", bridge_command("clipboard")
        )
        return [*command, "--request-id", request_id] if request_id is not None else command
    if operation.kind == "alarm-read":
        command = command_from_environment("IPHONE_ALARM_COMMAND", bridge_command("alarms"))
        return [*command, "--request-id", request_id] if request_id is not None else command
    raise IPhoneError(f"Unsupported operation kind: {operation.kind}")


def preview(operation: Operation, *, request_id: str | None = None) -> str:
    return shlex.join(command_for(operation, request_id=request_id))


def _run(command: list[str], *, timeout: float, environment: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except FileNotFoundError as error:
        raise IPhoneError(f"Required helper is not installed: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise IPhoneError(f"Timed out after {timeout:g}s while running {command[0]}.") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise IPhoneError(f"{command[0]} failed: {detail}")
    return completed.stdout


def execute(
    operation: Operation,
    *,
    dry_run: bool = False,
    timeout: float = 30,
    output: str | None = None,
    request_id: str | None = None,
) -> Result:
    if request_id is not None:
        validate_request_id(request_id)
    command = command_for(operation, request_id=request_id)
    common = {
        "kind": operation.kind,
        "command": command,
        **operation.metadata,
    }
    if operation.url:
        common["url"] = operation.url

    if dry_run:
        return Result(
            resource=operation.resource,
            action=operation.action,
            status="dry-run",
            summary=preview(operation, request_id=request_id),
            data=common,
            receipt_action=operation.receipt_action or operation.resource + "." + operation.action,
        )

    environment = os.environ.copy()
    environment["READ_SCREEN_TIMEOUT"] = str(max(1, int(timeout)))
    environment["SCREENSHOT_TIMEOUT"] = str(max(1, int(timeout)))
    environment["CLIPBOARD_TIMEOUT"] = str(max(1, int(timeout)))
    environment["ALARM_TIMEOUT"] = str(max(1, int(timeout)))
    stdout = _run(command, timeout=timeout + 5, environment=environment)

    if operation.kind == "hola":
        try:
            receipt = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise IPhoneError(
                "The phone action helper did not return a versioned execution receipt."
            ) from error
        reported_action = receipt.get("receipt_action") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("protocol_version") != PROTOCOL_VERSION
            or receipt.get("status") not in {"completed", "failed", "timeout"}
            or not isinstance(receipt.get("request_id"), str)
            or REQUEST_ID_PATTERN.fullmatch(receipt["request_id"]) is None
            or reported_action != (operation.receipt_action or operation.resource + "." + operation.action)
            or (request_id is not None and receipt.get("request_id") != request_id)
        ):
            raise IPhoneError("The phone action helper returned an invalid execution receipt.")
        status = str(receipt["status"])
        error_code = receipt.get("error_code")
        summary = operation.summary
        if status != "completed":
            summary = f"The phone did not complete {operation.action} ({error_code or status})."
        return Result(
            resource=operation.resource,
            action=operation.action,
            status=status,
            summary=summary,
            data={
                **common,
                "protocol_version": receipt["protocol_version"],
                "request_id": receipt["request_id"],
                "receipt_action": reported_action,
                "error_code": error_code,
            },
            receipt_action=str(reported_action),
        )

    if operation.kind == "screen-read":
        receipt = _parse_data_receipt(stdout, operation, request_id=request_id)
        if receipt["status"] != "completed":
            return _data_failure_result(operation, common, receipt)
        text = receipt.get("data", {}).get("text") if isinstance(receipt.get("data"), dict) else None
        if not isinstance(text, str):
            raise IPhoneError("The screen receipt did not contain text data.")
        return Result(
            resource=operation.resource,
            action=operation.action,
            status=str(receipt["status"]),
            summary=text,
            data={
                **common,
                "protocol_version": receipt["protocol_version"],
                "request_id": receipt["request_id"],
                "text": text,
            },
            receipt_action=str(receipt["receipt_action"]),
        )

    if operation.kind == "clipboard-read":
        receipt = _parse_data_receipt(stdout, operation, request_id=request_id)
        if receipt["status"] != "completed":
            return _data_failure_result(operation, common, receipt)
        text = receipt.get("data", {}).get("text") if isinstance(receipt.get("data"), dict) else None
        if not isinstance(text, str):
            raise IPhoneError("The clipboard receipt did not contain text data.")
        return Result(
            resource=operation.resource,
            action=operation.action,
            status=str(receipt["status"]),
            summary=text,
            data={
                **common,
                "protocol_version": receipt["protocol_version"],
                "request_id": receipt["request_id"],
                "text": text,
            },
            receipt_action=str(receipt["receipt_action"]),
        )

    if operation.kind == "alarm-read":
        receipt = _parse_data_receipt(stdout, operation, request_id=request_id)
        if receipt["status"] != "completed":
            return _data_failure_result(operation, common, receipt)
        payload = receipt.get("data")
        alarms = payload.get("alarms") if isinstance(payload, dict) else None
        if not isinstance(alarms, list) or not all(isinstance(alarm, dict) for alarm in alarms):
            raise IPhoneError("The alarm receipt did not contain an alarms array.")
        if alarms:
            lines = []
            for alarm in alarms:
                time_value = str(alarm.get("time") or "Unknown time")
                label = str(alarm.get("label") or "Alarm")
                repeat_days = str(alarm.get("repeat_days") or "").strip()
                line = f"{time_value} — {label}"
                if repeat_days and repeat_days.lower() not in {"never", "none"}:
                    line += f" ({repeat_days})"
                lines.append(line)
            summary = "\n".join(lines)
        else:
            summary = "No active alarms."
        return Result(
            resource=operation.resource,
            action=operation.action,
            status=str(receipt["status"]),
            summary=summary,
            data={
                **common,
                "protocol_version": receipt["protocol_version"],
                "request_id": receipt["request_id"],
                "alarms": alarms,
            },
            receipt_action=str(receipt["receipt_action"]),
        )

    receipt = _parse_data_receipt(stdout, operation, request_id=request_id)
    if receipt["status"] != "completed":
        return _data_failure_result(operation, common, receipt)
    receipt_data = receipt.get("data")
    path_text = receipt_data.get("path") if isinstance(receipt_data, dict) else None
    if not isinstance(path_text, str):
        raise IPhoneError("The screenshot receipt did not contain a path.")
    source = Path(path_text).expanduser()
    inbox = (DATA_DIR / "inbox").resolve()
    source_path = source.absolute()
    try:
        information = source_path.lstat()
        final_path = source_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise IPhoneError(f"Screenshot helper returned a missing file: {source}") from error
    if (
        stat.S_ISLNK(information.st_mode)
        or not stat.S_ISREG(information.st_mode)
        or not final_path.is_relative_to(inbox)
        or information.st_size > 12_000_000
    ):
        raise IPhoneError("Screenshot helper returned an invalid or oversized image.")
    with final_path.open("rb") as image_file:
        signature = image_file.read(4)
    if signature != b"\x89PNG" and signature[:2] != b"\xff\xd8":
        raise IPhoneError("Screenshot helper returned a non-PNG/JPEG image.")
    if output:
        destination = Path(output).expanduser()
        if destination.is_dir():
            destination /= source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        final_path = destination.resolve()
    return Result(
        resource=operation.resource,
        action=operation.action,
        status=str(receipt["status"]),
        summary=str(final_path),
        data={
            **common,
            "protocol_version": receipt["protocol_version"],
            "request_id": receipt["request_id"],
            "path": str(final_path),
        },
        receipt_action=str(receipt["receipt_action"]),
    )


def _parse_data_receipt(
    stdout: str,
    operation: Operation,
    *,
    request_id: str | None = None,
) -> dict[str, object]:
    try:
        receipt = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise IPhoneError("The phone data helper did not return a versioned receipt.") from error
    expected = operation.receipt_action or operation.resource + "." + operation.action
    reported_action = receipt.get("receipt_action") if isinstance(receipt, dict) else None
    if (
        not isinstance(receipt, dict)
        or receipt.get("protocol_version") != PROTOCOL_VERSION
        or receipt.get("status") not in {"completed", "failed", "timeout"}
        or not isinstance(receipt.get("request_id"), str)
        or REQUEST_ID_PATTERN.fullmatch(receipt["request_id"]) is None
        or reported_action != expected
        or (request_id is not None and receipt.get("request_id") != request_id)
        or not isinstance(receipt.get("data"), dict)
    ):
        raise IPhoneError("The phone data helper returned an invalid execution receipt.")
    return receipt


def _data_failure_result(
    operation: Operation,
    common: dict[str, object],
    receipt: dict[str, object],
) -> Result:
    status = str(receipt["status"])
    error_code = receipt.get("error_code") or status
    return Result(
        resource=operation.resource,
        action=operation.action,
        status=status,
        summary=f"The phone did not complete {operation.action} ({error_code}).",
        data={
            **common,
            "protocol_version": receipt["protocol_version"],
            "request_id": receipt["request_id"],
            "error_code": error_code,
        },
        receipt_action=str(receipt["receipt_action"]),
    )


def dependency_report() -> list[dict[str, object]]:
    report: list[dict[str, object]] = []
    configuration_file_ready = private_config_ready(CONFIG_FILE)
    # Do not read a token-bearing file until its ownership, type, mode, and
    # parent directory have passed the private-file check. This also keeps
    # doctor usable when an unreadable root-owned file was left behind.
    configured = file_values() if configuration_file_ready else {}
    required_names = (
        "IPHONE_MSG_TARGET",
        "IPHONE_PUBLIC_URL",
        "IPHONE_RECEIVER_TOKEN",
    )
    configuration_ready = configuration_file_ready and all(
        os.environ.get(name, configured.get(name, "")).strip() for name in required_names
    )
    report.append(
        {
            "name": "Private config",
            "required": True,
            "available": configuration_ready,
            "command": str(CONFIG_FILE),
            "path": str(CONFIG_FILE) if CONFIG_FILE.is_file() else None,
        }
    )

    socket_path = sender_socket()
    report.append(
        {
            "name": "Messages sender",
            "required": True,
            "available": socket_path.exists() and socket_path.is_socket(),
            "command": str(socket_path),
            "path": str(socket_path) if socket_path.exists() else None,
        }
    )

    receipt_socket = registration_socket()
    report.append(
        {
            "name": "Receipt registration",
            "required": True,
            "available": receipt_socket.exists() and receipt_socket.is_socket(),
            "command": str(receipt_socket),
            "path": str(receipt_socket) if receipt_socket.exists() else None,
        }
    )

    receiver_available = False
    try:
        with urlopen(receiver_url() + "/health", timeout=1) as response:
            receiver_available = (
                response.status == 200
                and b"codex-ios-assistant receiver up" in response.read(256)
            )
    except (OSError, URLError):
        pass
    report.append(
        {
            "name": "Local receiver",
            "required": True,
            "available": receiver_available,
            "command": receiver_url(),
            "path": receiver_url() if receiver_available else None,
        }
    )

    specifications = [
        ("Contacts lookup", "IPHONE_CONTACTS_COMMAND", "contacts", False),
        ("Messages history", "IPHONE_HISTORY_IMSG_COMMAND", "imsg", False),
    ]
    for label, variable, default, required in specifications:
        command = command_from_environment(variable, default)
        executable = command[0]
        if "/" in executable:
            resolved = str(Path(executable).expanduser().resolve()) if Path(executable).expanduser().exists() else None
        else:
            resolved = shutil.which(executable)
        report.append(
            {
                "name": label,
                "required": required,
                "available": resolved is not None,
                "command": executable,
                "path": resolved,
            }
        )
    return report
