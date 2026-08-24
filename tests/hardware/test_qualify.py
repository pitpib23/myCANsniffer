import unittest
from unittest.mock import patch

from cansniff.qualification.hardware import EXECUTE_CONFIRMATION, qualify
from cansniff.qualification.model import (
    QualificationLevel, QualificationStatus,
)


class HardwareHarnessTests(unittest.TestCase):
    def test_default_is_dry_run_and_never_opens(self):
        with patch("cansniff.qualification.hardware.LiveSource.open") as opened:
            record = qualify({"interface": "virtual", "channel": "q"}, 0)
        opened.assert_not_called()
        self.assertEqual(record.status, QualificationStatus.SKIPPED)
        self.assertEqual(record.level, QualificationLevel.UNTESTED)

    def test_execute_requires_exact_confirmation(self):
        with patch("cansniff.qualification.hardware.LiveSource.open") as opened:
            record = qualify({"interface": "virtual", "channel": "q"}, 0,
                             execute=True, confirmation="yes")
        opened.assert_not_called()
        self.assertEqual(record.status, QualificationStatus.REFUSED)

    def test_virtual_execution_is_software_not_hardware(self):
        with patch("cansniff.qualification.hardware.LiveSource.open") as opened, \
             patch("cansniff.qualification.hardware.LiveSource.close") as closed:
            record = qualify({"interface": "virtual", "channel": "q"}, 0,
                             execute=True, confirmation=EXECUTE_CONFIRMATION)
        opened.assert_called_once(); closed.assert_called_once()
        self.assertEqual(record.level, QualificationLevel.SOFTWARE_TESTED)

    def test_physical_receive_error_still_closes(self):
        # qualification_plan()/open() are mocked below, but qualify() still
        # calls the real _preflight() first to build the recorded plan --
        # verify listen-only so this stays hermetic (no real `ip` call).
        with patch("cansniff.sources.live._socketcan_is_listen_only",
                   return_value=True), \
             patch("cansniff.qualification.hardware.LiveSource.open"), \
             patch("cansniff.qualification.hardware.LiveSource.receive",
                   side_effect=RuntimeError("receive failed")), \
             patch("cansniff.qualification.hardware.LiveSource.close") as closed:
            record = qualify({"interface": "socketcan", "channel": "can0"}, 0.01,
                             execute=True, confirmation=EXECUTE_CONFIRMATION)
        closed.assert_called_once()
        self.assertEqual(record.status, QualificationStatus.FAIL)
        self.assertIn("receive failed", record.limitations[-1])


if __name__ == "__main__":
    unittest.main()
