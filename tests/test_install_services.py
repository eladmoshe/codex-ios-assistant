import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SERVICES = ROOT / "scripts" / "install-services"


class InstallServicesTests(unittest.TestCase):
    def test_preflight_cannot_be_masked_by_shell_only_iphone_overrides(self):
        module = runpy.run_path(str(INSTALL_SERVICES), run_name="install_services_test")
        environment = {
            "PATH": "/attacker/path",
            "IPHONE_RECEIVER_PORT": "8787",
            "IPHONE_COMMAND_SECRET": "a" * 64,
            "IOS_ASSISTANT_CONFIG_DIR": "/tmp/override",
        }
        with patch.dict(os.environ, environment, clear=True):
            filtered = module["launchagent_validation_environment"]()
        self.assertNotEqual(filtered["PATH"], "/attacker/path")
        self.assertNotIn("IPHONE_RECEIVER_PORT", filtered)
        self.assertNotIn("IPHONE_COMMAND_SECRET", filtered)
        self.assertNotIn("IOS_ASSISTANT_CONFIG_DIR", filtered)

    def test_sender_and_receiver_commands_clear_inherited_environment(self):
        module = runpy.run_path(str(INSTALL_SERVICES), run_name="install_services_test")
        arguments = module["file_backed_python"]("iphone_cli.receiver")
        self.assertEqual(arguments[:2], ["/usr/bin/env", "-i"])
        self.assertIn("HOME=" + str(Path.home()), arguments)
        self.assertEqual(arguments[-2:], [str(module["PYTHON"]), "-m", "iphone_cli.receiver"][-2:])
        self.assertNotIn("IPHONE_RECEIVER_PORT=8787", arguments)


if __name__ == "__main__":
    unittest.main()
