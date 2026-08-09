"""Local bridge commands used by the public ``iphone`` CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import (
    DATA_DIR,
    message_target,
    receiver_token,
    receiver_url,
    registration_socket,
    sender_socket,
)
from .errors import IPhoneError
from .protocol import (
    PROTOCOL_VERSION,
    new_receipt_capability,
    new_request_id,
    protocol_command,
    validate_action,
    validate_request_id,
)


MAX_COMMAND_BYTES = 4096
MAX_REPLY_BYTES = 16_384
MAX_REGISTRATION_REPLY_BYTES = 16_384


def _read_line(connection: socket.socket, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        chunk = connection.recv(min(4096, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if b"\n" in chunk:
            break
    data = b"".join(chunks)
    if len(data) > limit:
        raise IPhoneError("Bridge message exceeded its size limit.")
    return data.split(b"\n", 1)[0]


def send_command(command: str) -> None:
    """Ask the per-user sender agent to deliver one private command."""
    if not command.startswith("hola ") or "\n" in command or "\r" in command:
        raise IPhoneError("The sender accepts one single-line 'hola' command.")
    payload = json.dumps({"command": command}, separators=(",", ":")).encode() + b"\n"
    path = sender_socket()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(8)
            connection.connect(str(path))
            connection.sendall(payload)
            reply_bytes = _read_line(connection, MAX_REPLY_BYTES)
    except (FileNotFoundError, ConnectionRefusedError) as error:
        raise IPhoneError(
            "The Messages sender service is not running. Run scripts/install-services."
        ) from error
    except OSError as error:
        raise IPhoneError(f"Could not reach the Messages sender service: {error}") from error
    try:
        reply = json.loads(reply_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise IPhoneError("The Messages sender service returned an invalid response.") from error
    if not isinstance(reply, dict) or not reply.get("ok"):
        detail = reply.get("error") if isinstance(reply, dict) else None
        raise IPhoneError(str(detail or "The Messages sender service rejected the command."))


def _registration_request(payload: dict[str, object]) -> dict[str, object]:
    """Call the receiver's private registration/poll socket."""
    encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > MAX_REGISTRATION_REPLY_BYTES:
        raise IPhoneError("registration request exceeded its size limit")
    path = registration_socket()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(8)
            connection.connect(str(path))
            connection.sendall(encoded)
            reply_bytes = _read_line(connection, MAX_REGISTRATION_REPLY_BYTES)
    except (FileNotFoundError, ConnectionRefusedError) as error:
        raise IPhoneError(
            "The hardened receiver service is not running. Run scripts/install-services."
        ) from error
    except OSError as error:
        raise IPhoneError(f"Could not reach the receiver registration service: {error}") from error
    try:
        reply = json.loads(reply_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise IPhoneError("The receiver registration service returned an invalid response.") from error
    if not isinstance(reply, dict) or not reply.get("ok"):
        detail = reply.get("error") if isinstance(reply, dict) else None
        raise IPhoneError(str(detail or "The receiver rejected the registration request."))
    if reply.get("protocol_version") != PROTOCOL_VERSION:
        raise IPhoneError("The receiver returned an unsupported protocol version.")
    return reply


def _register(request_id: str, capability: str, action: str, timeout: float) -> None:
    validate_request_id(request_id)
    validate_action(action)
    _registration_request(
        {
            "op": "register",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "capability": capability,
            "action": action,
            "expires_at": time.time() + min(max(timeout + 10, 30), 180),
        }
    )


def _poll_registered(request_id: str, timeout: float, expected_action: str) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _registration_request(
            {
                "op": "poll",
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
            }
        )
        state = response.get("state")
        if state == "complete":
            return response
        if state == "unknown":
            # A receiver restart discards in-memory capabilities. Surface a
            # typed failure to the caller; never infer completion from the
            # sender's earlier delivery acknowledgment.
            return {
                "ok": True,
                "protocol_version": PROTOCOL_VERSION,
                "state": "complete",
                "request_id": request_id,
                "action": expected_action,
                "status": "failed",
                "data": {},
                "error_code": "receipt_unknown",
            }
        time.sleep(0.5)
    try:
        _registration_request(
            {
                "op": "cancel",
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
            }
        )
    except IPhoneError:
        # The timeout remains the truthful result even if cancellation races a
        # receiver restart.
        return {
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
            "state": "complete",
            "request_id": request_id,
            "action": expected_action,
            "status": "timeout",
            "data": {},
            "error_code": "receipt_timeout",
        }
    try:
        response = _registration_request(
            {
                "op": "poll",
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
            }
        )
        if response.get("state") == "complete":
            return response
    except IPhoneError:
        pass
    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "state": "complete",
        "request_id": request_id,
        "action": expected_action,
        "status": "timeout",
        "data": {},
        "error_code": "receipt_timeout",
    }


