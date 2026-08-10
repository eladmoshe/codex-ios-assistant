import http.server
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from iphone_cli.errors import IPhoneError
from iphone_cli.receiver import (
    ALARMS,
    CLIPBOARDS,
    COMPLETIONS,
    PENDING,
    TEXTS,
    Handler,
    RegistrationServer,
    MAX_REGISTRATION_BYTES,
    _read_line,
    accept_receipt,
    purge_inbox,
    poll_completion,
    register_pending,
    SCREENSHOT_RETENTION_SECONDS,
)


class ReceiverStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_state_path = os.environ.get("IOS_ASSISTANT_RECEIVER_STATE_PATH")
        self.state_path = Path(self.temporary.name) / "receiver-state.json"
        os.environ["IOS_ASSISTANT_RECEIVER_STATE_PATH"] = str(self.state_path)
        PENDING.clear()
        COMPLETIONS.clear()

    def tearDown(self):
        PENDING.clear()
        COMPLETIONS.clear()
        if self.previous_state_path is None:
            os.environ.pop("IOS_ASSISTANT_RECEIVER_STATE_PATH", None)
        else:
            os.environ["IOS_ASSISTANT_RECEIVER_STATE_PATH"] = self.previous_state_path
        self.temporary.cleanup()

    def test_pending_capability_and_completion_survive_process_memory_loss(self):
        request_id = "7" * 32
        capability = "8" * 64
        register_pending(request_id, capability, "screen.read")
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o600)
        self.assertNotIn(capability, self.state_path.read_text())

        PENDING.clear()
        COMPLETIONS.clear()
        self.assertEqual(poll_completion(request_id)["state"], "pending")
        accept_receipt(
            request_id,
            capability,
            "screen.read",
            "completed",
            data={"text": "private text survives restart"},
        )

        PENDING.clear()
        COMPLETIONS.clear()
        result = poll_completion(request_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["receipt_action"], "screen.read")
        self.assertEqual(result["data"], {"text": "private text survives restart"})


