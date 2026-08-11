import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "shortcut" / "actions.template.plist"
VALIDATOR = ROOT / "scripts" / "validate-shortcut.py"


class ShortcutValidatorTests(unittest.TestCase):
    def test_rejects_condition_mode_that_inverts_on_iphone(self):
        with TEMPLATE.open("rb") as source:
            actions = plistlib.load(source)
        command_condition = next(
            action["WFWorkflowActionParameters"]
            for action in actions
            if action.get("WFWorkflowActionIdentifier") == "is.workflow.actions.conditional"
            and str(action.get("WFWorkflowActionParameters", {}).get("WFConditionalActionString", "")).startswith(
                "hola "
            )
        )
        command_condition["WFCondition"] = 4

        with tempfile.NamedTemporaryFile(suffix=".plist") as mutated:
            plistlib.dump(actions, mutated)
            mutated.flush()
            result = subprocess.run(
                [sys.executable, str(VALIDATOR), mutated.name],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must use begins-with mode 8", result.stderr)


if __name__ == "__main__":
    unittest.main()