def _one_way_action(command: str) -> str:
    words = command.split()
    if len(words) < 2 or words[0] != "hola":
        raise IPhoneError("The sender accepts a hola command for phone actions.")
    key = tuple(words[1:3])
    mapping = {
        ("timer", "start"): "timer.start",
        ("timer", "pause"): "timer.pause",
        ("timer", "resume"): "timer.resume",
        ("timer", "cancel"): "timer.cancel",
        ("flashlight", "on"): "flashlight.set",
        ("flashlight", "off"): "flashlight.set",
        ("call",): "call.start",
        ("lowpower", "on"): "low_power.set",
        ("lowpower", "off"): "low_power.set",
        ("copytoclipboard",): "clipboard.copy",
        ("getclipboard",): "clipboard.get",
        ("controlcenter", "open"): "control_center.set",
        ("controlcenter", "close"): "control_center.set",
        ("openurl",): "url.open",
        ("screentext",): "screen.read",
        ("screenshot",): "screen.capture",
        ("homescreen",): "home.open",
        ("alarm", "get"): "alarm.list",
        ("alarm", "set"): "alarm.set",
        ("alarm", "off"): "alarm.disable_at",
    }
    if (words[1],) in mapping:
        return mapping[(words[1],)]
    if key in mapping:
        return mapping[key]
    raise IPhoneError("Unsupported phone action command.")


def execute_one_way(
    command: str,
    *,
    timeout: float = 30,
    expected_action: str | None = None,
) -> dict[str, object]:
    """Send a finite Shortcut command and wait for its correlated receipt."""
    action = expected_action or _one_way_action(command)
    if expected_action is not None:
        validate_action(expected_action)
    request_id = new_request_id()
    capability = new_receipt_capability()
    _register(request_id, capability, action, timeout)
    wire_command = protocol_command(
        command,
        action=action,
        request_id=request_id,
        capability=capability,
    )
    try:
        send_command(wire_command)
    except IPhoneError:
        try:
            _registration_request(
                {
                    "op": "cancel",
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                }
            )
        except IPhoneError:
            pass
        raise
    result = _poll_registered(request_id, timeout, action)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "action": result.get("action", action),
        "status": result.get("status", "failed"),
        "data": result.get("data", {}),
        "error_code": result.get("error_code"),
    }


