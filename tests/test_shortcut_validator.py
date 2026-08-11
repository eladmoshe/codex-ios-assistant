import plistlib
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "shortcut" / "actions.template.plist"
VALIDATOR = ROOT / "scripts" / "validate-shortcut.py"
AUGMENT_RECEIPTS = ROOT / "scripts" / "augment-receipts.py"


class ShortcutValidatorTests(unittest.TestCase):
    def run_validator(self, mutate):
        with TEMPLATE.open("rb") as source:
            actions = plistlib.load(source)
        command_conditions = [
            action["WFWorkflowActionParameters"]
            for action in actions
            if action.get("WFWorkflowActionIdentifier") == "is.workflow.actions.conditional"
            and str(action.get("WFWorkflowActionParameters", {}).get("WFConditionalActionString", "")).startswith(
                "__IOS_ASSISTANT_COMMAND_SECRET__ hola "
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

    def test_rejects_raw_message_object_condition_input(self):
        def restore_raw_input(conditions):
            conditions[0]["WFInput"] = {
                "Value": {"Type": "ExtensionInput"},
                "WFSerializationType": "WFTextTokenAttachment",
            }

        result = self.run_validator(restore_raw_input)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("raw Message automation input", result.stderr)

    def test_rejects_generic_message_input_without_content_extraction(self):
        with TEMPLATE.open("rb") as source:
            actions = plistlib.load(source)
        actions[0]["WFWorkflowActionParameters"]["WFInput"]["Value"].pop(
            "Aggrandizements"
        )
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
        self.assertIn("extract the Message Content property", result.stderr)

    def test_augment_repairs_message_content_input_and_rewrites_later_consumers(self):
        with TEMPLATE.open("rb") as source:
            actions = plistlib.load(source)
        module = runpy.run_path(str(AUGMENT_RECEIPTS), run_name="augment_receipts_test")
        actions[0]["WFWorkflowActionParameters"]["WFInput"]["Value"].pop(
            "Aggrandizements"
        )
        actions[1]["WFWorkflowActionParameters"]["WFInput"] = {
            "Value": {"Type": "ExtensionInput"},
            "WFSerializationType": "WFTextTokenAttachment",
        }

        changed = module["harden_command_input"](actions)

        self.assertTrue(changed)
        first_value = actions[0]["WFWorkflowActionParameters"]["WFInput"]["Value"]
        self.assertEqual(
            first_value["Aggrandizements"],
            [{"PropertyName": "Content", "Type": "WFPropertyVariableAggrandizement"}],
        )

        def has_extension_input(value):
            if isinstance(value, dict):
                return value.get("Type") == "ExtensionInput" or any(
                    has_extension_input(child) for child in value.values()
                )
            if isinstance(value, list):
                return any(has_extension_input(child) for child in value)
            return False

        self.assertFalse(
            any(has_extension_input(action) for action in actions[1:])
        )

    def test_rejects_timer_parser_that_can_read_secret_digits(self):
        with TEMPLATE.open("rb") as source:
            actions = plistlib.load(source)
        timer_parser = next(
            action["WFWorkflowActionParameters"]
            for action in actions
            if action.get("WFWorkflowActionIdentifier") == "is.workflow.actions.text.match"
            and action.get("WFWorkflowActionParameters", {}).get("UUID")
            == "E9A5A5F0-0001-4CCC-8CCC-000000000001"
        )
        timer_parser["WFMatchTextPattern"] = "[0-9]+"
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
        self.assertIn("timer duration parser", result.stderr)


if __name__ == "__main__":
    unittest.main()
