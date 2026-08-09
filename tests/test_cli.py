import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
IPHONE = ROOT / "iphone"


def run_cli(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if env:
        environment.update(env)
    return subprocess.run(
        [str(IPHONE), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


class CLITests(unittest.TestCase):
    def test_alarm_off_normalizes_time(self):
        result = run_cli(
            "alarm",
            "off",
            "10:30 PM",
            "--dry-run",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["time"], "22:30")
        self.assertEqual(
            value["command"][-5:],
            ["alarm", "off", "22:30", "--receipt-action", "alarm.disable_at"],
        )

    def test_alarm_off_rejects_invalid_time(self):
        result = run_cli("alarm", "off", "25:00", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alarm time must be", result.stderr)

    def test_alarm_set_normalizes_time_and_label(self):
        result = run_cli(
            "alarm",
            "set",
            "10:30 PM",
            "--label",
            "Wake up",
            "--dry-run",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["time"], "22:30")
        self.assertEqual(value["label"], "Wake up")
        self.assertEqual(
            value["command"][4:8],
            ["hola", "alarm", "set", "22:30"],
        )
        self.assertEqual(value["command"][-3:], ["Wake up", "--receipt-action", "alarm.set"])

    def test_alarm_set_rejects_invalid_time(self):
        result = run_cli("alarm", "set", "25:00", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alarm time must be", result.stderr)

    def test_alarm_list_returns_structured_records(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "get-alarms"
            helper.write_text(
                "#!/bin/sh\n"
                "printf '%s' '{\"protocol_version\":2,\"request_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"action\":\"alarm.list\",\"status\":\"completed\",\"data\":{\"alarms\":[{\"time\":\"7:30 AM\","
                "\"label\":\"Wake up\",\"repeat_days\":\"Weekdays\","
                "\"allows_snooze\":true,\"enabled\":true}]}}'\n"
            )
            helper.chmod(0o755)
            result = run_cli(
                "alarm",
                "list",
                "--json",
                env={"IPHONE_ALARM_COMMAND": str(helper)},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["alarms"][0]["label"], "Wake up")
        self.assertIn("7:30 AM — Wake up (Weekdays)", value["summary"])

    def test_alarm_list_handles_no_active_alarms(self):
        result = run_cli(
            "alarm",
            "list",
            env={
                "IPHONE_ALARM_COMMAND": "/usr/bin/printf '{\"protocol_version\":2,\"request_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"action\":\"alarm.list\",\"status\":\"completed\",\"data\":{\"alarms\":[]}}'"
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "No active alarms.\n")

    def test_bare_domain_defaults_to_https(self):
        result = run_cli("url", "open", "google.com", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("https://google.com", result.stdout)

    def test_clipboard_get_uses_response_helper(self):
        result = run_cli("clipboard", "get", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("iphone_cli.bridge clipboard", result.stdout)

    def test_clipboard_get_wraps_nonempty_contents(self):
        result = run_cli(
            "clipboard",
            "get",
            env={
                "IPHONE_CLIPBOARD_COMMAND": "/usr/bin/printf '{\"protocol_version\":2,\"request_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"action\":\"clipboard.get\",\"status\":\"completed\",\"data\":{\"text\":\"clipboard-value\"}}'"
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "<clipboard-contents>clipboard-value</clipboard-contents>",
        )

    def test_clipboard_get_distinguishes_an_empty_clipboard(self):
        result = run_cli(
            "clipboard",
            "get",
            env={
                "IPHONE_CLIPBOARD_COMMAND": "/usr/bin/printf '{\"protocol_version\":2,\"request_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"action\":\"clipboard.get\",\"status\":\"completed\",\"data\":{\"text\":\"\"}}'"
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "<clipboard-contents></clipboard-contents>")

    def test_screen_read_exposes_correlated_json_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "screen"
            helper.write_text(
                "#!/bin/sh\n"
                "printf '%s' '{\"protocol_version\":2,\"request_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"action\":\"screen.read\",\"status\":\"completed\",\"data\":{\"text\":\"Settings\\nWi-Fi\"}}'\n"
            )
            helper.chmod(0o755)
            result = run_cli(
                "screen",
                "read",
                "--json",
                env={"IPHONE_READ_SCREEN_COMMAND": str(helper)},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["receipt_action"], "screen.read")
        self.assertEqual(value["request_id"], "a" * 32)
        self.assertEqual(value["protocol_version"], 2)
        self.assertEqual(value["text"], "Settings\nWi-Fi")

    def test_screenshot_exposes_correlated_json_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            inbox = data_dir / "inbox"
            inbox.mkdir(parents=True)
            image = inbox / "shot.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            helper = root / "screenshot"
            helper.write_text(
                "#!/bin/sh\n"
                f"printf '%s' '{{\"protocol_version\":2,\"request_id\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\",\"action\":\"screen.capture\",\"status\":\"completed\",\"data\":{{\"path\":\"{image}\"}}}}'\n"
            )
            helper.chmod(0o755)
            result = run_cli(
                "screen",
                "capture",
                "--json",
                env={
                    "IPHONE_SCREENSHOT_COMMAND": str(helper),
                    "IOS_ASSISTANT_DATA_DIR": str(data_dir),
                },
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["receipt_action"], "screen.capture")
        self.assertEqual(value["request_id"], "b" * 32)
        self.assertTrue(value["path"].endswith("shot.png"))

    def test_mac_local_json_results_get_a_valid_correlation(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "contacts"
            helper.write_text("#!/bin/sh\nprintf '%s\\n' 'Dana' ' +15550101001'\n")
            helper.chmod(0o755)
            result = run_cli(
                "contacts",
                "search",
                "Dana",
                "--json",
                env={"IPHONE_CONTACTS_COMMAND": str(helper)},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["protocol_version"], 2)
        self.assertRegex(value["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(value["receipt_action"], "contacts.search")

    def test_messages_read_json_results_have_correlated_contract(self):
        cases = (
            (("chats", "--limit", "3"), "messages.chats"),
            (("history", "--chat-id", "123"), "messages.history"),
            (("search", "pizza", "--match", "contains", "--limit", "3"), "messages.search"),
            (("group", "--chat-id", "123"), "messages.group"),
        )
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "imsg"
            helper.write_text("#!/bin/sh\nprintf '%s\\n' '{\"id\":1,\"text\":\"hello\"}'\n")
            helper.chmod(0o755)
            for arguments, receipt_action in cases:
                with self.subTest(arguments=arguments):
                    result = run_cli(
                        "messages",
                        *arguments,
                        "--json",
                        env={"IPHONE_HISTORY_IMSG_COMMAND": str(helper)},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    value = json.loads(result.stdout)
                    self.assertEqual(value["protocol_version"], 2)
                    self.assertRegex(value["request_id"], r"^[0-9a-f]{32}$")
                    self.assertEqual(value["receipt_action"], receipt_action)

    def test_contacts_search_preserves_readable_output(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "contacts"
            helper.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'Johnny' '  +15550101001' '' "
                "'Johnny Appleseed' '  johnny@example.com' '  +15550101002'\n"
            )
            helper.chmod(0o755)
            result = run_cli(
                "contacts",
                "search",
                "Johnny",
                env={"IPHONE_CONTACTS_COMMAND": str(helper)},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "Johnny\n  +15550101001\n\n"
            "Johnny Appleseed\n  johnny@example.com\n  +15550101002\n",
        )

    def test_contacts_search_has_stable_json_output(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "contacts"
            helper.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'Johnny' '  +15550101001' '' "
                "'Johnny Appleseed' '  johnny@example.com' '  +15550101002'\n"
            )
            helper.chmod(0o755)
            result = run_cli(
                "contacts",
                "search",
                "Johnny",
                "--json",
                env={"IPHONE_CONTACTS_COMMAND": str(helper)},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(value["read_only"])
        self.assertEqual(value["query"], "Johnny")
        self.assertEqual(
            value["contacts"],
            [
                {"name": "Johnny", "emails": [], "phones": ["+15550101001"]},
                {
                    "name": "Johnny Appleseed",
                    "emails": ["johnny@example.com"],
                    "phones": ["+15550101002"],
                },
            ],
        )

    def test_contacts_search_dry_run_does_not_run_the_helper(self):
        result = run_cli(
            "contacts",
            "search",
            "Johnny",
            "--dry-run",
            "--json",
            env={"IPHONE_CONTACTS_COMMAND": "/missing/contacts-helper"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "dry-run")
        self.assertEqual(
            value["command"],
            ["/missing/contacts-helper", "search", "Johnny"],
        )

    def test_global_flag_after_leaf_command(self):
        result = run_cli("camera", "open", "--mode", "video", "--facing", "front", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("camera://configuration?capturemode=video&capturedevice=front", result.stdout)

    def test_global_flag_between_resource_and_action(self):
        result = run_cli("calendar", "--dry-run", "open", "--date", "2027-01-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"calshow:\d+\.0")

    def test_weather_opens_generically(self):
        result = run_cli("weather", "open", "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["resource"], "weather")
        self.assertEqual(value["url"], "weather://")

    def test_weather_opens_a_named_location_by_coordinates(self):
        result = run_cli(
            "weather",
            "open",
            "--location",
            "Chicago",
            "--lat",
            "41.8781",
            "--lng",
            "-87.6298",
            "--dry-run",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["location"], "Chicago")
        self.assertEqual(value["latitude"], 41.8781)
        self.assertEqual(value["longitude"], -87.6298)
        self.assertEqual(
            value["url"],
            "weather://weather.apple.com/?lat=41.8781&long=-87.6298",
        )

    def test_weather_location_label_requires_coordinates(self):
        result = run_cli("weather", "open", "--location", "Chicago", "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--location requires --latitude and --longitude", result.stderr)

    def test_uber_requires_coordinates(self):
        result = run_cli("uber", "open", "--destination", "Founders Inc", "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--latitude", result.stderr)
        self.assertIn("--longitude", result.stderr)

    def test_uber_uses_only_supplied_coordinates(self):
        result = run_cli(
            "uber",
            "open",
            "--destination",
            "Founders Inc",
            "--lat",
            "37.8001",
            "--lng",
            "-122.4376",
            "--dry-run",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["latitude"], 37.8001)
        self.assertEqual(value["longitude"], -122.4376)
        self.assertNotIn("geocoded", value)
        self.assertNotIn("attribution", value)

    def test_json_dry_run(self):
        result = run_cli("timer", "start", "10m", "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(value["ok"])
        self.assertEqual(value["status"], "dry-run")
        self.assertEqual(value["seconds"], 600)

    def test_default_transport_uses_the_packaged_sender_bridge(self):
        result = run_cli("timer", "start", "10m", "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["command"][0], str(Path(os.sys.executable)))
        self.assertEqual(value["command"][1:4], ["-m", "iphone_cli.bridge", "send"])
        self.assertEqual(value["command"][4:7], ["hola", "timer", "start"])
        self.assertEqual(value["command"][-2:], ["--receipt-action", "timer.start"])

    def test_openurl_transport_commands_keep_typed_receipt_actions(self):
        cases = (
            (("url", "open", "https://example.com"), "url.open"),
            (("camera", "open"), "camera.open"),
            (("messages", "open"), "messages.open"),
            (("messages", "compose", "--to", "+15550101001", "--body", "hello"), "messages.compose"),
            (("calculator", "evaluate", "40+2"), "calculator.evaluate"),
            (("photos", "open"), "photos.open"),
        )
        for arguments, receipt_action in cases:
            with self.subTest(arguments=arguments):
                result = run_cli(*arguments, "--dry-run", "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertEqual(value["receipt_action"], receipt_action)
                self.assertEqual(value["command"][-2:], ["--receipt-action", receipt_action])

    def test_specific_message_url(self):
        guid = "3690DA00-7DDA-4B72-A847-197715B89D82"
        result = run_cli("messages", "open", "--message", guid, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"sms://open?message-guid={guid}", result.stdout)

    def test_group_compose_resolves_the_synced_group_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            helper = temporary / "imsg"
            helper.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                "'{\"id\":2859,\"display_name\":\"best boys\",\"identifier\":\"chat-old\","
                "\"participants\":[\"+1\",\"+2\"],\"is_group\":true}' "
                "'{\"id\":2929,\"display_name\":\"Best Boys\",\"identifier\":\"chat-new\","
                "\"participants\":[\"+1\",\"+2\"],\"is_group\":true}'\n"
            )
            helper.chmod(0o755)
            database = temporary / "chat.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE chat (group_id TEXT)")
                connection.execute(
                    "INSERT INTO chat (ROWID, group_id) VALUES (?, ?)",
                    (2859, "BF86E595-E17B-4D78-8178-49EE8B53C06C"),
                )

            result = run_cli(
                "messages",
                "compose",
                "--to",
                "best boys",
                "--body",
                "hello there frens",
                "--dry-run",
                "--json",
                env={
                    "IPHONE_HISTORY_IMSG_COMMAND": str(helper),
                    "IPHONE_MESSAGES_DB": str(database),
                },
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["thread"], "best boys")
        self.assertEqual(value["group_id"], "BF86E595-E17B-4D78-8178-49EE8B53C06C")
        self.assertIn("groupid=BF86E595-E17B-4D78-8178-49EE8B53C06C", value["url"])
        self.assertIn("body=hello%20there%20frens", value["url"])

    def test_invalid_doordash_url_is_clear_error(self):
        result = run_cli(
            "doordash",
            "open",
            "--store-url",
            "https://example.com/store/123/",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Expected a URL on doordash.com", result.stderr)

    def test_spotify_open_uses_the_supplied_url(self):
        url = "https://open.spotify.com/playlist/37i9dQZF1DXaQm3ZVg9Z2X"
        result = run_cli("spotify", "open", url, "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["resource"], "spotify")
        self.assertEqual(value["url"], url)

    def test_success_messages_use_concise_past_tense(self):
        cases = (
            (("url", "open", "google.com"), "URL opened.\n"),
            (("control-center", "close"), "Control Center closed.\n"),
            (("flashlight", "on"), "Flashlight turned on.\n"),
            (("timer", "pause"), "Timer paused.\n"),
            (("low-power", "off"), "Low Power Mode turned off.\n"),
        )
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "receipt-helper"
            helper.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'hola openurl'*) action='url.open' ;;\n"
                "  *'hola controlcenter close'*) action='control_center.set' ;;\n"
                "  *'hola flashlight on'*) action='flashlight.set' ;;\n"
                "  *'hola timer pause'*) action='timer.pause' ;;\n"
                "  *'hola lowpower off'*) action='low_power.set' ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
                "printf '{\"protocol_version\":2,\"request_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"action\":\"%s\",\"status\":\"completed\"}\\n' \"$action\"\n"
            )
            helper.chmod(0o755)
            for arguments, expected in cases:
                with self.subTest(arguments=arguments):
                    result = run_cli(
                        *arguments,
                        env={"IPHONE_IMSG_COMMAND": str(helper)},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)

    def test_json_failed_receipt_preserves_typed_non_success_status(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "failed-receipt-helper"
            helper.write_text(
                "#!/bin/sh\n"
                "printf '%s' '{\"protocol_version\":2,\"request_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"action\":\"timer.pause\",\"status\":\"failed\",\"error_code\":\"shortcut_failed\"}'\n"
            )
            helper.chmod(0o755)
            result = run_cli(
                "timer",
                "pause",
                "--json",
                env={"IPHONE_IMSG_COMMAND": str(helper)},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(value["ok"])
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["receipt_action"], "timer.pause")

    def test_bare_messages_command_shows_actions_and_examples(self):
        result = run_cli("messages")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("{open,compose,chats,history,search,group}", result.stdout)
        self.assertIn("open Messages, a conversation, or a specific message", result.stdout)
        self.assertIn("open an unsent message draft", result.stdout)
        self.assertIn("search text across old messages", result.stdout)
        self.assertIn("iphone messages compose --to", result.stdout)

    def test_resource_help_lists_every_supported_action(self):
        expected = {
            "url": ("open",),
            "camera": ("open",),
            "weather": ("open",),
            "calendar": ("open",),
            "calculator": ("open", "evaluate"),
            "contacts": ("search",),
            "messages": ("open", "compose", "chats", "history", "search", "group"),
            "find-my": ("open",),
            "uber": ("open",),
            "doordash": ("open",),
            "spotify": ("open",),
            "books": ("open",),
            "app-store": ("open",),
            "photos": ("open",),
            "wallet": ("open",),
            "notes": ("open",),
            "screen": ("read", "capture"),
            "timer": ("start", "pause", "resume", "cancel"),
            "clipboard": ("copy", "get"),
        }
        for resource, actions in expected.items():
            with self.subTest(resource=resource):
                result = run_cli(resource, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("{" + ",".join(actions) + "}", result.stdout)
                for action in actions:
                    self.assertRegex(result.stdout, rf"(?m)^\s+{action}\s+\S")

    def test_messages_search_wraps_only_the_read_command(self):
        result = run_cli(
            "messages",
            "search",
            "pizza tonight",
            "--match",
            "exact",
            "--limit",
            "7",
            "--dry-run",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(value["read_only"])
        self.assertEqual(
            value["command"][1:],
            ["search", "--query", "pizza tonight", "--match", "exact", "--limit", "7", "--json"],
        )

    def test_messages_send_is_not_exposed(self):
        result = run_cli("messages", "send", "--to", "+15551234567", "--text", "hello")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_removed_watch_and_speak_commands_are_not_exposed(self):
        for arguments in (("messages", "watch"), ("messages", "stats"), ("speak", "hello")):
            with self.subTest(arguments=arguments):
                result = run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("invalid choice", result.stderr)

    def test_history_help_says_newest_messages_are_first(self):
        result = run_cli("messages", "history", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("newest messages shown first", result.stdout)
        self.assertNotIn("chronological order", result.stdout)

    def test_screen_help_uses_the_exact_read_command(self):
        result = run_cli("screen", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("iphone screen read", result.stdout)
        self.assertNotIn("iphone screen text", result.stdout)

    def test_messages_chats_preserves_human_imsg_output(self):
        result = run_cli(
            "messages",
            "chats",
            "--limit",
            "3",
            env={"IPHONE_HISTORY_IMSG_COMMAND": "/bin/echo"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "chats --limit 3\n")


if __name__ == "__main__":
    unittest.main()
