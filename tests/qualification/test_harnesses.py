import unittest

from cansniff.qualification.soak import run_soak
from cansniff.qualification.stress import run_stress


class OfflineHarnessTests(unittest.TestCase):
    def test_small_stress_run_preserves_bounded_retention(self):
        result = run_stress(250, retained=100)
        self.assertEqual(result["processed"], 250)
        self.assertEqual(result["retained"], 100)
        self.assertGreater(result["frames_per_second"], 0)

    def test_small_lifecycle_soak_completes_cleanup(self):
        result = run_soak(3, 8)
        self.assertEqual(result["cycles_completed"], 3)
        self.assertEqual(result["cleanup"], "completed")


if __name__ == "__main__":
    unittest.main()
