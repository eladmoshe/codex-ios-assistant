import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import iphone_cli.config as config
import iphone_cli.transport as transport
from iphone_cli.config import (
    _validated_socket_path,
    ensure_socket_parent,
    private_config_ready,
    registration_socket,
    sender_socket,
)
from iphone_cli.errors import IPhoneError


class ConfigTests(unittest.TestCase):
    def test_service_config_validation_checks_every_required_value(self):
        valid = {
            "IPHONE_MSG_TARGET": "target",
            "IPHONE_PUBLIC_URL": "https://iphone.example",
            "IPHONE_RECEIVER_TOKEN": "x" * 32,
            "IPHONE_COMMAND_SECRET": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            config, "CONFIG_DIR", Path(directory)
        ), patch.object(config, "file_values", return_value={}):
            Path(directory).chmod(0o700)
            with patch.dict(os.environ, valid, clear=True):
                config.validate_runtime_config()
            for missing in valid:
                with self.subTest(missing=missing), patch.dict(
                    os.environ,
                    {key: value for key, value in valid.items() if key != missing},
                    clear=True,
                ), self.assertRaises(IPhoneError):
                    config.validate_runtime_config()

    def test_command_secret_requires_exact_lowercase_hex(self):
        with patch.dict(os.environ, {"IPHONE_COMMAND_SECRET": "a" * 64}):
            self.assertEqual(config.command_secret(), "a" * 64)
        for invalid in ("a" * 63, "A" * 64, "g" * 64):
            with self.subTest(invalid=invalid), patch.dict(
                os.environ, {"IPHONE_COMMAND_SECRET": invalid}
            ), self.assertRaisesRegex(IPhoneError, "64 lowercase hexadecimal"):
                config.command_secret()

    def test_default_socket_paths_are_private_and_outside_tmp(self):
        for path in (sender_socket(), registration_socket()):
            self.assertFalse(path.is_relative_to(Path("/tmp")))

    def test_socket_parent_is_created_mode_0700(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            path = config_root / "sender.sock"
            ensure_socket_parent(path)
            with patch.object(config, "CONFIG_DIR", config_root):
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(_validated_socket_path(path, "TEST_SOCKET"), path.resolve())

    def test_world_accessible_socket_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            config_root.mkdir(mode=0o700)
            parent = Path(directory) / "shared"
            parent.mkdir(mode=0o755)
            with patch.object(config, "CONFIG_DIR", config_root):
                with self.assertRaises(IPhoneError):
                    _validated_socket_path(parent / "sender.sock", "TEST_SOCKET")

    def test_socket_outside_config_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            config_root.mkdir(mode=0o700)
            with patch.object(config, "CONFIG_DIR", config_root):
                with self.assertRaises(IPhoneError):
                    _validated_socket_path(Path(directory) / "other.sock", "TEST_SOCKET")

    def test_private_config_requires_exact_file_and_parent_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            config_root.mkdir(mode=0o700)
            config_file = config_root / "config.env"
            config_file.write_text("IPHONE_RECEIVER_TOKEN=private\n")
            config_file.chmod(0o600)
            self.assertTrue(private_config_ready(config_file))

            config_file.chmod(0o640)
            self.assertFalse(private_config_ready(config_file))
            config_file.chmod(0o600)
            config_root.chmod(0o750)
            self.assertFalse(private_config_ready(config_file))

    def test_private_config_rejects_symlink_and_non_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            config_root.mkdir(mode=0o700)
            target = config_root / "real.env"
            target.write_text("IPHONE_RECEIVER_TOKEN=private\n")
            target.chmod(0o600)
            link = config_root / "config.env"
            link.symlink_to(target)
            self.assertFalse(private_config_ready(link))
            target.unlink()
            target.mkdir(mode=0o700)
            self.assertFalse(private_config_ready(target))

    def test_runtime_loader_rejects_permission_drift_after_initial_read(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            config_root.mkdir(mode=0o700)
            config_file = config_root / "config.env"
            config_file.write_text("IPHONE_RECEIVER_TOKEN=private\n")
            config_file.chmod(0o600)
            with patch.object(config, "CONFIG_FILE", config_file):
                self.assertEqual(config.file_values()["IPHONE_RECEIVER_TOKEN"], "private")
                config_file.chmod(0o644)
                with self.assertRaisesRegex(IPhoneError, "Refusing to read insecure"):
                    config.file_values()

    def test_runtime_loader_rejects_symlink_drift_after_initial_read(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            config_root.mkdir(mode=0o700)
            config_file = config_root / "config.env"
            config_file.write_text("IPHONE_RECEIVER_TOKEN=private\n")
            config_file.chmod(0o600)
            target = config_root / "other.env"
            target.write_text("IPHONE_RECEIVER_TOKEN=exposed\n")
            target.chmod(0o600)
            with patch.object(config, "CONFIG_FILE", config_file):
                self.assertEqual(config.file_values()["IPHONE_RECEIVER_TOKEN"], "private")
                config_file.unlink()
                config_file.symlink_to(target)
                with self.assertRaisesRegex(IPhoneError, "Refusing to read insecure"):
                    config.file_values()

    def test_doctor_marks_insecure_config_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            config_root.mkdir(mode=0o700)
            config_file = config_root / "config.env"
            config_file.write_text(
                "IPHONE_MSG_TARGET=target\n"
                "IPHONE_PUBLIC_URL=https://iphone.example\n"
                "IPHONE_RECEIVER_TOKEN=private-token\n"
            )
            config_file.chmod(0o644)
            values = {
                "IPHONE_MSG_TARGET": "target",
                "IPHONE_PUBLIC_URL": "https://iphone.example",
                "IPHONE_RECEIVER_TOKEN": "private-token",
            }
            with patch.object(transport, "CONFIG_FILE", config_file), patch.object(
                transport, "file_values", return_value=values
            ), patch.object(transport, "sender_socket", return_value=config_root / "sender.sock"), patch.object(
                transport, "registration_socket", return_value=config_root / "receiver.sock"
            ), patch.object(transport, "receiver_url", return_value="http://127.0.0.1:1"):
                report = transport.dependency_report()
            self.assertFalse(report[0]["available"])

    def test_doctor_rejects_malformed_command_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            config_root.mkdir(mode=0o700)
            config_file = config_root / "config.env"
            config_file.write_text("private\n")
            config_file.chmod(0o600)
            values = {
                "IPHONE_MSG_TARGET": "target",
                "IPHONE_PUBLIC_URL": "https://iphone.example",
                "IPHONE_RECEIVER_TOKEN": "private-token-with-at-least-32-characters",
                "IPHONE_COMMAND_SECRET": "A" * 64,
            }
            with patch.object(transport, "CONFIG_FILE", config_file), patch.object(
                transport, "file_values", return_value=values
            ), patch.object(transport, "sender_socket", return_value=config_root / "sender.sock"), patch.object(
                transport, "registration_socket", return_value=config_root / "receiver.sock"
            ), patch.object(transport, "receiver_url", return_value="http://127.0.0.1:1"):
                report = transport.dependency_report()
            self.assertFalse(report[0]["available"])

    def test_doctor_does_not_read_an_insecure_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "config"
            config_root.mkdir(mode=0o700)
            config_file = config_root / "config.env"
            config_file.write_text("IPHONE_RECEIVER_TOKEN=private\n")
            config_file.chmod(0o644)
            with patch.object(transport, "CONFIG_FILE", config_file), patch.object(
                transport, "file_values", side_effect=AssertionError("insecure file was read")
            ), patch.object(transport, "sender_socket", return_value=config_root / "sender.sock"), patch.object(
                transport, "registration_socket", return_value=config_root / "receiver.sock"
            ), patch.object(transport, "receiver_url", return_value="http://127.0.0.1:1"):
                report = transport.dependency_report()
            self.assertFalse(report[0]["available"])


if __name__ == "__main__":
    unittest.main()
