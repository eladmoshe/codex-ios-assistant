"""Small, dependency-free configuration loader for codex-ios-assistant."""

from __future__ import annotations

import os
import shlex
import stat
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
PRIVATE_SOCKET_DIR_MODE = 0o700
PRIVATE_SOCKET_MODE = 0o600
PRIVATE_CONFIG_FILE_MODE = 0o600


def file_values() -> dict[str, str]:
    raw_contents = _read_private_config()
    values: dict[str, str] = {}
    if raw_contents is None:
        return values
    for line_number, raw_line in enumerate(raw_contents.splitlines(), 1):
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


def _ensure_private_config_readable() -> None:
    """Fail closed before every runtime read of the token-bearing config."""

    try:
        CONFIG_FILE.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise IPhoneError(f"Could not inspect configuration file {CONFIG_FILE}.") from error
    if not private_config_ready(CONFIG_FILE):
        raise IPhoneError(
            f"Refusing to read insecure configuration file {CONFIG_FILE}; expected an "
            "operator-owned mode-0600 file under a mode-0700 parent."
        )


def _read_private_config() -> str | None:
    """Read the config through an owner/mode-checked, no-follow file handle."""

    _ensure_private_config_readable()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(CONFIG_FILE, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise IPhoneError(f"Could not read configuration file {CONFIG_FILE}.") from error
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.getuid()
            or stat.S_IMODE(information.st_mode) != PRIVATE_CONFIG_FILE_MODE
        ):
            raise IPhoneError(
                f"Refusing to read insecure configuration file {CONFIG_FILE}; expected an "
                "operator-owned mode-0600 file under a mode-0700 parent."
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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
    path = Path(configured).expanduser() if configured else CONFIG_DIR / "sender.sock"
    return _validated_socket_path(path, "IPHONE_SENDER_SOCKET")


def registration_socket() -> Path:
    configured = value("IPHONE_REGISTRATION_SOCKET")
    path = Path(configured).expanduser() if configured else CONFIG_DIR / "receiver.sock"
    return _validated_socket_path(path, "IPHONE_REGISTRATION_SOCKET")


def _validated_socket_path(path: Path, setting: str) -> Path:
    """Return a private socket path and reject predictable/shared locations.

    The old default under ``/tmp`` was guessable by any local process.  A
    configured path is accepted only when it is absolute, outside ``/tmp``,
    directly inside the private application config directory, and its
    existing parent/socket are owned by this user without group/other access.
    Missing parents/sockets are validated when the service creates them via
    :func:`ensure_socket_parent`.
    """

    raw_path = path.expanduser()
    if raw_path.is_symlink():
        raise IPhoneError(f"{setting} must not be a symbolic link: {raw_path}")
    config_raw = CONFIG_DIR.expanduser()
    if config_raw.is_symlink():
        raise IPhoneError(
            f"{setting} config directory must not be a symbolic link: {config_raw}"
        )
    path = raw_path.resolve(strict=False)
    tmp_root = Path("/tmp").resolve()
    try:
        path.relative_to(tmp_root)
    except ValueError:
        pass
    else:
        raise IPhoneError(f"{setting} must point outside /tmp: {path}")

    config_root = config_raw.resolve(strict=False)
    try:
        path.relative_to(config_root)
    except ValueError as error:
        raise IPhoneError(
            f"{setting} must live directly under the private config directory: {config_root}"
        ) from error
    if path.parent != config_root:
        raise IPhoneError(f"{setting} must be a direct child of {config_root}")
    if config_root.exists():
        _validate_private_directory(config_root, setting)

    parent = path.parent
    if parent.exists():
        _validate_private_directory(parent, setting)
    if path.exists():
        information = path.lstat()
        if information.st_uid != os.getuid() or not stat.S_ISSOCK(information.st_mode):
            raise IPhoneError(f"{setting} must be a socket owned by the current user: {path}")
        if information.st_mode & 0o077:
            raise IPhoneError(f"{setting} socket must be mode 0600: {path}")
    return path


def _validate_private_directory(path: Path, setting: str) -> None:
    information = path.lstat()
    if (
        not stat.S_ISDIR(information.st_mode)
        or stat.S_ISLNK(information.st_mode)
        or information.st_uid != os.getuid()
        or stat.S_IMODE(information.st_mode) != PRIVATE_SOCKET_DIR_MODE
    ):
        raise IPhoneError(
            f"{setting} parent must be a mode-0700 directory owned by the current user: {path}"
        )


def private_config_ready(path: Path | None = None) -> bool:
    """Return whether a config file is a private, operator-owned regular file.

    The doctor and runtime loader must not treat a token-bearing config as
    private merely because it is readable. Reject symlinks, non-regular files,
    other owners, non-0600 modes, and a parent that is not an exact mode-0700
    directory.
    """

    target = CONFIG_FILE if path is None else path
    try:
        information = target.expanduser().lstat()
        if (
            stat.S_ISLNK(information.st_mode)
            or not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.getuid()
            or stat.S_IMODE(information.st_mode) != PRIVATE_CONFIG_FILE_MODE
        ):
            return False
        _validate_private_directory(target.expanduser().parent, "config file")
    except OSError:
        return False
    except IPhoneError:
        return False
    return True


def ensure_socket_parent(path: Path) -> None:
    """Create/validate the private parent directory before binding a socket."""

    parent = path.parent
    if parent.exists():
        _validate_private_directory(parent, "socket")
    else:
        parent.mkdir(parents=True, mode=PRIVATE_SOCKET_DIR_MODE, exist_ok=False)
        os.chmod(parent, PRIVATE_SOCKET_DIR_MODE)
        _validate_private_directory(parent, "socket")
