import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONFIGURE = ROOT / "scripts" / "configure"


def parse_config(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = shlex.split(value)[0]
    return values


class ConfigureTests(unittest.TestCase):
    def test_noninteractive_upgrade_preserves_values_and_backfills_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            config_dir.mkdir(mode=0o700)
            config_file = config_dir / "config.env"
            config_file.write_text(
                'IPHONE_MSG_TARGET="target"\n'
                'IPHONE_PUBLIC_URL="https://iphone.example"\n'
                'IPHONE_RECEIVER_TOKEN="existing-private-token-with-32-chars"\n'
            )
            config_file.chmod(0o600)
            result = subprocess.run(
                [sys.executable, str(CONFIGURE), "--non-interactive"],
                env={**os.environ, "IOS_ASSISTANT_CONFIG_DIR": str(config_dir)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = parse_config(config_file)
            self.assertEqual(values["IPHONE_MSG_TARGET"], "target")
            self.assertEqual(values["IPHONE_PUBLIC_URL"], "https://iphone.example")
            self.assertEqual(
                values["IPHONE_RECEIVER_TOKEN"], "existing-private-token-with-32-chars"
            )
            self.assertRegex(values["IPHONE_COMMAND_SECRET"], r"^[0-9a-f]{64}$")
            self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
            self.assertNotIn(values["IPHONE_COMMAND_SECRET"], result.stdout)

    def test_noninteractive_upgrade_refuses_insecure_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            config_dir.mkdir(mode=0o700)
            config_file = config_dir / "config.env"
            config_file.write_text('IPHONE_MSG_TARGET="target"\n')
            config_file.chmod(0o644)
            result = subprocess.run(
                [sys.executable, str(CONFIGURE), "--non-interactive"],
                env={**os.environ, "IOS_ASSISTANT_CONFIG_DIR": str(config_dir)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to read insecure configuration", result.stderr)


if __name__ == "__main__":
    unittest.main()
