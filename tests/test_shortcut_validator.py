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
    def run_validator(self, mutate):
        with TEMPLATE.open("rb") as source:
            actions = plistlib.load(source)
        command_conditions = [
            action["WFWorkflowActionParameters"]
            for action in actions
            if action.get("WFWorkflowActionIdentifier") == "is.workflow.actions.conditional"
            and str(action.get("WFWorkflowActionParameters", {}).get("WFConditionalActionString", "")).startswith(
                "hola "
            )
        ]
        mutate(command_conditions)

        with tempfile.NamedTemporaryFile(suffix=".plist") as mutated:
            plistlib.dump(actions, mutated)
            mutated.flush()
            result = subprocess.run(
                [sys.executable, str(VALIDATOR), mutated.name],
                capture_output=True,
                text=True,
                check=False,
            )
        return result

    def test_rejects_condition_mode_that_inverts_on_iphone(self):
        result = self.run_validator(lambda conditions: conditions[0].__setitem__("WFCondition", 4))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must use begins-with mode 8", result.stderr)

    def test_rejects_duplicate_or_overlapping_command_prefix(self):
        def duplicate(conditions):
            conditions[1]["WFConditionalActionString"] = conditions[0]["WFConditionalActionString"]

        result = self.run_validator(duplicate)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact command set once each", result.stderr)


if __name__ == "__main__":
    unittest.main()
