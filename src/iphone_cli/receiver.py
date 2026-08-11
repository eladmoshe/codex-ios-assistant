"""Receiver for iPhone data and versioned, one-time execution receipts.

The public HTTP listener is intentionally boring: it accepts only bounded,
typed payloads.  Registration and polling happen over a separate mode-0600
Unix socket owned by the receiver process.  A tunnel makes a remote request
look like loopback traffic, so loopback source checks are not an authorization
mechanism here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from typing import Any

from .config import DATA_DIR, ensure_socket_parent, receiver_port, receiver_token, registration_socket
from .errors import IPhoneError
from .protocol import (
    ACTION_PATTERN,
    PROTOCOL_VERSION,
    RECEIPT_CAPABILITY_PATTERN,
    REQUEST_ID_PATTERN,
)


MAX_PENDING = 512
MAX_COMPLETIONS = 200
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_REGISTRATION_BYTES = 8_192
MAX_RECEIPT_BYTES = 16_384
MAX_DATA_BYTES = 64_000
MAX_TEXT_CHARS = 20_000
MAX_ALARMS = 100
MAX_SCREENSHOT_BYTES = 12_000_000
MAX_HTTP_WORKERS = 16
HTTP_REQUEST_TIMEOUT_SECONDS = 15
SCREENSHOT_RETENTION_SECONDS = 10 * 60
PENDING_TTL_SECONDS = 180
COMPLETION_TTL_SECONDS = 600
STATE_FILE_ENV = "IOS_ASSISTANT_RECEIVER_STATE_PATH"
ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


# These maps remain for backward-compatible interactive use of the original
# static-token endpoints. Hardened CLI requests never read them directly.
TEXTS: dict[str, str] = {}
CLIPBOARDS: dict[str, str] = {}
ALARMS: dict[str, list[dict[str, object]]] = {}


@dataclasses.dataclass(frozen=True)
class PendingRequest:
    request_id: str
    capability_hash: bytes
    expected_action: str
    expires_at: float


@dataclasses.dataclass(frozen=True)
class Completion:
    request_id: str
    action: str
    status: str
    data: dict[str, object]
    error_code: str | None
    created_at: float


PENDING: dict[str, PendingRequest] = {}
COMPLETIONS: dict[str, Completion] = {}
STATE_LOCK = threading.RLock()

SCREEN_TEXT_POST_PATHS = {"/text", "/screentext"}
SCREENSHOT_POST_PATHS = {"/photo", "/screenshot", "/shot"}
HARDENED_ACTION_BY_PATH = {
    "/text": "screen.read",
    "/screentext": "screen.read",
    "/photo": "screen.capture",
    "/screenshot": "screen.capture",
    "/shot": "screen.capture",
    "/clipboard": "clipboard.get",
    "/get-alarm": "alarm.list",
}
# Data-producing actions have a correlated response endpoint.  A generic
# receipt for one of these actions must never consume the pending capability;
# otherwise a later response carrying the actual data would be rejected as a
# replay.  Derive this set from the route registry so aliases cannot drift.
DEDICATED_DATA_ACTIONS = frozenset(HARDENED_ACTION_BY_PATH.values())


def _now() -> float:
    return time.time()


def _hash_capability(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def _state_file() -> Path:
    configured = os.environ.get(STATE_FILE_ENV)
    path = Path(configured).expanduser() if configured else DATA_DIR / "receiver-state.json"
    parent = path.parent
    if parent.is_symlink():
        raise IPhoneError(f"Refusing receiver state under symbolic-link directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    information = parent.stat()
    if information.st_uid != os.getuid() or stat.S_IMODE(information.st_mode) != 0o700:
        raise IPhoneError(f"Receiver state directory must be operator-owned mode 0700: {parent}")
    if path.is_symlink():
        raise IPhoneError(f"Refusing symbolic-link receiver state file: {path}")
    return path


def _pending_from_json(value: object) -> PendingRequest:
    if not isinstance(value, dict):
        raise IPhoneError("Receiver pending state is malformed.")
    request_id = value.get("request_id")
    capability_hash = value.get("capability_hash")
    expected_action = value.get("expected_action")
    expires_at = value.get("expires_at")
    if (
        not _valid_request_id(request_id)
        or not isinstance(capability_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", capability_hash) is None
        or not _valid_action(expected_action)
        or not isinstance(expires_at, (int, float))
    ):
        raise IPhoneError("Receiver pending state contains invalid fields.")
    return PendingRequest(request_id, bytes.fromhex(capability_hash), expected_action, float(expires_at))


def _completion_from_json(value: object) -> Completion:
    if not isinstance(value, dict):
        raise IPhoneError("Receiver completion state is malformed.")
    request_id = value.get("request_id")
    action = value.get("action")
    status = value.get("status")
    data = value.get("data")
    error_code = value.get("error_code")
    created_at = value.get("created_at")
    if (
        not _valid_request_id(request_id)
        or not _valid_action(action)
        or status not in {"completed", "failed", "timeout"}
        or not isinstance(data, dict)
        or (error_code is not None and (not isinstance(error_code, str) or ERROR_CODE_PATTERN.fullmatch(error_code) is None))
        or not isinstance(created_at, (int, float))
    ):
        raise IPhoneError("Receiver completion state contains invalid fields.")
    return Completion(request_id, action, status, dict(data), error_code, float(created_at))


def _load_state_locked() -> None:
    path = _state_file()
    if not path.exists():
        PENDING.clear()
        COMPLETIONS.clear()
        return
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise IPhoneError(f"Could not open receiver state file: {path}") from error
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.getuid()
            or stat.S_IMODE(information.st_mode) != 0o600
            or information.st_size > MAX_STATE_BYTES
        ):
            raise IPhoneError("Receiver state file must be operator-owned mode 0600 and bounded.")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            document = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IPhoneError("Receiver state file is unreadable or malformed.") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(document, dict) or document.get("version") != 1:
        raise IPhoneError("Receiver state file has an unsupported version.")
    pending_values = document.get("pending")
    completion_values = document.get("completions")
    if not isinstance(pending_values, list) or not isinstance(completion_values, list):
        raise IPhoneError("Receiver state file is malformed.")
    pending = [_pending_from_json(value) for value in pending_values]
    completions = [_completion_from_json(value) for value in completion_values]
    if len(pending) > MAX_PENDING or len(completions) > MAX_COMPLETIONS:
        raise IPhoneError("Receiver state file exceeds bounded registry limits.")
    PENDING.clear()
    PENDING.update((value.request_id, value) for value in pending)
    COMPLETIONS.clear()
    COMPLETIONS.update((value.request_id, value) for value in completions)


def _persist_state_locked() -> None:
    path = _state_file()
    while True:
        document = {
            "version": 1,
            "pending": [
                {
                    "request_id": value.request_id,
                    "capability_hash": value.capability_hash.hex(),
                    "expected_action": value.expected_action,
                    "expires_at": value.expires_at,
                }
                for value in PENDING.values()
            ],
            "completions": [dataclasses.asdict(value) for value in COMPLETIONS.values()],
        }
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) <= MAX_STATE_BYTES:
            break
        if not COMPLETIONS:
            raise IPhoneError("Receiver pending state exceeds its durable size limit.")
        COMPLETIONS.pop(next(iter(COMPLETIONS)))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _valid_request_id(value: object) -> bool:
    return isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) is not None


def _valid_capability(value: object) -> bool:
    return isinstance(value, str) and RECEIPT_CAPABILITY_PATTERN.fullmatch(value) is not None


def _valid_action(value: object) -> bool:
    return isinstance(value, str) and ACTION_PATTERN.fullmatch(value) is not None


def _trim_text(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    value = value.replace("\x00", "")
    return value if len(value) <= limit else value[:limit] + "\n[truncated]"


def _purge_locked(now: float | None = None) -> bool:
    instant = _now() if now is None else now
    changed = False
    for request_id, pending in list(PENDING.items()):
        if pending.expires_at <= instant:
            PENDING.pop(request_id, None)
            changed = True
            if request_id not in COMPLETIONS:
                _store_completion_locked(
                    Completion(
                        request_id=request_id,
                        action=pending.expected_action,
                        status="timeout",
                        data={},
                        error_code="receipt_timeout",
                        created_at=instant,
                    )
                )
    for request_id, completion in list(COMPLETIONS.items()):
        if completion.created_at + COMPLETION_TTL_SECONDS <= instant:
            COMPLETIONS.pop(request_id, None)
            changed = True
    return changed


def _bounded_map_insert(mapping: dict[str, Any], key: str, value: Any, limit: int = 200) -> None:
    mapping[key] = value
    while len(mapping) > limit:
        mapping.pop(next(iter(mapping)))


def _store_completion_locked(completion: Completion) -> None:
    _bounded_map_insert(COMPLETIONS, completion.request_id, completion, MAX_COMPLETIONS)


def register_pending(
    request_id: str,
    capability: str,
    expected_action: str,
    *,
    expires_at: float | None = None,
) -> None:
    if not _valid_request_id(request_id):
        raise IPhoneError("request id must be exactly 32 lowercase hexadecimal characters")
    if not _valid_capability(capability):
        raise IPhoneError("receipt capability must be exactly 64 lowercase hexadecimal characters")
    if not _valid_action(expected_action):
        raise IPhoneError("action must be a lowercase dotted identifier")
    expiry = _now() + PENDING_TTL_SECONDS if expires_at is None else float(expires_at)
    if not _now() < expiry <= _now() + PENDING_TTL_SECONDS:
        raise IPhoneError("receipt expiry is outside the allowed window")
    with STATE_LOCK:
        _load_state_locked()
        if _purge_locked():
            _persist_state_locked()
        if request_id in PENDING or request_id in COMPLETIONS:
            raise IPhoneError("request id is already registered")
        if len(PENDING) >= MAX_PENDING:
            raise IPhoneError("receiver pending-request limit reached")
        PENDING[request_id] = PendingRequest(
            request_id=request_id,
            capability_hash=_hash_capability(capability),
            expected_action=expected_action,
            expires_at=expiry,
        )
        _persist_state_locked()


def cancel_pending(request_id: str, *, status: str = "timeout") -> None:
    with STATE_LOCK:
        _load_state_locked()
        _purge_locked()
        pending = PENDING.pop(request_id, None)
        if pending is not None:
            _store_completion_locked(
                Completion(
                    request_id=request_id,
                    action=pending.expected_action,
                    status=status,
                    data={},
                    error_code="receipt_timeout" if status == "timeout" else status,
                    created_at=_now(),
                )
            )
        _persist_state_locked()


def poll_completion(request_id: str) -> dict[str, object]:
    if not _valid_request_id(request_id):
        raise IPhoneError("request id must be exactly 32 lowercase hexadecimal characters")
    with STATE_LOCK:
        _load_state_locked()
        changed = _purge_locked()
        completion = COMPLETIONS.get(request_id)
        if completion is not None:
            result = {
                "ok": True,
                "protocol_version": PROTOCOL_VERSION,
                "state": "complete",
                "request_id": completion.request_id,
                "action": completion.action,
                "receipt_action": completion.action,
                "status": completion.status,
                "data": completion.data,
                "error_code": completion.error_code,
            }
            if changed:
                _persist_state_locked()
            return result
        pending = PENDING.get(request_id)
        if pending is None:
            result = {
                "ok": True,
                "protocol_version": PROTOCOL_VERSION,
                "state": "unknown",
                "request_id": request_id,
            }
            if changed:
                _persist_state_locked()
            return result
        result = {
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
            "state": "pending",
            "request_id": request_id,
            "action": pending.expected_action,
            "receipt_action": pending.expected_action,
            "expires_at": pending.expires_at,
        }
        if changed:
            _persist_state_locked()
        return result


def accept_receipt(
    request_id: str,
    capability: str,
    action: str,
    status: str,
    *,
    data: dict[str, object] | None = None,
    error_code: str | None = None,
) -> None:
    if not _valid_request_id(request_id) or not _valid_capability(capability):
        raise PermissionError("invalid receipt credentials")
    if not _valid_action(action) or status not in {"completed", "failed"}:
        raise ValueError("invalid receipt fields")
    if error_code is not None and ERROR_CODE_PATTERN.fullmatch(error_code) is None:
        raise ValueError("invalid receipt error code")
    with STATE_LOCK:
        _load_state_locked()
        if _purge_locked():
            _persist_state_locked()
        pending = PENDING.get(request_id)
        if pending is None:
            raise LookupError("unknown or already consumed request")
        if pending.expires_at <= _now():
            PENDING.pop(request_id, None)
            raise LookupError("expired request")
        if pending.expected_action != action:
            raise PermissionError("receipt action does not match request")
        if not hmac.compare_digest(pending.capability_hash, _hash_capability(capability)):
            raise PermissionError("receipt capability does not match request")
        # Consume before returning success. A retry or replay cannot produce a
        # second completion even if the public receiver response is lost.
        PENDING.pop(request_id, None)
        _store_completion_locked(
            Completion(
                request_id=request_id,
                action=action,
                status=status,
                data=dict(data or {}),
                error_code=error_code,
                created_at=_now(),
            )
        )
        _persist_state_locked()


def id_from_header(value: str) -> str | None:
    """Legacy compatibility parser for the original static-token endpoints."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        return value
    digits = re.search(r"[0-9]{4,10}", value)
    return digits.group(0) if digits else None