class ReceiverTests(unittest.TestCase):
    token = "test-token-that-is-longer-than-thirty-two-characters"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_state_path = os.environ.get("IOS_ASSISTANT_RECEIVER_STATE_PATH")
        os.environ["IOS_ASSISTANT_RECEIVER_STATE_PATH"] = str(
            Path(self.temporary.name) / "receiver-state.json"
        )
        TEXTS.clear()
        CLIPBOARDS.clear()
        ALARMS.clear()
        PENDING.clear()
        COMPLETIONS.clear()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.receiver_token = self.token
        self.server.inbox = Path(self.temporary.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.origin = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.previous_state_path is None:
            os.environ.pop("IOS_ASSISTANT_RECEIVER_STATE_PATH", None)
        else:
            os.environ["IOS_ASSISTANT_RECEIVER_STATE_PATH"] = self.previous_state_path
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        authenticated: bool = True,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["X-Auth"] = self.token
        request = Request(
            self.origin + path,
            method=method,
            data=body,
            headers=request_headers,
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def test_health_is_public_but_data_paths_require_authentication(self):
        status, body = self.request("GET", "/health", authenticated=False)
        self.assertEqual(status, 200)
        self.assertIn(b"receiver up", body)

        status, _ = self.request("GET", "/text/12345", authenticated=False)
        self.assertEqual(status, 403)

    def test_screen_text_round_trip_and_delete(self):
        payload = json.dumps(
            {
                "screen": "Wi-Fi\nConnected",
                "current_app": "Settings",
                "selected_text": "Home Network",
                "messages": "No alerts",
                "urls": "https://example.com",
            }
        ).encode()
        status, _ = self.request(
            "POST",
            "/text",
            body=payload,
            headers={"Content-Type": "application/json", "X-Screenshot-Id": "12345"},
        )
        self.assertEqual(status, 200)

        status, body = self.request("GET", "/text/12345")
        self.assertEqual(status, 200)
        value = body.decode()
        self.assertIn("current app is **Settings**", value)
        self.assertIn("<selected_text>\nHome Network", value)
        self.assertIn("<screen>\nWi-Fi\nConnected", value)

        status, _ = self.request("DELETE", "/text/12345")
        self.assertEqual(status, 204)
        status, _ = self.request("GET", "/text/12345")
        self.assertEqual(status, 404)

    def test_screenshot_is_saved_by_correlated_id(self):
        image = b"\x89PNG\r\n\x1a\n" + b"test-image"
        status, _ = self.request(
            "POST",
            "/photo",
            body=image,
            headers={"Content-Type": "image/png", "X-Screenshot-Id": "23456"},
        )
        self.assertEqual(status, 200)
        self.assertEqual((Path(self.temporary.name) / "shot-23456.png").read_bytes(), image)

    def test_expired_screenshot_artifacts_are_removed_but_unrelated_files_remain(self):
        inbox = Path(self.temporary.name)
        expired = inbox / ("shot-" + "a" * 32 + ".png")
        fresh = inbox / ("shot-" + "b" * 32 + ".png")
        legacy_expired = inbox / "shot-23456.bin"
        unrelated = inbox / "keep.txt"
        expired.write_bytes(b"expired")
        fresh.write_bytes(b"fresh")
        legacy_expired.write_bytes(b"legacy")
        unrelated.write_bytes(b"keep")
        now = max(path.stat().st_mtime for path in (expired, fresh, legacy_expired, unrelated))
        os.utime(expired, (now - SCREENSHOT_RETENTION_SECONDS - 1,) * 2)
        os.utime(legacy_expired, (now - SCREENSHOT_RETENTION_SECONDS - 1,) * 2)
        self.assertEqual(purge_inbox(inbox, now=now), 2)
        self.assertFalse(expired.exists())
        self.assertFalse(legacy_expired.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrelated.exists())

    def test_hardened_screenshot_retry_cannot_delete_accepted_artifact(self):
        request_id = "8" * 32
        capability = "9" * 64
        register_pending(request_id, capability, "screen.capture")
        image = b"\x89PNG\r\n\x1a\nconcurrent"
        statuses: list[int] = []

        def submit() -> None:
            status, _ = self.request(
                "POST",
                "/photo",
                body=image,
                headers={
                    "Content-Type": "image/png",
                    "X-Protocol-Version": "2",
                    "X-Request-Id": request_id,
                    "X-Receipt-Capability": capability,
                },
            )
            statuses.append(status)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(sorted(statuses), [200, 409])
        artifact = Path(self.temporary.name) / f"shot-{request_id}.png"
        self.assertEqual(artifact.read_bytes(), image)
        self.assertEqual(poll_completion(request_id)["status"], "completed")

    def test_empty_clipboard_round_trip(self):
        status, _ = self.request(
            "POST",
            "/clipboard",
            body=b"",
            headers={"X-Screenshot-Id": "34567"},
        )
        self.assertEqual(status, 200)
        status, body = self.request("GET", "/clipboard/34567")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_alarm_text_becomes_structured_enabled_records(self):
        payload = json.dumps(
            {"alarms": "7:30 AM\tWake up\tWeekdays\ttrue\n8:15 AM\t\tNever\tfalse"}
        ).encode()
        status, _ = self.request(
            "POST",
            "/get-alarm",
            body=payload,
            headers={"Content-Type": "application/json", "X-Screenshot-Id": "45678"},
        )
        self.assertEqual(status, 200)
        status, body = self.request("GET", "/get-alarm/45678")
        self.assertEqual(status, 200)
        alarms = json.loads(body)["alarms"]
        self.assertEqual(len(alarms), 2)
        self.assertEqual(alarms[1]["label"], "")
        self.assertFalse(alarms[1]["allows_snooze"])
        self.assertTrue(all(alarm["enabled"] for alarm in alarms))

    def test_hardened_receipt_is_correlated_and_single_use(self):
        request_id = "a" * 32
        capability = "b" * 64
        register_pending(request_id, capability, "timer.start")
        self.assertEqual(poll_completion(request_id)["state"], "pending")
        accept_receipt(request_id, capability, "timer.start", "completed")
        result = poll_completion(request_id)
        self.assertEqual(result["status"], "completed")
        with self.assertRaises(LookupError):
            accept_receipt(request_id, capability, "timer.start", "completed")

    def test_duplicate_request_ids_are_rejected_before_a_second_capability_exists(self):
        request_id = "0" * 32
        register_pending(request_id, "1" * 64, "timer.start")
        with self.assertRaisesRegex(IPhoneError, "already registered"):
            register_pending(request_id, "2" * 64, "timer.start")

    def test_registration_line_limit_excludes_the_framing_newline(self):
        left, right = socket.socketpair()
        try:
            payload = b"x" * MAX_REGISTRATION_BYTES + b"\n"
            sender = threading.Thread(target=left.sendall, args=(payload,))
            sender.start()
            self.assertEqual(_read_line(right, MAX_REGISTRATION_BYTES), b"x" * MAX_REGISTRATION_BYTES)
            sender.join(timeout=1)
            self.assertFalse(sender.is_alive())
        finally:
            left.close()
            right.close()

    def test_http_receipt_requires_matching_body_and_header_identity(self):
        request_id = "1" * 32
        capability = "2" * 64
        register_pending(request_id, capability, "home.open")
        payload = json.dumps(
            {
                "protocol_version": "2",
                "request_id": request_id,
                "receipt_action": "home.open",
                "status": "completed",
            }
        ).encode()
        status, _ = self.request(
            "POST",
            "/receipt",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "X-Protocol-Version": "2",
                "X-Request-Id": request_id,
                "X-Receipt-Capability": capability,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(poll_completion(request_id)["status"], "completed")

    def test_http_receipt_rejects_mismatched_legacy_action_alias(self):
        request_id = "6" * 32
        capability = "7" * 64
        register_pending(request_id, capability, "home.open")
        payload = json.dumps(
            {
                "protocol_version": 2,
                "request_id": request_id,
                "receipt_action": "home.open",
                "action": "home.show",
                "status": "completed",
            }
        ).encode()
        status, _ = self.request(
            "POST",
            "/receipt",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "X-Protocol-Version": "2",
                "X-Request-Id": request_id,
                "X-Receipt-Capability": capability,
            },
        )
        self.assertEqual(status, 400)

        next_request_id = "3" * 32
        next_capability = "4" * 64
        register_pending(next_request_id, next_capability, "home.open")
        mismatched = payload.replace(request_id.encode(), ("5" * 32).encode(), 1)
        status, _ = self.request(
            "POST",
            "/receipt",
            body=mismatched,
            headers={
                "Content-Type": "application/json",
                "X-Protocol-Version": "2",
                "X-Request-Id": next_request_id,
                "X-Receipt-Capability": next_capability,
            },
        )
        self.assertEqual(status, 400)

    def test_hardened_data_response_does_not_accept_static_token_alone(self):
        request_id = "c" * 32
        capability = "d" * 64
        register_pending(request_id, capability, "screen.read")
        payload = json.dumps({"screen": "private text"}).encode()
        status, _ = self.request(
            "POST",
            "/text",
            body=payload,
            authenticated=False,
            headers={
                "Content-Type": "application/json",
                "X-Protocol-Version": "2",
                "X-Request-Id": request_id,
                "X-Receipt-Capability": capability,
            },
        )
        self.assertEqual(status, 403)
        status, _ = self.request(
            "POST",
            "/text",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "X-Protocol-Version": "2",
                "X-Request-Id": request_id,
            },
        )
        self.assertEqual(status, 400)
        status, _ = self.request(
            "POST",
            "/text",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "X-Protocol-Version": "2",
                "X-Request-Id": request_id,
                "X-Receipt-Capability": capability,
            },
        )
        self.assertEqual(status, 200)
        result = poll_completion(request_id)
        self.assertEqual(result["status"], "completed")
        self.assertIn("private text", result["data"]["text"])

    def test_registration_socket_supports_register_poll_and_cancel(self):
        path = Path(self.temporary.name) / "registration.sock"
        registration = RegistrationServer(path)
        registration.start()
        try:
            for _ in range(20):
                if path.exists():
                    break
                threading.Event().wait(0.01)
            self.assertTrue(path.is_socket())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            def call(payload):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(1)
                    connection.connect(str(path))
                    connection.sendall(json.dumps(payload).encode() + b"\n")
                    return json.loads(connection.recv(16_384))

            request_id = "e" * 32
            capability = "f" * 64
            self.assertTrue(
                call(
                    {
                        "op": "register",
                        "protocol_version": 2,
                        "request_id": request_id,
                        "capability": capability,
                        "action": "timer.start",
                    }
                )["ok"]
            )
            self.assertEqual(call({"op": "poll", "request_id": request_id})["state"], "pending")
            self.assertTrue(call({"op": "cancel", "request_id": request_id})["ok"])
            self.assertEqual(call({"op": "poll", "request_id": request_id})["status"], "timeout")
        finally:
            registration.close()
            registration.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
