from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class MotionFirmwareTests(unittest.TestCase):
    def test_native_motion_and_actual_timer_queue_code(self):
        compiler = shutil.which("c++") or shutil.which("clang++")
        if compiler is None:
            self.skipTest("A C++ compiler is needed for native motion tests.")
        tests = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "motion-tests"
            subprocess.run([compiler, "-std=c++11", "-O2", "-I", str(tests / "native_motion_stubs"),
                            str(tests / "motion_scheduler_test.cpp"), "-o", str(binary)],
                           check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