def compose(payload: dict[str, object], screen: str) -> str:
    def section(tag: str, value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return f"<{tag}>\n{_trim_text(value.strip())}\n</{tag}>"
        return None

    parts: list[str] = []
    app = payload.get("current_app")
    if isinstance(app, str) and app.strip():
        parts.append(
            f"The user's current app is **{_trim_text(app.strip(), 200)}**. "
            "You can read the contents of their screen below."
        )
    selected = section("selected_text", payload.get("selected_text"))
    if selected:
        parts.append("The user has selected/highlighted this text on their screen:")
        parts.append(selected)
    parts.extend(
        value
        for value in (
            section("messages", payload.get("messages")),
            section("webpage", payload.get("webpage")),
            section("urls", payload.get("urls")),
            section("screen", screen),
        )
        if value
    )
    return _trim_text("\n\n".join(parts))


def largest_multipart_payload(body: bytes, content_type: str) -> bytes | None:
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        return None
    best: bytes | None = None
    for part in body.split(b"--" + match.group(1).encode()):
        if b"\r\n\r\n" not in part:
            continue
        payload = part.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n-")
        if best is None or len(payload) > len(best):
            best = payload
    return best


def _peer_is_current_user(connection: socket.socket) -> bool:
    getpeereid = getattr(connection, "getpeereid", None)
    if getpeereid is not None:
        peer_uid, _ = getpeereid()
        return peer_uid == os.getuid()
    # Linux test/dev environments expose SO_PEERCRED instead of getpeereid.
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is not None:
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, so_peercred, 12)
            uid = int.from_bytes(credentials[4:8], "little")
            return uid == os.getuid()
        except OSError:
            return False
    # macOS does not expose peer credentials for AF_UNIX sockets in the
    # system Python. The listener is mode 0600 and lives in a 0700 parent,
    # so the filesystem permission is the authorization mechanism there.
    return True


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
    line = data.split(b"\n", 1)[0]
    if len(line) > limit:
        raise ValueError("registration message exceeded its size limit")
    return line


