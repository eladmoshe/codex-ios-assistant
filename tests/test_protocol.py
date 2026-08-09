import unittest
from unittest.mock import patch

from iphone_cli.errors import IPhoneError
from iphone_cli.bridge import _one_way_action, _poll_registered, build_parser
from iphone_cli.protocol import (
    PROTOCOL_VERSION,
    new_receipt_capability,
    new_request_id,
    parse_receipt_token,
    protocol_command,
)


class ProtocolTests(unittest.TestCase):
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
        )
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
            )

    def test_poll_timeout_is_a_typed_non_success_result(self):
        request_id = "a" * 32
        with patch(
            "iphone_cli.bridge._registration_request",
            side_effect=[
                {"ok": True, "protocol_version": PROTOCOL_VERSION, "state": "canceled"},
                {
                    "ok": True,
                    "protocol_version": PROTOCOL_VERSION,
                    "state": "complete",
                    "request_id": request_id,
                    "action": "timer.start",
                    "status": "timeout",
                    "data": {},
                    "error_code": "receipt_timeout",
                },
            ],
        ):
            result = _poll_registered(request_id, 0, "timer.start")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["action"], "timer.start")

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
