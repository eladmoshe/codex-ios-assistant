import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import iphone_cli.config as config
from iphone_cli.config import (
    _validated_socket_path,
    ensure_socket_parent,
    registration_socket,
    sender_socket,
)
from iphone_cli.errors import IPhoneError


class ConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