# Hardened requests use a 32-lowercase-hex request id. Legacy static-token
# screenshots use the narrower ``id_from_header`` alphabet and may fall back
# to ``.bin`` when the payload has no recognized image signature.
SCREENSHOT_NAME_PATTERN = re.compile(
    r"^shot-[A-Za-z0-9_-]{1,32}\.(?:png|jpg|bin)(?:\.part)?$"
)


def purge_inbox(inbox: Path, *, now: float | None = None) -> int:
    """Delete expired receiver-owned screenshot artifacts.

    Nami copies a screenshot into its own controlled artifact location.  The
    public receiver retains the temporary source only for a short bounded
    window, and never follows links or removes unrelated files in the inbox.
    """

    instant = time.time() if now is None else now
    removed = 0
    try:
        entries = list(inbox.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if SCREENSHOT_NAME_PATTERN.fullmatch(entry.name) is None:
            continue
        try:
            information = entry.lstat()
            if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
                continue
            if information.st_uid != os.getuid():
                continue
            if information.st_mtime + SCREENSHOT_RETENTION_SECONDS > instant:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _write_private_file(path: Path, data: bytes) -> None:
    """Create a receiver artifact without following a pre-existing symlink."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags | nofollow, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class RegistrationServer(threading.Thread):
    """Private receiver-side registration/poll/cancel API."""

    daemon = True

    def __init__(self, path: Path):
        super().__init__(name="iphone-receipt-registration")
        self.path = path
        self.stop_event = threading.Event()
        self.listener: socket.socket | None = None

    def run(self) -> None:
        ensure_socket_parent(self.path)
        if self.path.exists():
            mode = self.path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                print(f"refusing to replace non-socket registration path {self.path}", flush=True)
                return
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener = listener
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(8)
            listener.settimeout(0.5)
            while not self.stop_event.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    self._handle(connection)
        finally:
            listener.close()
            self.listener = None
            try:
                if self.path.exists() and stat.S_ISSOCK(self.path.stat().st_mode):
                    self.path.unlink()
            except OSError:
                pass

    def _handle(self, connection: socket.socket) -> None:
        try:
            if not _peer_is_current_user(connection):
                raise PermissionError("registration connection came from another user")
            request = json.loads(_read_line(connection, MAX_REGISTRATION_BYTES))
            if not isinstance(request, dict):
                raise ValueError("registration request must be an object")
            operation = request.get("op")
            if operation == "register":
                register_pending(
                    request.get("request_id"),  # type: ignore[arg-type]
                    request.get("capability"),  # type: ignore[arg-type]
                    request.get("action"),  # type: ignore[arg-type]
                    expires_at=request.get("expires_at"),  # type: ignore[arg-type]
                )
                response: dict[str, object] = {
                    "ok": True,
                    "protocol_version": PROTOCOL_VERSION,
                    "state": "registered",
                    "request_id": request.get("request_id"),
                }
            elif operation == "poll":
                response = poll_completion(request.get("request_id"))  # type: ignore[arg-type]
            elif operation == "cancel":
                request_id = request.get("request_id")
                if not _valid_request_id(request_id):
                    raise IPhoneError("invalid request id")
                cancel_pending(request_id)
                response = {
                    "ok": True,
                    "protocol_version": PROTOCOL_VERSION,
                    "state": "canceled",
                    "request_id": request_id,
                }
            else:
                raise ValueError("unknown registration operation")
        except (IPhoneError, KeyError, TypeError, ValueError, PermissionError, OSError) as error:
            response = {"ok": False, "error": str(error)}
        try:
            connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        except BrokenPipeError:
            pass

    def close(self) -> None:
        self.stop_event.set()
        listener = self.listener
        if listener is not None:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                    wake.settimeout(0.2)
                    wake.connect(str(self.path))
            except OSError:
                pass


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "CodexIOSAssistantReceiver/2"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(HTTP_REQUEST_TIMEOUT_SECONDS)

    @property
    def token(self) -> str:
        return self.server.receiver_token  # type: ignore[attr-defined]

    @property
    def inbox(self) -> Path:
        return self.server.inbox  # type: ignore[attr-defined]

    def _reply(
        self,
        code: int,
        text: str,
        *,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if hmac.compare_digest(self.headers.get("X-Auth", ""), self.token):
            return True
        self._reply(403, "bad token\n")
        return False

    def _hardened_credentials(self, path: str) -> tuple[str, str, str] | None:
        if self.headers.get("X-Protocol-Version") != str(PROTOCOL_VERSION):
            return None
        request_id = self.headers.get("X-Request-Id", "")
        capability = self.headers.get("X-Receipt-Capability", "")
        action = HARDENED_ACTION_BY_PATH.get(path)
        if not _valid_request_id(request_id) or not _valid_capability(capability) or action is None:
            self._reply(400, "invalid hardened receipt headers\n")
            return ()
        return request_id, capability, action

    def do_GET(self) -> None:
        alarm = re.fullmatch(r"/get-alarm/([A-Za-z0-9_-]{1,32})", self.path)
        if alarm:
            if not self._authorized():
                return
            records = ALARMS.get(alarm.group(1))
            if records is None:
                return self._reply(404, "no alarms for that id\n")
            return self._reply(
                200,
                json.dumps({"alarms": records}, ensure_ascii=False),
                content_type="application/json; charset=utf-8",
            )
        clipboard = re.fullmatch(r"/clipboard/([A-Za-z0-9_-]{1,32})", self.path)
        if clipboard:
            if not self._authorized():
                return
            text = CLIPBOARDS.get(clipboard.group(1))
            if text is None:
                return self._reply(404, "no clipboard for that id\n")
            return self._reply(200, text)
        screen = re.fullmatch(r"/(?:screentext|text)/([A-Za-z0-9_-]{1,32})", self.path)
        if screen:
            if not self._authorized():
                return
            text = TEXTS.get(screen.group(1))
            if text is None:
                return self._reply(404, "no text for that id\n")
            return self._reply(200, text + "\n")
        if self.path in {"/", "/health"}:
            return self._reply(200, "codex-ios-assistant receiver up\n")
        self._reply(404, "unknown endpoint\n")

    def do_DELETE(self) -> None:
        alarm = re.fullmatch(r"/get-alarm/([A-Za-z0-9_-]{1,32})", self.path)
        if alarm:
            if not self._authorized():
                return
            ALARMS.pop(alarm.group(1), None)
            return self._reply(204, "")
        clipboard = re.fullmatch(r"/clipboard/([A-Za-z0-9_-]{1,32})", self.path)
        if clipboard:
            if not self._authorized():
                return
            CLIPBOARDS.pop(clipboard.group(1), None)
            return self._reply(204, "")
        screen = re.fullmatch(r"/(?:screentext|text)/([A-Za-z0-9_-]{1,32})", self.path)
        if screen:
            if not self._authorized():
                return
            TEXTS.pop(screen.group(1), None)
            return self._reply(204, "")
        self._reply(404, "unknown endpoint\n")

    def do_POST(self) -> None:
        # Authenticate and classify the route before touching Content-Length
        # bytes.  A public tunnel makes remote callers look loopback-local,
        # and reading an unauthenticated 12 MiB upload would otherwise let a
        # caller consume memory/worker time before being rejected.
        if self.path == "/receipt":
            if not self._authorized():
                return
            credentials: tuple[str, str, str] | None = None
            limit = MAX_RECEIPT_BYTES
        elif self.path in HARDENED_ACTION_BY_PATH:
            if not self._authorized():
                return
            credentials = None
            if self.headers.get("X-Protocol-Version"):
                hardened = self._hardened_credentials(self.path)
                if hardened == ():
                    return
                if hardened is None:
                    return self._reply(401, "hardened receipt headers required\n")
                credentials = hardened
            limit = (
                MAX_SCREENSHOT_BYTES
                if self.path in SCREENSHOT_POST_PATHS
                else MAX_DATA_BYTES
            )
        else:
            return self._reply(404, "unknown endpoint\n")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._reply(400, "bad length\n")
        if not 0 <= length <= limit or (length == 0 and self.path not in {"/clipboard"}):
            return self._reply(400, "bad length\n")
        data = self.rfile.read(length)
        if self.path == "/receipt":
            return self.handle_receipt(data)
        if credentials is not None:
            return self.handle_hardened_data(data, *credentials)
        if self.path in SCREEN_TEXT_POST_PATHS:
            return self.handle_text(data)
        if self.path == "/clipboard":
            return self.handle_clipboard(data)
        if self.path == "/get-alarm":
            return self.handle_alarms(data)
        if self.path in SCREENSHOT_POST_PATHS:
            return self.handle_screenshot(data)
        self._reply(404, "unknown endpoint\n")

    def handle_receipt(self, data: bytes) -> None:
        if not self._authorized():
            return
        if self.headers.get("X-Protocol-Version") != str(PROTOCOL_VERSION):
            return self._reply(400, "unsupported receipt protocol\n")
        request_id = self.headers.get("X-Request-Id", "")
        capability = self.headers.get("X-Receipt-Capability", "")
        if not _valid_request_id(request_id) or not _valid_capability(capability):
            return self._reply(400, "invalid receipt credentials\n")
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return self._reply(400, "receipt body is not valid JSON\n")
        if not isinstance(payload, dict):
            return self._reply(400, "receipt body must be an object\n")
        action = payload.get("receipt_action")
        status = payload.get("status")
        if not _valid_action(action) or status not in {"completed", "failed"}:
            return self._reply(400, "invalid receipt_action or status\n")
        legacy_action = payload.get("action")
        if legacy_action is not None and legacy_action != action:
            return self._reply(400, "receipt action aliases do not match\n")
        # Shortcuts serializes dictionary values as strings even when the
        # semantic value is numeric. Accept both representations at this
        # boundary while keeping the emitted receipt version numeric.
        if payload.get("protocol_version") not in {PROTOCOL_VERSION, str(PROTOCOL_VERSION)}:
            return self._reply(400, "invalid receipt protocol version\n")
        body_request_id = payload.get("request_id")
        if not _valid_request_id(body_request_id) or body_request_id != request_id:
            return self._reply(400, "receipt request id does not match headers\n")
        error_code = payload.get("error_code")
        if error_code is not None and (
            not isinstance(error_code, str) or ERROR_CODE_PATTERN.fullmatch(error_code) is None
        ):
            return self._reply(400, "invalid receipt error code\n")
        if action in DEDICATED_DATA_ACTIONS:
            return self._reply(409, "data action requires its dedicated endpoint\n")
        try:
            accept_receipt(
                request_id,
                capability,
                action,
                status,
                error_code=error_code,
            )
        except PermissionError:
            return self._reply(403, "receipt authorization failed\n")
        except LookupError:
            return self._reply(404, "receipt request is unknown or expired\n")
        except ValueError as error:
            return self._reply(400, f"{error}\n")
        self._reply(200, json.dumps({"ok": True, "protocol_version": PROTOCOL_VERSION}))

    def handle_hardened_data(self, data: bytes, request_id: str, capability: str, action: str) -> None:
        if not self._authorized():
            return
        content_type = self.headers.get("Content-Type", "")
        path = self.path
        if path in SCREENSHOT_POST_PATHS:
            purge_inbox(self.inbox)
            if content_type.startswith("multipart/form-data"):
                data = largest_multipart_payload(data, content_type) or data
            if not data or len(data) > MAX_SCREENSHOT_BYTES:
                return self._reply(400, "screenshot exceeds size limit\n")
            extension = ".png" if data[:4] == b"\x89PNG" else ".jpg" if data[:2] == b"\xff\xd8" else None
            if extension is None:
                return self._reply(400, "screenshot must be PNG or JPEG\n")
            path_value = self.inbox / f"shot-{request_id}{extension}"
            try:
                # Publish directly with O_EXCL. A retry for the same request
                # can never replace an accepted artifact or unlink another
                # request's file when capability consumption races it.
                _write_private_file(path_value, data)
            except FileExistsError:
                return self._reply(409, "screenshot request already has an artifact\n")
            created_identity = path_value.lstat()
            try:
                accept_receipt(request_id, capability, action, "completed", data={"path": str(path_value.resolve())})
            except (PermissionError, LookupError):
                try:
                    current_identity = path_value.lstat()
                    if (
                        current_identity.st_dev == created_identity.st_dev
                        and current_identity.st_ino == created_identity.st_ino
                    ):
                        path_value.unlink()
                except OSError:
                    pass
                return self._reply(403, "receipt authorization failed\n")
            return self._reply(200, json.dumps({"ok": True, "protocol_version": PROTOCOL_VERSION}))

        if path in SCREEN_TEXT_POST_PATHS:
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                return self._reply(400, "body is not valid JSON\n")
            screen = payload.get("screen") if isinstance(payload, dict) else None
            if not isinstance(screen, str) or not screen.strip():
                return self._reply(400, "no usable screen text\n")
            text = compose(payload, screen)
            value = {"text": text}
        elif path == "/clipboard":
            try:
                payload = json.loads(data) if data else ""
            except (ValueError, UnicodeDecodeError):
                try:
                    payload = data.decode("utf-8")
                except UnicodeDecodeError:
                    return self._reply(400, "clipboard body is not UTF-8 text\n")
            if isinstance(payload, str):
                text = payload
            elif isinstance(payload, dict) and isinstance(payload.get("clipboard"), str):
                text = payload["clipboard"]
            else:
                return self._reply(400, "clipboard JSON must be a string or clipboard field\n")
            value = {"text": _trim_text(text)}
        elif path == "/get-alarm":
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                return self._reply(400, "body is not valid JSON\n")
            records = _parse_alarm_records(payload)
            value = {"alarms": records}
        else:
            return self._reply(404, "unknown endpoint\n")
        try:
            accept_receipt(request_id, capability, action, "completed", data=value)
        except PermissionError:
            return self._reply(403, "receipt authorization failed\n")
        except LookupError:
            return self._reply(404, "receipt request is unknown or expired\n")
        self._reply(200, json.dumps({"ok": True, "protocol_version": PROTOCOL_VERSION}))

    def handle_screenshot(self, data: bytes) -> None:
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            data = largest_multipart_payload(data, content_type) or data
        extension = ".png" if data[:4] == b"\x89PNG" else ".jpg" if data[:2] == b"\xff\xd8" else ".bin"
        request_id = id_from_header(self.headers.get("X-Screenshot-Id", ""))
        if not request_id:
            return self._reply(400, "missing or unusable X-Screenshot-Id\n")
        path = self.inbox / f"shot-{request_id}{extension}"
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        os.replace(temporary, path)
        print(f"saved {path.name} ({len(data)} bytes)", flush=True)
        self._reply(200, f"saved {path.name}\n")

    def handle_text(self, data: bytes) -> None:
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return self._reply(400, "body is not valid JSON\n")
        text = payload.get("screen") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            if isinstance(payload, dict):
                text = next(
                    (value for value in payload.values() if isinstance(value, str) and value.strip()),
                    None,
                )
        if not isinstance(text, str) or not text:
            return self._reply(400, "no usable text in JSON\n")
        request_id = id_from_header(self.headers.get("X-Screenshot-Id", ""))
        if not request_id:
            return self._reply(400, "missing or unusable X-Screenshot-Id\n")
        TEXTS[request_id] = compose(payload, text)
        while len(TEXTS) > 200:
            TEXTS.pop(next(iter(TEXTS)))
        print(f"stored screen text for {request_id} ({len(text)} chars; value redacted)", flush=True)
        self._reply(200, f"stored text for {request_id}\n")

    def handle_clipboard(self, data: bytes) -> None:
        try:
            payload = json.loads(data) if data else ""
        except (ValueError, UnicodeDecodeError):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return self._reply(400, "clipboard body is not UTF-8 text\n")
        else:
            if isinstance(payload, str):
                text = payload
            elif isinstance(payload, dict) and isinstance(payload.get("clipboard"), str):
                text = payload["clipboard"]
            else:
                return self._reply(400, "clipboard JSON must be a string or clipboard field\n")
        request_id = id_from_header(self.headers.get("X-Screenshot-Id", ""))
        if not request_id:
            return self._reply(400, "missing or unusable X-Screenshot-Id\n")
        CLIPBOARDS[request_id] = text
        while len(CLIPBOARDS) > 200:
            CLIPBOARDS.pop(next(iter(CLIPBOARDS)))
        print(f"stored clipboard for {request_id} ({len(text)} chars; value redacted)", flush=True)
        self._reply(200, f"stored clipboard for {request_id}\n")

    def handle_alarms(self, data: bytes) -> None:
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return self._reply(400, "body is not valid JSON\n")
        try:
            records = _parse_alarm_records(payload)
        except ValueError as error:
            return self._reply(400, f"{error}\n")
        request_id = id_from_header(self.headers.get("X-Screenshot-Id", ""))
        if not request_id:
            return self._reply(400, "missing or unusable X-Screenshot-Id\n")
        ALARMS[request_id] = records
        while len(ALARMS) > 200:
            ALARMS.pop(next(iter(ALARMS)))
        print(f"stored {len(records)} active alarms for {request_id} (details redacted)", flush=True)
        self._reply(200, f"stored alarms for {request_id}\n")

    def log_message(self, format_string: str, *arguments: object) -> None:
        print(f"{self.address_string()} {format_string % arguments}", flush=True)


class BoundedThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded server with a hard cap and periodic screenshot cleanup."""

    daemon_threads = True
    request_queue_size = MAX_HTTP_WORKERS * 2

    def __init__(self, *arguments: object, max_workers: int = MAX_HTTP_WORKERS, **kwargs: object):
        super().__init__(*arguments, **kwargs)
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self._last_inbox_purge = 0.0

    def process_request(self, request: socket.socket, client_address: object) -> None:
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return

        def worker() -> None:
            try:
                self.finish_request(request, client_address)
            except BaseException:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)
                self._worker_slots.release()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def service_actions(self) -> None:
        super().service_actions()
        now = time.time()
        if now - self._last_inbox_purge < 30:
            return
        self._last_inbox_purge = now
        inbox = getattr(self, "inbox", None)
        if isinstance(inbox, Path):
            purge_inbox(inbox, now=now)


