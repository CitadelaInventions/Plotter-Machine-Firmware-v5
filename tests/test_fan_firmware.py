"""Exercise the same fan counter compiled into the Uno firmware."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class FanFirmwareTests(unittest.TestCase):
    def test_cycle_boundaries_manual_override_and_long_jobs(self):
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        source = Path(__file__).with_name("fan_cycle_test.cpp")
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "fan-cycle-test"
            subprocess.run([compiler, "-std=c++11", str(source), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
