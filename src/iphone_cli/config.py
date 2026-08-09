"""Small, dependency-free configuration loader for codex-ios-assistant."""

from __future__ import annotations

import os
import shlex
from functools import lru_cache
from pathlib import Path

from .errors import IPhoneError


APP_NAME = "codex-ios-assistant"
CONFIG_DIR = Path(
    os.environ.get("IOS_ASSISTANT_CONFIG_DIR", Path.home() / ".config" / APP_NAME)
).expanduser()
CONFIG_FILE = Path(
    os.environ.get("IOS_ASSISTANT_CONFIG_FILE", CONFIG_DIR / "config.env")
).expanduser()
DATA_DIR = Path(
    os.environ.get("IOS_ASSISTANT_DATA_DIR", Path.home() / ".local" / "share" / APP_NAME)
).expanduser()
LOG_DIR = Path(
    os.environ.get("IOS_ASSISTANT_LOG_DIR", Path.home() / "Library" / "Logs" / APP_NAME)
).expanduser()


@lru_cache(maxsize=1)
def file_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if not CONFIG_FILE.is_file():
        return values
    for line_number, raw_line in enumerate(CONFIG_FILE.read_text().splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise IPhoneError(f"Invalid configuration at {CONFIG_FILE}:{line_number}.")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        try:
            parsed = shlex.split(raw_value.strip(), posix=True)
        except ValueError as error:
            raise IPhoneError(
                f"Invalid quoted value at {CONFIG_FILE}:{line_number}: {error}"
            ) from error
        if len(parsed) > 1:
            raise IPhoneError(
                f"Configuration values containing spaces must be quoted at "
                f"{CONFIG_FILE}:{line_number}."
            )
        values[name] = parsed[0] if parsed else ""
    return values


def value(name: str, *, required: bool = False) -> str:
    result = os.environ.get(name, file_values().get(name, "")).strip()
    if required and not result:
        raise IPhoneError(
            f"{name} is not configured. Run scripts/configure or edit {CONFIG_FILE}."
        )
    return result


def message_target() -> str:
    return value("IPHONE_MSG_TARGET", required=True)


def receiver_token() -> str:
    token = value("IPHONE_RECEIVER_TOKEN", required=True)
    if len(token) < 32:
        raise IPhoneError("IPHONE_RECEIVER_TOKEN must contain at least 32 characters.")
    return token


def public_url() -> str:
    url = value("IPHONE_PUBLIC_URL", required=True).rstrip("/")
    if not url.startswith("https://") or "/" in url[len("https://") :]:
        raise IPhoneError(
            "IPHONE_PUBLIC_URL must be an HTTPS origin such as "
            "https://iphone.example.com (without a path)."
        )
    return url


def receiver_port() -> int:
    raw = value("IPHONE_RECEIVER_PORT") or "8787"
    try:
        port = int(raw)
    except ValueError as error:
        raise IPhoneError("IPHONE_RECEIVER_PORT must be an integer.") from error
    if not 1 <= port <= 65535:
        raise IPhoneError("IPHONE_RECEIVER_PORT must be between 1 and 65535.")
    return port


def receiver_url() -> str:
    return f"http://127.0.0.1:{receiver_port()}"


def sender_socket() -> Path:
    configured = value("IPHONE_SENDER_SOCKET")
    if configured:
        return Path(configured).expanduser()
    return Path("/tmp") / f"codex-ios-assistant-{os.getuid()}.sock"


def registration_socket() -> Path:
    configured = value("IPHONE_REGISTRATION_SOCKET")
    if configured:
        return Path(configured).expanduser()
    return Path("/tmp") / f"codex-ios-assistant-receiver-{os.getuid()}.sock"