def _parse_alarm_records(payload: object) -> list[dict[str, object]]:
    value = payload.get("alarms") if isinstance(payload, dict) else None
    records: list[dict[str, object]] = []
    if isinstance(value, str):
        for line in value.splitlines()[:MAX_ALARMS]:
            if not line.strip():
                continue
            parts = line.split("\t", 3)
            parts += [""] * (4 - len(parts))
            snooze = parts[3].strip().lower()
            allows_snooze = (
                True if snooze in {"true", "yes", "1", "on"}
                else False if snooze in {"false", "no", "0", "off"}
                else None
            )
            records.append(
                {
                    "time": _trim_text(parts[0].strip(), 64),
                    "label": _trim_text(parts[1].strip(), 200),
                    "repeat_days": _trim_text(parts[2].strip(), 200),
                    "allows_snooze": allows_snooze,
                    "enabled": True,
                }
            )
    elif isinstance(value, list) and all(isinstance(item, dict) for item in value[:MAX_ALARMS]):
        for item in value[:MAX_ALARMS]:
            records.append(
                {
                    key: (_trim_text(val, 200) if isinstance(val, str) else val)
                    for key, val in item.items()
                    if isinstance(key, str) and len(key) <= 64
                }
            )
    else:
        raise ValueError("alarms must be a text value or an array of objects")
    return records


def main() -> int:
    inbox = DATA_DIR / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    os.chmod(inbox, 0o700)
    token = receiver_token()
    registration = RegistrationServer(registration_socket())
    registration.start()
    try:
        server = BoundedThreadingHTTPServer(("127.0.0.1", receiver_port()), Handler)
    except BaseException:
        registration.close()
        registration.join(timeout=2)
        raise
    server.receiver_token = token  # type: ignore[attr-defined]
    server.inbox = inbox  # type: ignore[attr-defined]
    print(
        f"receiver listening on 127.0.0.1:{receiver_port()}, "
        f"registration socket {registration_socket()}, saving to {inbox}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.shutdown()
        server.server_close()
        registration.close()
        registration.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
