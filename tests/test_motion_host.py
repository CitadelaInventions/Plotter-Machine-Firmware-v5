"""Focused host-side checks for protocol-v5 motion settings; no hardware access."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from plotter_app import PlotterAPI
from plotter_core import (MAX_FEED, MICROSTEP_CHOICES, build_job, feed_for_move,
                          ramp_command, validate_machine_settings)


def settings(**overrides) -> dict:
    result = dict(canvas_mode="machine", orientation="landscape", bed_width=60,
                  bed_height=100, spacing=1, steps_x=800, steps_y=800, feed=1200,
                  white_cutoff=245, palette_mode="mono", mono_pen="BLACK", mono_threshold=225,
                  servo_up=45, servo_red=95, servo_blue=95, servo_green=95, servo_black=95,
                  rpm_x=600, rpm_y=75, full_steps_x=200, full_steps_y=200,
                  microsteps_x=32, microsteps_y=32)
    for color in ("red", "blue", "green", "black"):
        result[f"offset_{color}_x"] = result[f"offset_{color}_y"] = 0
    result.update(overrides)
    return result


def status(proto=5, **overrides) -> str:
    values = dict(proto=proto, x=0, y=0, y_pins="A0,A1,A2", max_feed=24000,
                  accel=1, queue=32, idle_ms=5000, fan_pin=9, enabled=0, pen="UP")
    values.update(overrides)
    return "STATUS " + " ".join(f"{key}={value}" for key, value in values.items())


class StatusOnlyPort:
    """Refuse movement while testing the actual connection handshake."""
    def __init__(self, response):
        self.lines = deque([(response + "\n").encode("ascii")])
        self.commands = []
        self.is_open = True

    def write(self, raw):
        command = raw.decode("ascii").strip()
        if command != "STATUS":
            raise AssertionError("Connection attempted a machine action: " + command)
        self.commands.append(command)
        return len(raw)

    def flush(self):
        pass

    def readline(self):
        return self.lines.popleft() if self.lines else b""

    def close(self):
        self.is_open = False


class MotionHostTests(unittest.TestCase):
    def test_ramp_defaults_axis_conversion_and_legacy_feed(self):
        machine = validate_machine_settings(settings())
        self.assertEqual((machine["ramp_start_rpm"], machine["ramp_ms"], machine["corner_percent"]),
                         (10, 300, 25))
        self.assertEqual(ramp_command(machine), "RAMP 1067 300 25")
        # Different driver resolutions must use the slower starting pulse rate.
        config = settings(microsteps_x=128, microsteps_y=5, ramp_start_rpm=12,
                          ramp_ms=850, corner_percent=40)
        self.assertEqual(ramp_command(validate_machine_settings(config)), "RAMP 200 850 40")
        legacy = settings()
        for key in ("rpm_x", "rpm_y", "full_steps_x", "full_steps_y", "microsteps_x", "microsteps_y"):
            legacy.pop(key)
        for feed, expected in ((1200, 800), (275, 275)):
            legacy["feed"] = feed
            self.assertEqual(ramp_command(validate_machine_settings(legacy)), f"RAMP {expected} 300 25")

    def test_rotated_canvas_moves_obey_physical_axis_rpm_and_firmware_cap(self):
        config = settings()
        image = Image.new("RGB", (100, 60), "white")
        draw = ImageDraw.Draw(image)
        draw.line((10, 8, 35, 8), fill="black")  # View X becomes physical Y.
        draw.line((70, 25, 70, 47), fill="black")  # View Y becomes physical X.
        job = build_job(image, config, full_page=True)
        self.assertEqual(MAX_FEED, 24000)
        self.assertLess(job.commands.index("RAMP 1067 300 25"), job.commands.index("ENABLE ON"))
        x = y = 0
        down = False
        physical_rates = set()
        for command in job.commands:
            parts = command.split()
            if parts[0] == "PEN":
                down = parts[1] != "UP"
            if parts[0] != "MOVE":
                continue
            nx, ny, feed = map(int, parts[1:])
            dx, dy = nx - x, ny - y
            major = max(abs(dx), abs(dy))
            self.assertLessEqual(feed, 24000)
            if major:
                self.assertLessEqual(feed * abs(dx) / major, 64000)
                self.assertLessEqual(feed * abs(dy) / major, 8000)
            self.assertTrue(0 <= nx <= 60 * 800 and 0 <= ny <= 100 * 800)
            if down and dx and not dy:
                physical_rates.add(("X", feed))
            elif down and dy and not dx:
                physical_rates.add(("Y", feed))
            x, y = nx, ny
        self.assertEqual(physical_rates, {("X", 24000), ("Y", 8000)})

    def test_fine_128_microsteps_and_large_step_coordinates(self):
        config = settings(bed_width=1000, bed_height=1000, steps_x=20000, steps_y=20000,
                          microsteps_x=128, microsteps_y=128, rpm_x=600, rpm_y=600)
        machine = validate_machine_settings(config)
        self.assertEqual(feed_for_move(400000, 0, machine), 24000)
        self.assertEqual(ramp_command(machine), "RAMP 4267 300 25")
        image = Image.new("RGB", (4, 4), "white")
        image.putpixel((3, 3), (0, 0, 0))
        job = build_job(image, config, full_page=True)
        self.assertEqual(job.commands[0], "LIMIT 20000000 20000000")
        for command in job.commands:
            if command.startswith("MOVE "):
                x, y, _ = map(int, command.split()[1:])
                self.assertTrue(0 <= x <= 20000000 and 0 <= y <= 20000000)

    def test_supported_photographed_dm556_microsteps(self):
        expected = {2, 4, 5, 8, 10, 16, 20, 25, 32, 40, 50, 64, 100, 125, 128}
        self.assertEqual(set(MICROSTEP_CHOICES), expected)
        for choice in expected:
            with self.subTest(microsteps=choice):
                machine = validate_machine_settings(settings(microsteps_x=choice, microsteps_y=choice))
                self.assertEqual(machine["microsteps_x"], choice)

    def test_invalid_ramp_motor_and_calibration_settings_are_rejected(self):
        invalid = {"ramp_start_rpm": (0, 61, float("nan")), "ramp_ms": (49, 3001),
                   "corner_percent": (-1, 101), "rpm_x": (0, 601),
                   "microsteps_y": (0, 1, 3, 6, 12, 32.5, 256, 512, 513), "steps_x": (0, 20001)}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                    validate_machine_settings(settings(**{key: value}))
        # Valid range endpoints stay usable.
        validate_machine_settings(settings(ramp_start_rpm=1, ramp_ms=50, corner_percent=0,
                                           rpm_x=1, steps_x=1))
        validate_machine_settings(settings(ramp_start_rpm=60, ramp_ms=3000, corner_percent=100,
                                           rpm_x=600, steps_x=20000))

    def test_calibration_programs_ramp_before_enable_and_jog(self):
        config = settings(bed_width=1000, bed_height=1000, steps_x=20000, steps_y=20000,
                          microsteps_x=128, microsteps_y=128, rpm_x=600, rpm_y=600)
        app = PlotterAPI()
        app.port = Mock()
        app._set(connected=True)
        commands = []
        with patch.object(app, "_execute", side_effect=commands.append), \
             patch.object(app, "_write"), patch.object(app, "_read_line", return_value=status()):
            app.calibrate("y+", config, 20)
        self.assertEqual(commands, ["PEN UP", "LIMIT 20000000 20000000", "RAMP 4267 300 25",
                                    "ENABLE ON", "JOG 0 400000 24000", "ENABLE OFF"])
        app.port.reset_input_buffer.assert_called_once()

    def test_smaller_limits_explain_rezero_without_zeroing_or_moving(self):
        app = PlotterAPI()
        app.port = Mock()
        position = {"x": 92164, "y": 47920}
        app._set(connected=True, position=position.copy())
        config = settings(bed_width=250, bed_height=500, steps_x=200, steps_y=200,
                          microsteps_x=2, microsteps_y=2)
        with patch.object(app, "_write") as write, \
             patch.object(app, "_read_line", side_effect=["OK", "ERR invalid_limit"]):
            with self.assertRaisesRegex(RuntimeError, "physical machine origin.*Zero current position"):
                app.calibrate("x+", config, 5)
        self.assertEqual([item.args[0] for item in write.call_args_list],
                         ["PEN UP", "LIMIT 50000 100000", "STOP"])
        self.assertEqual(app.get_state()["position"], position)
        self.assertFalse(app.get_state()["enabled"])

    def test_export_preserves_generated_ramp_and_protocol_v5(self):
        config = settings(ramp_start_rpm=15, ramp_ms=700, corner_percent=35)
        image = Image.new("RGB", (4, 4), "white")
        image.putpixel((2, 2), (0, 0, 0))
        app = PlotterAPI()
        app.job = build_job(image, config, full_page=True)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "ramped.txt"
            dialog = Mock()
            dialog.create_file_dialog.return_value = str(target)
            with patch("plotter_app.webview.windows", [dialog]):
                self.assertEqual(app.save_job(), str(target))
            text = target.read_text()
        self.assertIn("protocol v5", text)
        lines = text.splitlines()
        self.assertIn("RAMP 1600 700 35", lines)
        self.assertLess(lines.index("RAMP 1600 700 35"), lines.index("ENABLE ON"))
        self.assertEqual(lines[1], "JOB BEGIN")
        self.assertEqual(lines[-1], "JOB END")

    def test_protocol_v4_is_rejected_without_movement(self):
        app = PlotterAPI()
        port = StatusOnlyPort(status(proto=4, max_feed=16000, accel=0))
        with patch("plotter_app.serial.Serial", return_value=port):
            with self.assertRaisesRegex(RuntimeError, "eased-motion firmware v5"):
                app.connect("test-port")
        self.assertEqual(port.commands, ["STATUS"])
        self.assertFalse(port.is_open)
        self.assertIsNone(app.port)
        self.assertFalse(app.get_state()["connected"])

    def test_protocol_v5_requires_all_motion_capabilities_before_connecting(self):
        for missing in ({"accel": 0}, {"max_feed": 16000}, {"queue": 8},
                        {"idle_ms": 0}, {"fan_pin": 10}):
            with self.subTest(capability=missing):
                app = PlotterAPI()
                port = StatusOnlyPort(status(**missing))
                with patch("plotter_app.serial.Serial", return_value=port), self.assertRaises(RuntimeError):
                    app.connect("test-port")
                self.assertEqual(port.commands, ["STATUS"])
                self.assertFalse(port.is_open)
        app = PlotterAPI()
        port = StatusOnlyPort(status())
        with patch("plotter_app.serial.Serial", return_value=port):
            self.assertTrue(app.connect("test-port")["connected"])
        self.assertEqual(port.commands, ["STATUS"])


if __name__ == "__main__":
    unittest.main()
