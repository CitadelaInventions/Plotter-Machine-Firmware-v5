from __future__ import annotations

from pathlib import Path
from io import BytesIO
import base64
import errno
import termios
import threading
import tempfile
import unittest
from collections import deque
from unittest.mock import call, patch

from PIL import Image, ImageDraw

from plotter_core import PALETTE, PlotJob, _classify, _job_metrics, build_job, compose_page, feed_for_move, validate_machine_settings, validate_settings
from plotter_app import PlotterAPI
from plotter_paths import order_for_travel


def settings() -> dict:
    result = {
        "bed_width": 210, "bed_height": 297,
        "draw_width": 100, "draw_height": 100,
        "origin_x": 20, "origin_y": 20,
        "spacing": 2, "steps_x": 80, "steps_y": 80,
        "feed": 1200, "white_cutoff": 245,
        "palette_mode": "four", "mono_pen": "BLACK", "mono_threshold": 210,
        "threshold_red": 20, "threshold_blue": 20,
        "threshold_green": 20, "threshold_black": 15,
        "servo_up": 45, "servo_red": 95, "servo_blue": 95,
        "servo_green": 95, "servo_black": 95,
    }
    for color in ("red", "blue", "green", "black"):
        for axis in ("x", "y"):
            result[f"offset_{color}_{axis}"] = 0
    return result


def a4_settings() -> dict:
    result = settings()
    result.update(bed_width=330, bed_height=330, orientation="portrait")
    return result