def _send_imessage(command: str) -> None:
    target = message_target()
    script = [
        "/usr/bin/osascript",
        "-e",
        "on run argv",
        "-e",
        'tell application id "com.apple.MobileSMS"',
        "-e",
        "set acct to 1st account whose service type is iMessage and enabled is true",
        "-e",
        "send (item 1 of argv) to participant (item 2 of argv) of acct",
        "-e",
        "end tell",
        "-e",
        "end run",
        command,
        target,
    ]
    completed = subprocess.run(script, capture_output=True, text=True, timeout=15, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise IPhoneError(f"Messages automation failed: {detail}")


def _peer_is_current_user(connection: socket.socket) -> bool:
    getpeereid = getattr(connection, "getpeereid", None)
    if getpeereid is None:
        return True
    peer_uid, _ = getpeereid()
    return peer_uid == os.getuid()


def _handle_sender_connection(connection: socket.socket) -> None:
    try:
        if not _peer_is_current_user(connection):
            raise IPhoneError("Sender connection came from another user.")
        request = json.loads(_read_line(connection, MAX_COMMAND_BYTES))
        command = request.get("command") if isinstance(request, dict) else None
        if (
            not isinstance(command, str)
            or not command.startswith("hola ")
            or "\n" in command
            or "\r" in command
        ):
            raise IPhoneError("The sender accepts one single-line 'hola' command.")
        _send_imessage(command)
        response = {"ok": True}
    except (IPhoneError, json.JSONDecodeError, UnicodeDecodeError) as error:
        response = {"ok": False, "error": str(error)}
    connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")


def run_sender() -> None:
    """Run the unsandboxed per-user Messages automation service."""
    path = sender_socket()
    if path.exists():
        mode = path.stat().st_mode
        if not stat.S_ISSOCK(mode):
            raise IPhoneError(f"Refusing to replace non-socket path: {path}")
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(8)
        print(f"sender listening on {path}", flush=True)
        while True:
            connection, _ = server.accept()
            with connection:
                _handle_sender_connection(connection)
    finally:
        server.close()
        try:
            if path.exists() and stat.S_ISSOCK(path.stat().st_mode):
                path.unlink()
        except OSError:
            pass


def _correlation_id() -> str:
    return f"{secrets.randbelow(90_000) + 10_000}"


def _receiver_request(method: str, path: str) -> bytes | None:
    request = Request(
        receiver_url() + path,
        method=method,
        headers={"X-Auth": receiver_token()},
    )
    try:
        with urlopen(request, timeout=2) as response:
            return response.read()
    except HTTPError as error:
        if error.code == 404:
            return None
        raise IPhoneError(f"Receiver returned HTTP {error.code} for {path}.") from error
    except URLError as error:
        raise IPhoneError(f"Could not reach the local receiver: {error.reason}") from error


def _poll(path: str, timeout: int) -> bytes:
    deadline = time.monotonic() + timeout
    last_error: IPhoneError | None = None
    while time.monotonic() < deadline:
        try:
            value = _receiver_request("GET", path)
        except IPhoneError as error:
            last_error = error
        else:
            if value is not None:
                return value
        time.sleep(0.5)
    if last_error:
        raise last_error
    raise IPhoneError(f"Timed out after {timeout}s waiting for the iPhone response.")


def _timeout(environment_name: str, default: int) -> int:
    raw = os.environ.get(environment_name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise IPhoneError(f"{environment_name} must be an integer.") from error
    return max(1, value)


def _execute_data_request(
    *,
    action: str,
    command: str,
    timeout: int,
) -> dict[str, object]:
    request_id = new_request_id()
    capability = new_receipt_capability()
    _register(request_id, capability, action, timeout)
    wire_command = protocol_command(
        f"{command} {request_id}",
        action=action,
        request_id=request_id,
        capability=capability,
    )
    try:
        send_command(wire_command)
    except IPhoneError:
        try:
            _registration_request(
                {
                    "op": "cancel",
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                }
            )
        except IPhoneError:
            pass
        raise
    return _poll_registered(request_id, timeout, action)


def read_screen() -> None:
    result = _execute_data_request(
        action="screen.read",
        command="hola screentext",
        timeout=_timeout("READ_SCREEN_TIMEOUT", 30),
    )
    _write_data_receipt(result)


def read_clipboard() -> None:
    result = _execute_data_request(
        action="clipboard.get",
        command="hola getclipboard",
        timeout=_timeout("CLIPBOARD_TIMEOUT", 30),
    )
    _write_data_receipt(result)


def read_alarms() -> None:
    result = _execute_data_request(
        action="alarm.list",
        command="hola alarm get",
        timeout=_timeout("ALARM_TIMEOUT", 30),
    )
    _write_data_receipt(result)


def capture_screen() -> None:
    result = _execute_data_request(
        action="screen.capture",
        command="hola screenshot",
        timeout=_timeout("SCREENSHOT_TIMEOUT", 45),
    )
    _write_data_receipt(result)


def _write_data_receipt(result: dict[str, object]) -> None:
    """Emit a stable JSON envelope consumed by the transport adapter."""
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iphone-assistant-bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send", help="send one command through the sender agent")
    send.add_argument("words", nargs="+")
    send.add_argument(
        "--receipt-action",
        help="canonical receipt action supplied by the typed CLI policy",
    )
    commands.add_parser("sender", help="run the per-user sender agent")
    commands.add_parser("read-screen", help="request and return on-screen text")
    commands.add_parser("screenshot", help="request a screenshot and return its path")
    commands.add_parser("clipboard", help="request and return the iPhone clipboard")
    commands.add_parser("alarms", help="request and return enabled alarms")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "send":
            command = " ".join(args.words)
            result = execute_one_way(
                command,
                timeout=_timeout("IPHONE_ACTION_TIMEOUT", 30),
                expected_action=args.receipt_action,
            )
            sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        elif args.command == "sender":
            run_sender()
        elif args.command == "read-screen":
            read_screen()
        elif args.command == "screenshot":
            capture_screen()
        elif args.command == "clipboard":
            read_clipboard()
        elif args.command == "alarms":
            read_alarms()
        return 0
    except IPhoneError as error:
        print(f"iphone-assistant-bridge: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
