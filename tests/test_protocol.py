import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import iphone_cli.transport as transport
import iphone_cli.bridge as bridge
from iphone_cli.errors import IPhoneError
from iphone_cli.bridge import (
    MAX_COMMAND_BYTES,
    _execute_data_request,
    _handle_sender_connection,
    _one_way_action,
    _poll_registered,
    _sender_payload,
    _send_imessage,
    build_parser,
    execute_one_way,
)
from iphone_cli.protocol import (
    PROTOCOL_VERSION,
    new_receipt_capability,
    new_request_id,
    parse_receipt_token,
    protocol_command,
)

COMMAND_SECRET = "a" * 64


def wire(command: str) -> str:
    return f"{COMMAND_SECRET} {command}"


class ProtocolTests(unittest.TestCase):
    def test_outer_transport_budget_covers_sender_loss_and_receipt_reconciliation(self):
        request_id = "d" * 32
        for kind in ("hola", "screen-read", "screen-capture", "clipboard-read", "alarm-read"):
            with self.subTest(kind=kind):
                operation = transport.Operation(
                    resource="timer",
                    action="pause",
                    kind=kind,
                    arguments=("timer", "pause"),
                    receipt_action="timer.pause",
                )
                receipt = json.dumps(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": request_id,
                        "action": "timer.pause",
                        "receipt_action": "timer.pause",
                        "status": "timeout",
                        "data": {},
                        "error_code": "receipt_poll_unavailable",
                    }
                )
                with patch("iphone_cli.transport._run", return_value=receipt) as run:
                    result = transport.execute(operation, timeout=30, request_id=request_id)
                self.assertEqual(result.status, "timeout")
                self.assertEqual(run.call_args.kwargs["timeout"], 70)

    def test_every_shortcut_one_way_branch_has_one_canonical_receipt_action(self):
        cases = {
            "hola timer start 600": "timer.start",
            "hola timer pause": "timer.pause",
            "hola timer resume": "timer.resume",
            "hola timer cancel": "timer.cancel",
            "hola flashlight on": "flashlight.set",
            "hola flashlight off": "flashlight.set",
            "hola call +15550101001": "call.start",
            "hola lowpower on": "low_power.set",
            "hola lowpower off": "low_power.set",
            "hola copytoclipboard hello": "clipboard.copy",
            "hola getclipboard": "clipboard.get",
            "hola controlcenter open": "control_center.set",
            "hola controlcenter close": "control_center.set",
            "hola openurl https://example.test": "url.open",
            "hola screentext": "screen.read",
            "hola screenshot": "screen.capture",
            "hola homescreen": "home.open",
            "hola alarm get": "alarm.list",
            "hola alarm set 07:15 gym": "alarm.set",
            "hola alarm off 07:15": "alarm.disable_at",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(_one_way_action(command), expected)

    def test_protocol_command_contains_machine_owned_correlations(self):
        request_id = new_request_id()
        capability = new_receipt_capability()
        command = protocol_command(
            "hola timer start 600",
            action="timer.start",
            request_id=request_id,
            capability=capability,
            command_secret=COMMAND_SECRET,
        )
        self.assertTrue(command.startswith(f"{COMMAND_SECRET} hola "))
        self.assertIn(f"--v={PROTOCOL_VERSION}", command)
        self.assertIn(f"--request-id={request_id}", command)
        self.assertIn(f"--receipt={request_id}.{capability}", command)
        self.assertIn("--action=timer.start", command)
        self.assertEqual(parse_receipt_token(command), (request_id, capability))

    def test_reserved_metadata_cannot_be_smuggled_in_arguments(self):
        with self.assertRaises(IPhoneError):
            protocol_command(
                "hola openurl https://example.test --receipt=attacker",
                action="url.open",
                request_id=new_request_id(),
                capability=new_receipt_capability(),
                command_secret=COMMAND_SECRET,
            )

    def test_sender_payload_escapes_newlines_and_preserves_unicode(self):
        command = wire("hola copytoclipboard café ☕\nsecond line")
        payload = _sender_payload(command)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertLessEqual(len(payload) - 1, MAX_COMMAND_BYTES)
        self.assertEqual(json.loads(payload), {"command": command})

    def test_sender_payload_rejects_public_or_wrong_length_prefixes(self):
        for command in ("hola homescreen", f"{'b' * 63} hola homescreen"):
            with self.subTest(command=command), self.assertRaisesRegex(
                IPhoneError, "secret-prefixed"
            ):
                _sender_payload(command)

    def test_sender_service_rejects_a_different_well_formed_secret(self):
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        client.sendall(_sender_payload(f"{'b' * 64} hola homescreen"))
        with patch("iphone_cli.bridge.command_secret", return_value=COMMAND_SECRET), patch(
            "iphone_cli.bridge._send_imessage"
        ) as send:
            _handle_sender_connection(server)
        response = json.loads(client.recv(4096).split(b"\n", 1)[0])
        self.assertFalse(response["ok"])
        self.assertIn("configured secret-prefixed", response["error"])
        send.assert_not_called()

    def test_messages_helper_keeps_command_secret_out_of_process_arguments(self):
        command = wire("hola homescreen --v=2")
        observed_path = None

        def inspect_run(arguments, **kwargs):
            nonlocal observed_path
            self.assertNotIn(command, arguments)
            observed_path = arguments[-2]
            self.assertEqual(Path(observed_path).read_text(encoding="utf-8"), command)
            return transport.subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            bridge, "CONFIG_DIR", Path(directory)
        ), patch("iphone_cli.bridge.message_target", return_value="private-target"), patch(
            "iphone_cli.bridge.subprocess.run", side_effect=inspect_run
        ):
            _send_imessage(command)
        self.assertIsNotNone(observed_path)
        self.assertFalse(Path(observed_path).exists())

    def test_sender_payload_accepts_exact_encoded_budget(self):
        # The budget is on the UTF-8 JSON line, not on Python characters.
        prefix = f'{{"command":"{COMMAND_SECRET} hola copytoclipboard '
        suffix = '"}'
        value = "x" * (MAX_COMMAND_BYTES - len(prefix.encode()) - len(suffix.encode()))
        payload = _sender_payload(wire("hola copytoclipboard " + value))
        self.assertEqual(len(payload) - 1, MAX_COMMAND_BYTES)

    def test_sender_payload_rejects_one_byte_over_budget(self):
        prefix = f'{{"command":"{COMMAND_SECRET} hola copytoclipboard '
        suffix = '"}'
        value = "x" * (MAX_COMMAND_BYTES - len(prefix.encode()) - len(suffix.encode()) + 1)
        with self.assertRaisesRegex(IPhoneError, "4096-byte"):
            _sender_payload(wire("hola copytoclipboard " + value))

    def test_sender_payload_multibyte_boundary_is_measured_in_utf8_bytes(self):
        command_prefix = wire("hola copytoclipboard ")
        candidate = "é" * 4_000
        while True:
            try:
                payload = _sender_payload(command_prefix + candidate)
            except IPhoneError:
                candidate = candidate[:-1]
                continue
            break
        self.assertLessEqual(len(payload) - 1, MAX_COMMAND_BYTES)
        with self.assertRaisesRegex(IPhoneError, "4096-byte"):
            _sender_payload(command_prefix + candidate + "é")

    def test_sender_payload_rejects_carriage_return_and_nul(self):
        for value in (
            wire("hola copytoclipboard line\rbreak"),
            wire("hola copytoclipboard line\x00break"),
        ):
            with self.subTest(value=repr(value)), self.assertRaises(IPhoneError):
                _sender_payload(value)

    def test_poll_timeout_is_a_typed_non_success_result(self):
        request_id = "a" * 32
        with patch(
            "iphone_cli.bridge._registration_request",
            side_effect=[
                {
                    "ok": True,
                    "protocol_version": PROTOCOL_VERSION,
                    "state": "canceled",
                    "request_id": request_id,
                },
                {
                    "ok": True,
                    "protocol_version": PROTOCOL_VERSION,
                    "state": "complete",
                    "request_id": request_id,
                    "action": "timer.start",
                    "receipt_action": "timer.start",
                    "status": "timeout",
                    "data": {},
                    "error_code": "receipt_timeout",
                },
            ],
        ):
            result = _poll_registered(request_id, 0, "timer.start")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["action"], "timer.start")

    def test_post_dispatch_mismatched_poll_is_timeout_not_failure(self):
        with patch(
            "iphone_cli.bridge._registration_request",
            return_value={
                "ok": True,
                "protocol_version": PROTOCOL_VERSION,
                "state": "complete",
                "request_id": "b" * 32,
                "receipt_action": "timer.start",
                "status": "completed",
                "data": {},
            },
        ), patch("iphone_cli.bridge.time.monotonic", side_effect=[0, 0.1, 1.1]), patch(
            "iphone_cli.bridge.time.sleep"
        ):
            result = _poll_registered("a" * 32, 1, "timer.start")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["error_code"], "receipt_poll_unavailable")

    def test_post_dispatch_receiver_outage_is_timeout_not_failure(self):
        with patch(
            "iphone_cli.bridge._registration_request",
            side_effect=IPhoneError("receiver restarted"),
        ), patch("iphone_cli.bridge.time.monotonic", side_effect=[0, 0.1, 1.1]), patch(
            "iphone_cli.bridge.time.sleep"
        ):
            result = _poll_registered("a" * 32, 1, "timer.start")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["error_code"], "receipt_poll_unavailable")

    def test_unknown_receipt_state_after_possible_dispatch_is_timeout_not_failure(self):
        request_id = "d" * 32
        with patch(
            "iphone_cli.bridge._registration_request",
            return_value={
                "ok": True,
                "protocol_version": PROTOCOL_VERSION,
                "state": "unknown",
                "request_id": request_id,
            },
        ):
            result = _poll_registered(request_id, 1, "timer.start")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["error_code"], "receipt_state_unknown")

    def test_execute_one_way_echoes_caller_request_id_through_registration_and_receipt(self):
        request_id = "c" * 32
        with patch("iphone_cli.bridge.command_secret", return_value=COMMAND_SECRET), patch(
            "iphone_cli.bridge._register"
        ) as register, patch(
            "iphone_cli.bridge.send_command"
        ) as send, patch(
            "iphone_cli.bridge._poll_registered",
            return_value={
                "request_id": request_id,
                "action": "timer.start",
                "receipt_action": "timer.start",
                "status": "completed",
                "data": {},
            },
        ):
            result = execute_one_way(
                "hola timer start 60",
                expected_action="timer.start",
                request_id=request_id,
            )
        self.assertEqual(register.call_args.args[0], request_id)
        self.assertIn(f"--request-id={request_id}", send.call_args.args[0])
        self.assertEqual(result["request_id"], request_id)
        self.assertEqual(result["receipt_action"], "timer.start")

    def test_sender_response_loss_still_polls_for_phone_receipt(self):
        request_id = "e" * 32
        with patch("iphone_cli.bridge.command_secret", return_value=COMMAND_SECRET), patch(
            "iphone_cli.bridge._register"
        ), patch(
            "iphone_cli.bridge.send_command", side_effect=IPhoneError("sender response lost")
        ), patch(
            "iphone_cli.bridge._poll_registered",
            return_value={
                "request_id": request_id,
                "action": "timer.start",
                "receipt_action": "timer.start",
                "status": "completed",
                "data": {},
            },
        ) as poll:
            result = execute_one_way(
                "hola timer start 60",
                expected_action="timer.start",
                request_id=request_id,
            )
        poll.assert_called_once_with(request_id, 30, "timer.start")
        self.assertEqual(result["status"], "completed")

    def test_data_request_sender_response_loss_still_polls_for_phone_receipt(self):
        request_id = "f" * 32
        with patch("iphone_cli.bridge.command_secret", return_value=COMMAND_SECRET), patch(
            "iphone_cli.bridge._register"
        ), patch(
            "iphone_cli.bridge.send_command", side_effect=IPhoneError("sender response lost")
        ), patch(
            "iphone_cli.bridge._poll_registered",
            return_value={
                "request_id": request_id,
                "action": "screen.read",
                "receipt_action": "screen.read",
                "status": "completed",
                "data": {"text": "hello"},
            },
        ) as poll:
            result = _execute_data_request(
                action="screen.read",
                command="hola screentext",
                timeout=30,
                request_id=request_id,
            )
        poll.assert_called_once_with(request_id, 30, "screen.read")
        self.assertEqual(result["status"], "completed")

    def test_bridge_accepts_policy_owned_receipt_action_for_openurl_branches(self):
        args = build_parser().parse_args(
            [
                "send",
                "hola",
                "openurl",
                "camera://",
                "--receipt-action",
                "camera.open",
            ]
        )
        self.assertEqual(args.receipt_action, "camera.open")


if __name__ == "__main__":
    unittest.main()