class PlotterCoreTests(unittest.TestCase):
    def test_faint_monochrome_detail_survives_composition(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "faint.png"
            Image.new("RGB", (1, 1), (220, 220, 220)).save(path)
            config = settings()
            config.update(canvas_mode="machine", orientation="portrait", bed_width=30,
                          bed_height=30, spacing=1, palette_mode="mono", mono_threshold=221)
            layer = {"id":"faint", "x_mm":10, "y_mm":10, "width_mm":1, "height_mm":1,
                     "alpha":100, "color_alpha":dict.fromkeys(PALETTE,100)}
            page = compose_page([layer], {"faint":path}, config)
            self.assertEqual(page.getpixel((10,10)), (220,220,220))
            job = build_job(page, config, full_page=True)
            self.assertEqual(job.counts["BLACK"], 1)
            self.assertTrue(job.plot_ready)

    def test_time_estimate_includes_moves_and_servo_waits(self) -> None:
        config = validate_settings(settings())
        config.update(pen_up_ms=100, pen_down_ms=80)
        metrics = _job_metrics(["SERVO 45 95 95 95 95", "MOVE 80 0 80", "PEN RED",
                                "MOVE 80 80 80", "PEN UP", "ENABLE OFF"], config)
        self.assertEqual(metrics["draw_mm"], 1)
        self.assertEqual(metrics["travel_mm"], 1)
        self.assertEqual(metrics["estimated_seconds"], 2.3)
        self.assertEqual(metrics["moves"], 2)

    def test_machine_canvas_uses_full_travel_and_ignores_old_page_origin(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "black.png"
            Image.new("RGB", (10, 10), "black").save(path)
            config = settings()
            config.update(canvas_mode="machine", orientation="portrait", bed_width=400,
                          bed_height=350, origin_x=200, origin_y=100, spacing=1)
            layer = {"id": "black", "x_mm": 350, "y_mm": 310,
                     "width_mm": 20, "height_mm": 10, "alpha": 100,
                     "color_alpha": dict.fromkeys(PALETTE, 100)}
            page = compose_page([layer], {"black": path}, config)
            self.assertEqual(page.size, (400, 350))
            self.assertEqual(page.getpixel((355, 315)), (0, 0, 0))
            job = build_job(page, config, full_page=True)
            self.assertTrue(job.plot_ready)
            self.assertEqual((job.width_mm, job.height_mm), (400, 350))
            self.assertEqual(job.commands[0], "LIMIT 32000 28000")
            self.assertTrue(all(350 <= segment.x1 <= 370 and 310 <= segment.y1 <= 320
                                for segment in job.segments))

    def test_rotated_machine_canvas_preserves_physical_image_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "asymmetric.png"
            image = Image.new("RGB", (10, 6), "white")
            ImageDraw.Draw(image).point([(0, 0), (7, 4)], fill="black")
            image.save(path)
            config = settings()
            config.update(canvas_mode="machine", orientation="portrait", bed_width=80,
                          bed_height=50, spacing=1)
            layer = {"id": "shape", "x_mm": 20, "y_mm": 10,
                     "width_mm": 10, "height_mm": 6, "alpha": 100,
                     "color_alpha": dict.fromkeys(PALETTE, 100)}
            normal_page = compose_page([layer], {"shape": path}, config)
            normal = build_job(normal_page, config, full_page=True)
            layer.update(x_mm=50-10-6, y_mm=20, width_mm=6, height_mm=10, rotation=90)
            config["orientation"] = "landscape"
            page = compose_page([layer], {"shape": path}, config)
            self.assertEqual(page.size, (50, 80))
            self.assertEqual(page.transpose(Image.Transpose.ROTATE_90).tobytes(), normal_page.tobytes())
            rotated = build_job(page, config, full_page=True)
            coordinates = lambda job: {tuple(command.split()[1:3]) for command in job.commands
                                       if command.startswith("MOVE ")}
            self.assertEqual(coordinates(normal), coordinates(rotated))
            self.assertTrue(rotated.plot_ready)
            config["offset_black_x"] = 1
            offset_job = build_job(page, config, full_page=True)
            self.assertEqual(coordinates(offset_job),
                             {(str(int(x)-80), y) for x, y in coordinates(rotated)})

    def test_full_canvas_accepts_ultra_resolution_and_maximum_machine_size(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dot.png"
            Image.new("RGB", (1, 1), "black").save(path)
            layer = {"id": "dot", "x_mm": 5, "y_mm": 5, "width_mm": 1,
                     "height_mm": 1, "alpha": 100, "color_alpha": dict.fromkeys(PALETTE, 100)}
            config = settings()
            config.update(canvas_mode="machine", orientation="portrait", bed_width=300,
                          bed_height=300, spacing=0.35)
            self.assertEqual(compose_page([layer], {"dot": path}, config).size, (857, 857))
            config.update(bed_width=250, bed_height=500)
            self.assertEqual(compose_page([layer], {"dot": path}, config).size, (714, 1429))
            config.update(bed_width=1000, bed_height=1000, spacing=1)
            self.assertEqual(compose_page([layer], {"dot": path}, config).size, (1000, 1000))

    def test_nearest_path_order_reduces_pen_up_distance(self) -> None:
        paths = [[(100, 0), (101, 0)], [(3, 0), (4, 0)], [(8, 0), (7, 0)]]
        ordered = order_for_travel(paths)
        self.assertEqual(ordered[0], [(3, 0), (4, 0)])
        self.assertEqual(ordered[1], [(7, 0), (8, 0)])
        self.assertEqual(ordered[2], [(100, 0), (101, 0)])

    def test_rpm_limits_become_coordinated_step_rates(self) -> None:
        config = settings()
        config.update(rpm_x=60, rpm_y=30, full_steps_x=200, full_steps_y=200,
                      microsteps_x=32, microsteps_y=32)
        speed = validate_machine_settings(config)
        self.assertEqual(feed_for_move(800, 0, speed), 6400)
        self.assertEqual(feed_for_move(0, 800, speed), 3200)
        self.assertEqual(feed_for_move(800, 800, speed), 3200)
        speed["rpm_x"] = 120
        self.assertEqual(feed_for_move(800, 0, speed), 12800)
        speed["rpm_x"] = 200
        self.assertEqual(feed_for_move(800, 0, speed), 21333)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "black.png"
            Image.new("RGB", (10, 10), "black").save(path)
            config["rpm_x"] = 60
            job = build_job(path, config)
            self.assertTrue(any(command.startswith("MOVE ") and command.endswith(" 6400")
                                for command in job.commands))
        config["microsteps_y"] = 32.5
        with self.assertRaisesRegex(ValueError, "whole number"):
            validate_machine_settings(config)

    def test_oversized_image_is_cropped_to_a4_paper(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "red.png"
            Image.new("RGB", (20, 20), "red").save(path)
            layer = {"id": "red", "x_mm": -30, "y_mm": -40,
                     "width_mm": 300, "height_mm": 400, "alpha": 100,
                     "color_alpha": dict.fromkeys(("RED", "BLUE", "GREEN", "BLACK"), 100)}
            config = a4_settings()
            config.update(spacing=1, bed_width=210, bed_height=297, origin_x=20, origin_y=20)
            page = compose_page([layer], {"red": path}, config)
            self.assertEqual(page.size, (210, 297))
            self.assertEqual(page.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(page.getpixel((209, 296)), (255, 0, 0))

    def test_off_page_left_side_crops_source_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "halves.png"
            image = Image.new("RGB", (100, 10), "red")
            ImageDraw.Draw(image).rectangle((50, 0, 99, 9), fill="blue")
            image.save(path)
            layer = {"id": "halves", "x_mm": -50, "y_mm": 10,
                     "width_mm": 100, "height_mm": 10, "alpha": 100,
                     "color_alpha": dict.fromkeys(("RED", "BLUE", "GREEN", "BLACK"), 100)}
            config = a4_settings()
            config["spacing"] = 1
            page = compose_page([layer], {"halves": path}, config)
            self.assertEqual(page.getpixel((10, 15)), (0, 0, 255))
            self.assertEqual(page.getpixel((60, 15)), (255, 255, 255))

    def test_a4_composition_and_orientation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            red = Path(folder) / "red.png"
            blue = Path(folder) / "blue.png"
            Image.new("RGB", (20, 20), "red").save(red)
            Image.new("RGB", (20, 20), "blue").save(blue)
            sources = {"red": red, "blue": blue}
            layers = [
                {"id": "red", "x_mm": 10, "y_mm": 10, "width_mm": 30, "height_mm": 30,
                 "alpha": 100, "color_alpha": dict.fromkeys(("RED", "BLUE", "GREEN", "BLACK"), 100)},
                {"id": "blue", "x_mm": 50, "y_mm": 10, "width_mm": 30, "height_mm": 30,
                 "alpha": 100, "color_alpha": dict.fromkeys(("RED", "BLUE", "GREEN", "BLACK"), 100)},
            ]
            config = a4_settings()
            config["spacing"] = 1
            portrait = compose_page(layers, sources, config)
            self.assertEqual(portrait.size, (210, 297))
            self.assertEqual(portrait.getpixel((20, 20)), (255, 0, 0))
            self.assertEqual(portrait.getpixel((60, 20)), (0, 0, 255))
            self.assertEqual(portrait.getpixel((100, 100)), (255, 255, 255))
            job = build_job(portrait, config, full_page=True)
            self.assertEqual((job.width_mm, job.height_mm), (210, 297))
            self.assertEqual(job.commands[0], "LIMIT 26400 26400")
            self.assertIn("MOVE 2440 2440 1200", job.commands)
            self.assertGreater(job.counts["RED"], 0)
            self.assertGreater(job.counts["BLUE"], 0)
            config["orientation"] = "landscape"
            self.assertEqual(compose_page(layers, sources, config).size, (297, 210))

    def test_per_image_color_alpha_and_layer_order(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            red = Path(folder) / "red.png"
            blue = Path(folder) / "blue.png"
            Image.new("RGB", (8, 8), "red").save(red)
            Image.new("RGB", (8, 8), "blue").save(blue)
            alphas = lambda: dict.fromkeys(("RED", "BLUE", "GREEN", "BLACK"), 100)
            lower = {"id": "red", "x_mm": 10, "y_mm": 10, "width_mm": 20, "height_mm": 20,
                     "alpha": 100, "color_alpha": alphas()}
            upper = {"id": "blue", "x_mm": 10, "y_mm": 10, "width_mm": 20, "height_mm": 20,
                     "alpha": 100, "color_alpha": alphas()}
            config = a4_settings()
            config["spacing"] = 1
            sources = {"red": red, "blue": blue}
            self.assertEqual(compose_page([lower, upper], sources, config).getpixel((20, 20)), (0, 0, 255))
            upper["color_alpha"]["BLUE"] = 0
            self.assertEqual(compose_page([lower, upper], sources, config).getpixel((20, 20)), (255, 0, 0))
            upper["color_alpha"]["BLUE"] = 100
            upper["alpha"] = 50
            mixed = compose_page([lower, upper], sources, config).getpixel((20, 20))
            self.assertGreater(mixed[0], 0)
            self.assertGreater(mixed[2], 0)
            self.assertEqual(compose_page([upper, lower], sources, config).getpixel((20, 20)), (255, 0, 0))

    def test_finer_spacing_increases_a4_instruction_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "black.png"
            Image.new("RGB", (10, 10), "black").save(path)
            layer = {"id": "black", "x_mm": 10, "y_mm": 10, "width_mm": 15, "height_mm": 15,
                     "alpha": 100, "color_alpha": dict.fromkeys(("RED", "BLUE", "GREEN", "BLACK"), 100)}
            config = a4_settings()
            config["spacing"] = 2
            coarse = build_job(compose_page([layer], {"black": path}, config), config, full_page=True)
            config["spacing"] = 0.5
            fine = build_job(compose_page([layer], {"black": path}, config), config, full_page=True)
            self.assertGreater(fine.cells[0] * fine.cells[1], coarse.cells[0] * coarse.cells[1])
            self.assertGreater(len(fine.commands), len(coarse.commands))

    def test_solid_area_is_one_continuous_contour_path(self) -> None:
        image = Image.new("RGB", (20, 20), "black")
        job = build_job(image, settings(), full_page=True)
        self.assertEqual(job.counts["BLACK"], 1)
        self.assertEqual(job.commands.count("PEN BLACK"), 1)
        self.assertLess(len(job.commands), 20 * 4)
        self.assertTrue(any(segment.x1 == segment.x2 and segment.y1 != segment.y2
                            for segment in job.segments))

    def test_disconnected_areas_keep_separate_pen_strokes(self) -> None:
        image = Image.new("RGB", (24, 12), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((2, 2, 8, 9), fill="black")
        draw.rectangle((15, 2, 21, 9), fill="black")
        job = build_job(image, settings(), full_page=True)
        self.assertEqual(job.counts["BLACK"], 2)
        self.assertEqual(job.commands.count("PEN BLACK"), 2)
        self.assertEqual(job.commands.count("PEN UP"), 2)  # after each stroke

    def test_contour_paths_leave_an_interior_hole_blank(self) -> None:
        image = Image.new("RGB", (15, 15), "black")
        ImageDraw.Draw(image).rectangle((5, 5, 9, 9), fill="white")
        job = build_job(image, settings(), full_page=True)
        for segment in job.segments:
            for step in range(101):
                fraction = step / 100
                x = (segment.x1 + (segment.x2 - segment.x1) * fraction) * 15 / 100
                y = (segment.y1 + (segment.y2 - segment.y1) * fraction) * 15 / 100
                self.assertFalse(5 <= x < 10 and 5 <= y < 10)

    def test_adjustable_color_and_black_thresholds(self) -> None:
        config = validate_settings(settings())
        self.assertEqual(PALETTE["BLUE"], (99, 149, 158))
        self.assertEqual(_classify((99, 149, 158), config), "BLUE")
        self.assertEqual(_classify((180, 70, 70), config), "RED")
        config["threshold_red"] = 80
        config["threshold_black"] = 100
        self.assertIsNone(_classify((180, 70, 70), config))
        config["threshold_black"] = 15
        self.assertEqual(_classify((180, 70, 70), config), "BLACK")

    def test_monochrome_uses_one_pen_and_brightness_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "gray.png"
            image = Image.new("RGB", (40, 20), (220, 220, 220))
            ImageDraw.Draw(image).rectangle((0, 0, 19, 19), fill=(80, 80, 80))
            image.save(path)
            config = settings()
            config.update(palette_mode="mono", mono_pen="BLUE", mono_threshold=150)
            dark_only = build_job(path, config)
            self.assertEqual(set(segment.color for segment in dark_only.segments), {"BLUE"})
            self.assertEqual(dark_only.counts["RED"], 0)
            self.assertIn("PEN BLUE", dark_only.commands)
            config["mono_threshold"] = 230
            more_ink = build_job(path, config)
            length = lambda job: sum(abs(s.x2 - s.x1) for s in job.segments)
            self.assertGreater(length(more_ink), length(dark_only))

    def test_four_colors_and_bounded_steps(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            image = Image.new("RGB", (80, 40), "white")
            draw = ImageDraw.Draw(image)
            for index, color in enumerate(("red", "blue", "green", "black")):
                draw.rectangle((index * 20, 5, index * 20 + 14, 34), fill=color)
            path = Path(folder) / "four.png"
            image.save(path)
            job = build_job(path, settings())
            self.assertTrue(all(job.counts.values()))
            self.assertEqual(job.commands[0], "LIMIT 16800 23760")
            self.assertEqual(job.commands[-2:], ["PEN UP", "ENABLE OFF"])
            for command in job.commands:
                if command.startswith("MOVE "):
                    _, x, y, feed = command.split()
                    self.assertLessEqual(int(x), 16800)
                    self.assertLessEqual(int(y), 23760)
                    self.assertEqual(feed, "1200")

    def test_pen_offset_outside_bed_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "red.png"
            Image.new("RGB", (10, 10), "red").save(path)
            config = settings()
            config["offset_red_x"] = -100
            config["draw_width"] = 100
            config["origin_x"] = 20
            config["bed_width"] = 210
            with self.assertRaisesRegex(ValueError, "exceed machine travel"):
                build_job(path, config)

    def test_transparent_white_image_has_no_strokes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty.png"
            Image.new("RGBA", (20, 20), (255, 255, 255, 0)).save(path)
            with self.assertRaisesRegex(ValueError, "No drawable color"):
                build_job(path, settings())


class FakeUno:
    def __init__(self, *args, **kwargs) -> None:
        self.lines = deque([b"READY plotter_v5\n", b"STATUS proto=5 accel=1 x=0 y=0 y_pins=A0,A1,A2 max_feed=24000 queue=32 idle_ms=5000 queued=0 moving=0 enabled=0 fan_pin=9 fan_mode=AUTO fan_on=0 fan_count=0 fan_active=0 pen=UP\n"])
        self.is_open = True
        self.commands: list[str] = []
        self.x = self.y = 0
        self.enabled = False
        self.pen = "UP"
        self.fan_mode = "AUTO"
        self.fan_on = False
        self.fan_active = False

    def fan_report(self) -> bytes:
        return (f"FAN mode={self.fan_mode} on={int(self.fan_on)} count=0 "
                f"active={int(self.fan_active)}\n").encode()

    @property
    def in_waiting(self) -> int:
        return sum(len(line) for line in self.lines)

    def write(self, payload: bytes) -> None:
        command = payload.decode().strip()
        self.commands.append(command)
        if command == "STATUS":
            self.lines.append(f"STATUS proto=5 accel=1 x={self.x} y={self.y} y_pins=A0,A1,A2 max_feed=24000 queue=32 idle_ms=5000 queued=0 moving=0 enabled={int(self.enabled)} fan_pin=9 fan_mode={self.fan_mode} fan_on={int(self.fan_on)} fan_count=0 fan_active={int(self.fan_active)} pen={self.pen}\n".encode())
        elif command == "POS":
            self.lines.append(f"POS {self.x} {self.y}\n".encode())
        elif command.startswith("FAN "):
            self.fan_mode = command.split()[1]
            self.fan_on = command == "FAN MANUAL ON"
            self.lines.append(self.fan_report())
        elif command in ("JOB BEGIN", "JOB END"):
            self.fan_active = command == "JOB BEGIN"
            if self.fan_mode == "AUTO":
                self.fan_on = False
            self.lines.extend((self.fan_report(), b"OK\n"))
        elif command == "ENABLE ON":
            self.enabled = True
            self.lines.append(b"OK\n")
        elif command in ("ENABLE OFF", "STOP"):
            self.enabled = False
            if command == "STOP":
                self.pen = "UP"
                self.fan_active = False
                if self.fan_mode == "AUTO":
                    self.fan_on = False
            self.lines.append(b"OK\n")
        elif command.startswith("SERVO "):
            self.pen = "UP"
            self.lines.append(b"OK\n")
        elif command.startswith("PEN "):
            self.pen = command[4:]
            self.lines.append(b"OK\n")
        elif command.startswith("QUEUE "):
            if not self.enabled:
                self.lines.append(b"ERR driver_disabled\n")
                return
            _, move_id, x, y, _ = command.split()
            self.x, self.y = int(x), int(y)
            self.lines.extend((f"ACK {move_id}\n".encode(), f"DONE {move_id}\n".encode()))
        elif command.startswith(("MOVE ", "JOG ")):
            if not self.enabled:
                self.lines.append(b"ERR driver_disabled\n")
                return
            _, x, y, _ = command.split()
            if command.startswith("JOG "):
                self.x += int(x)
                self.y += int(y)
            else:
                self.x, self.y = int(x), int(y)
            self.lines.extend((b"OK\n", b"DONE\n"))
        else:
            self.lines.append(b"OK\n")

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        return self.lines.popleft() if self.lines else b""

    def reset_input_buffer(self) -> None:
        self.lines.clear()

    def close(self) -> None:
        self.is_open = False


class HostProtocolTests(unittest.TestCase):
    def test_export_accepts_macos_string_and_tuple_save_paths(self) -> None:
        commands = ["LIMIT 32000 28000", "PEN UP", "ENABLE OFF"]
        app = PlotterAPI()
        app.job = PlotJob(commands, [], "", {}, 400, 350, (400, 350))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "instructions.txt"
            for result in (str(path), (str(path),), path):
                path.write_text("old job", encoding="utf-8")
                class Dialog:
                    def create_file_dialog(self, *args, **kwargs):
                        return result
                with patch("plotter_app.webview.windows", [Dialog()]):
                    self.assertEqual(app.save_job(), str(path))
                exported = path.read_text()
                self.assertIn("JOB BEGIN\nLIMIT 32000 28000", exported)
                self.assertTrue(exported.endswith("ENABLE OFF\nJOB END\n"))
            with patch("plotter_app.webview.windows", [Dialog()]):
                result = None
                self.assertIsNone(app.save_job())
                result = folder
                with self.assertRaisesRegex(ValueError, "Choose a filename"):
                    app.save_job()
                result = str(Path(folder) / "without-extension")
                self.assertEqual(app.save_job(), result + ".txt")

    def test_manual_fan_power_and_mode_reports(self) -> None:
        app = PlotterAPI()
        app.port = FakeUno()
        app.port.reset_input_buffer()
        app._set(connected=True)
        self.assertTrue(app.set_fan("MANUAL", True)["fan_on"])
        self.assertEqual(app.get_state()["fan_mode"], "MANUAL")
        self.assertFalse(app.set_fan("MANUAL", False)["fan_on"])
        self.assertEqual(app.set_fan("AUTO")["fan_mode"], "AUTO")
        with self.assertRaises(ValueError):
            app.set_fan("invalid")

    def test_fan_report_during_buffered_moves_is_not_an_ack(self) -> None:
        class FanDuringQueueUno(FakeUno):
            def write(self, payload: bytes) -> None:
                if payload.startswith(b"QUEUE "):
                    self.lines.append(b"FAN mode=AUTO on=1 count=2000 active=1\n")
                super().write(payload)

        app = PlotterAPI()
        app.port = FanDuringQueueUno()
        app.port.reset_input_buffer()
        app._set(connected=True)
        app._execute("ENABLE ON")
        app._run_move_batch(["MOVE 0 0 1200", "MOVE 0 0 1200"], 1999)
        self.assertEqual(app.get_state()["progress"], 2001)
        self.assertTrue(app.get_state()["fan_on"])
        self.assertEqual(app.get_state()["fan_count"], 2000)

    def test_live_fan_control_does_not_read_from_plot_worker(self) -> None:
        app = PlotterAPI()
        app.port = FakeUno()
        app._set(connected=True)
        with patch.object(threading.Thread, "is_alive", return_value=True):
            app.worker = threading.Thread()
            with patch.object(app, "_read_line", side_effect=AssertionError("worker owns reads")):
                app.set_fan("MANUAL", True)
        self.assertIn("FAN MANUAL ON", app.port.commands)

    def test_stop_ends_automatic_fan_but_preserves_manual_cooling(self) -> None:
        app = PlotterAPI()
        app.port = FakeUno()
        app._set(connected=True, fan_mode="AUTO", fan_on=True, fan_active=True)
        self.assertFalse(app.stop()["fan_on"])
        app.cancel.clear()
        app.set_fan("MANUAL", True)
        app._set(fan_active=True)
        self.assertTrue(app.stop()["fan_on"])
        self.assertFalse(app.get_state()["fan_active"])

    def test_fan_reply_after_worker_exits_is_consumed_by_state_poll(self) -> None:
        app = PlotterAPI()
        app.port = FakeUno()
        app.port.reset_input_buffer()
        app.port.lines.append(b"FAN mode=MANUAL on=1 count=5100 active=0\n")
        app._set(connected=True, fan_pending=True)
        app.worker = threading.Thread()
        state = app.get_state()
        self.assertTrue(state["fan_on"])
        self.assertFalse(state["fan_pending"])
        self.assertEqual(state["fan_count"], 5100)

    def test_old_firmware_without_fan_is_rejected(self) -> None:
        class NoFanUno(FakeUno):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.lines = deque([b"STATUS proto=3 x=0 y=0 y_pins=A0,A1,A2 max_feed=16000 queue=32 idle_ms=5000 enabled=0 pen=UP\n"])
        with patch("plotter_app.serial.Serial", NoFanUno):
            app = PlotterAPI()
            with self.assertRaisesRegex(RuntimeError, "fan-control plotter firmware v5"):
                app.connect("fake")

    def test_pen_settling_times_are_configurable(self) -> None:
        app = PlotterAPI()
        app.port = FakeUno()
        app._set(connected=True)
        config = settings()
        config.update(pen_down_ms=80, pen_up_ms=90)
        with patch("plotter_app.time.sleep") as pause:
            lowered = app.calibrate("pen_red", config)
            app.calibrate("pen_up", config)
        self.assertEqual(lowered["pen"], "RED")
        self.assertFalse(lowered["enabled"])
        self.assertEqual(pause.call_args_list,
                         [call(0.09), call(0.08), call(0.09), call(0.09)])

    def test_calibration_jogs_use_axis_rpm(self) -> None:
        app = PlotterAPI()
        app.port = FakeUno()
        app._set(connected=True)
        config = settings()
        config.update(rpm_x=60, rpm_y=30, full_steps_x=200, full_steps_y=200,
                      microsteps_x=32, microsteps_y=32)
        app.calibrate("x+", config, 1)
        app.calibrate("y+", config, 1)
        self.assertIn("JOG 80 0 6400", app.port.commands)
        self.assertIn("JOG 0 80 3200", app.port.commands)
        self.assertEqual(app.port.commands.count("ENABLE ON"), 2)
        self.assertEqual(app.port.commands.count("ENABLE OFF"), 2)
        self.assertFalse(app.port.enabled)

    def test_old_speed_firmware_is_rejected(self) -> None:
        class OldSpeedUno(FakeUno):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.lines = deque([b"STATUS proto=1 x=0 y=0 y_pins=A0,A1,A2 max_feed=8000 moving=0 enabled=1 pen=UP\n"])

        with patch("plotter_app.serial.Serial", OldSpeedUno):
            app = PlotterAPI()
            with self.assertRaisesRegex(RuntimeError, "24,000-step/s motion firmware"):
                app.connect("fake")
            self.assertIsNone(app.port)

    def test_old_y_pin_layout_is_rejected(self) -> None:
        class OldPinmapUno(FakeUno):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.lines = deque([b"STATUS proto=1 x=0 y=0 moving=0 enabled=1 pen=UP\n"])

        with patch("plotter_app.serial.Serial", OldPinmapUno):
            app = PlotterAPI()
            with self.assertRaisesRegex(RuntimeError, "old Y pin layout"):
                app.connect("fake")
            self.assertIsNone(app.port)

    def test_old_unbuffered_firmware_is_rejected(self) -> None:
        class OldUno(FakeUno):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.lines = deque([b"STATUS proto=1 x=0 y=0 y_pins=A0,A1,A2 max_feed=16000 moving=0 enabled=1 pen=UP\n"])

        with patch("plotter_app.serial.Serial", OldUno):
            app = PlotterAPI()
            with self.assertRaisesRegex(RuntimeError, "idle-release plotter firmware"):
                app.connect("fake")

    def test_consecutive_moves_are_buffered_before_pen_change(self) -> None:
        class BufferedUno(FakeUno):
            def __init__(self) -> None:
                super().__init__()
                self.accepted: list[str] = []
                self.moves_before_first_done = 0

            def write(self, payload: bytes) -> None:
                command = payload.decode().strip()
                if not command.startswith("QUEUE "):
                    super().write(payload)
                    return
                self.commands.append(command)
                _, move_id, x, y, _ = command.split()
                self.accepted.append(move_id)
                self.x, self.y = int(x), int(y)
                self.lines.append(f"ACK {move_id}\n".encode())
                if len(self.accepted) == 32:
                    self.moves_before_first_done = len(self.accepted)
                    self.lines.extend(f"DONE {id_}\n".encode() for id_ in self.accepted)

        app = PlotterAPI()
        app.port = BufferedUno()
        app._set(connected=True)
        app.pen_up_delay_s = 0
        commands = ["LIMIT 100 100", "ENABLE ON", "PEN UP"] + [f"MOVE {n} 0 1200" for n in range(1, 33)] + ["PEN UP", "ENABLE OFF"]
        app.job = PlotJob(commands, [], "", {}, 1, 1, (1, 1))
        app._run_job()
        self.assertEqual(app.get_state()["phase"], "complete")
        self.assertEqual(app.get_state()["progress"], len(commands))
        self.assertEqual(app.get_state()["position"], {"x": 32, "y": 0})
        self.assertEqual(app.port.moves_before_first_done, 32)
        self.assertEqual(app.port.commands[-2:], ["ENABLE OFF", "JOB END"])
        self.assertIn("JOB BEGIN", app.port.commands)
        self.assertFalse(app.port.enabled)

    def test_invalid_serial_configuration_explains_usb_reconnect(self) -> None:
        app = PlotterAPI()
        with patch("plotter_app.serial.Serial", side_effect=termios.error(errno.EINVAL, "Invalid argument")):
            with self.assertRaisesRegex(RuntimeError, "Unplug and reconnect the Arduino USB cable"):
                app.connect("/dev/cu.usbserial-110")
        self.assertIsNone(app.port)
        self.assertFalse(app.get_state()["connected"])

    def test_clipboard_image_import(self) -> None:
        buffer = BytesIO()
        Image.new("RGBA", (16, 12), (255, 0, 0, 255)).save(buffer, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        app = PlotterAPI()
        imported = app.paste_image(data_url, "copied.png")
        self.assertEqual(imported["name"], "copied.png")
        self.assertEqual((imported["width"], imported["height"]), (16, 12))
        self.assertIn(imported["id"], app.images)
        app.pasted_images.cleanup()

    def test_calibration_does_not_require_a4_to_fit_machine_travel(self) -> None:
        app = PlotterAPI()
        app.port = FakeUno()
        app._set(connected=True)
        config = settings()
        config.update(orientation="landscape", white_cutoff=0)
        state = app.calibrate("enable", config)
        self.assertEqual(state["position"], {"x": 0, "y": 0})
        self.assertIn("ENABLE ON", app.port.commands)

    def test_import_multiple_images_and_generate_composition(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "one.png"
            second = Path(folder) / "two.png"
            Image.new("RGB", (12, 12), "red").save(first)
            Image.new("RGB", (12, 12), "blue").save(second)

            class Dialog:
                def create_file_dialog(self, *args, **kwargs):
                    self.allow_multiple = kwargs.get("allow_multiple")
                    return (str(first), str(second))

            dialog = Dialog()
            with patch("plotter_app.webview.windows", [dialog]):
                app = PlotterAPI()
                imported = app.pick_images()
            self.assertTrue(dialog.allow_multiple)
            self.assertEqual(len(imported), 2)
            self.assertNotEqual(imported[0]["id"], imported[1]["id"])
            layers = []
            for index, item in enumerate(imported):
                layers.append({"id": item["id"], "x_mm": 10 + index * 30, "y_mm": 10,
                               "width_mm": 20, "height_mm": 20, "alpha": 100,
                               "color_alpha": dict.fromkeys(("RED", "BLUE", "GREEN", "BLACK"), 100)})
            config = a4_settings()
            config["spacing"] = 1
            result = app.generate(config, layers)
            self.assertGreater(result["counts"]["RED"], 0)
            self.assertGreater(result["counts"]["BLUE"], 0)
            self.assertEqual((result["width_mm"], result["height_mm"]), (210, 297))
            self.assertTrue(result["plot_ready"])
            config.update(bed_width=210, bed_height=297, origin_x=200)
            preview_only = app.generate(config, layers)
            self.assertFalse(preview_only["plot_ready"])
            self.assertTrue(preview_only["preview"].startswith("data:image/png;base64,"))
            with self.assertRaisesRegex(ValueError, "exceed machine travel"):
                app.save_job()

    def test_connect_and_complete_move(self) -> None:
        with patch("plotter_app.serial.Serial", FakeUno):
            app = PlotterAPI()
            self.assertTrue(app.connect("fake")["connected"])
            app._execute("PEN RED")
            self.assertEqual(app.get_state()["pen"], "RED")
            app._execute("ENABLE ON")
            app._execute("MOVE 800 400 1200")
            self.assertEqual(app.get_state()["position"], {"x": 800, "y": 400})
            app._execute("JOG 0 80 1200")
            self.assertEqual(app.get_state()["position"], {"x": 800, "y": 480})
            app._execute("PEN UP")
            self.assertEqual(app.get_state()["pen"], "UP")
            app.disconnect()


if __name__ == "__main__":
    unittest.main()
