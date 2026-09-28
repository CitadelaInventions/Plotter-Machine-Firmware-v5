"""Compare path planners without touching hardware.

python3 tests/benchmark_planner.py --modules /path/to/frozen/modules --output result.json
Times include image composition, classification, planning, and preview generation.
Ideal duration assumes a logical origin of (0, 0), ignores serial/acceleration overhead,
and includes the host's 240 ms servo settling delays.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw

PCB = Path("/Users/macbook/Downloads/2D_PCB1_2026-08-31 (1).png")


def settings(width: float = 60, height: float = 80) -> dict:
    result = dict(canvas_mode="machine", orientation="landscape",
                  bed_width=width, bed_height=height, origin_x=0, origin_y=0,
                  draw_width=height, draw_height=width, spacing=0.35,
                  steps_x=800, steps_y=800, feed=12800, white_cutoff=245,
                  palette_mode="mono", mono_pen="BLACK", mono_threshold=221,
                  rpm_x=120, rpm_y=120, full_steps_x=200, full_steps_y=200,
                  microsteps_x=32, microsteps_y=32, pen_down_ms=240, pen_up_ms=240,
                  servo_up=45, servo_red=95, servo_blue=95, servo_green=95, servo_black=95)
    for color in ("red", "blue", "green", "black"):
        result[f"threshold_{color}"] = 20
        result[f"offset_{color}_x"] = result[f"offset_{color}_y"] = 0
    return result


def synthetic_cases() -> dict[str, Image.Image]:
    size = (round(80 / 0.35), round(60 / 0.35))
    cases = {}
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).line((15, 18, 212, 150), fill="black", width=1)
    cases["diagonal_thin_line"] = image
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.text((15, 8), "CITADELA PCB 2026", fill="black")
    for y in (40, 72, 104, 136):
        draw.line([(15, y), (45, y), (62, y - 14), (120, y - 14),
                   (136, y), (204, y)], fill="black", width=3)
        for x in (22, 90, 160, 199):
            draw.line([(x, y), (x, y + 13)], fill="black", width=2)
            draw.ellipse((x - 4, y + 10, x + 4, y + 18), fill="black")
    draw.rectangle((160, 38, 204, 120), outline="black", width=2)
    cases["text_and_connected_traces"] = image
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 20, 208, 150), radius=12, fill="black")
    draw.ellipse((76, 48, 152, 123), fill="white")
    cases["solid_fill_with_hole"] = image
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for y in range(12, 160, 12):
        for x in range(12, 218, 12):
            draw.point((x, y), fill="black")
    cases["separated_dots"] = image
    return cases


def metrics(job, config: dict, elapsed: float) -> dict:
    x = y = 0
    pen_down = False
    down_distance = up_distance = movement_seconds = settling_seconds = 0.0
    moves = strokes = 0
    limits = (round(config["bed_width"] * config["steps_x"]),
              round(config["bed_height"] * config["steps_y"]))
    for command in job.commands:
        tokens = command.split()
        if tokens[0] == "MOVE":
            nx, ny, feed = map(int, tokens[1:])
            if not (0 <= nx <= limits[0] and 0 <= ny <= limits[1]):
                raise AssertionError(f"MOVE outside calibrated bounds: {command}; limits={limits}")
            dx, dy = nx - x, ny - y
            distance = math.hypot(dx / config["steps_x"], dy / config["steps_y"])
            if pen_down:
                down_distance += distance
            else:
                up_distance += distance
            movement_seconds += max(abs(dx), abs(dy)) / feed
            x, y = nx, ny
            moves += 1
        elif tokens[0] == "PEN":
            pen_down = tokens[1] != "UP"
            strokes += int(pen_down)
            settling_seconds += config["pen_down_ms" if pen_down else "pen_up_ms"] / 1000
        elif tokens[0] == "SERVO":
            settling_seconds += config["pen_up_ms"] / 1000
    rounded = lambda value: round(value, 6)
    return dict(generation_seconds=rounded(elapsed), strokes=strokes, moves=moves,
                instructions=len(job.commands), pen_down_mm=rounded(down_distance),
                pen_up_mm=rounded(up_distance), move_seconds=rounded(movement_seconds),
                settling_seconds=rounded(settling_seconds),
                ideal_seconds=rounded(movement_seconds + settling_seconds),
                all_moves_within_bounds=True, cells=list(job.cells))


def benchmark(module_dir: Path, pcb_path: Path) -> dict:
    # Reload both coupled modules so importing the live app earlier cannot silently
    # mix a frozen core with a different path planner.
    sys.path.insert(0, str(module_dir.resolve()))
    for name in ("plotter_core", "plotter_paths"):
        sys.modules.pop(name, None)
    core = importlib.import_module("plotter_core")
    results = {}
    config = settings()
    for name, image in synthetic_cases().items():
        started = time.perf_counter()
        job = core.build_job(image, config, full_page=True)
        results[name] = metrics(job, config, time.perf_counter() - started)
    config = settings(250, 500)
    with Image.open(pcb_path) as image:
        scale = min(500 / image.width, 250 / image.height)
        width, height = image.width * scale, image.height * scale
    layer = dict(id="pcb", x_mm=(500 - width) / 2, y_mm=(250 - height) / 2,
                 width_mm=width, height_mm=height, alpha=100,
                 color_alpha=dict.fromkeys(core.PALETTE, 100))
    started = time.perf_counter()
    page = core.compose_page([layer], {"pcb": pcb_path}, config)
    job = core.build_job(page, config, full_page=True)
    results["real_pcb_full_machine"] = metrics(job, config, time.perf_counter() - started)
    return dict(modules=str(module_dir.resolve()), assumptions={
        "initial_machine_position_steps": [0, 0], "rpm": 120, "steps_per_mm": 800,
        "full_steps_per_revolution": 200, "microsteps": 32, "spacing_mm": 0.35,
        "monochrome_threshold": 221, "pen_up_ms": 240, "pen_down_ms": 240,
        "duration_excludes": "serial overhead and acceleration"}, cases=results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--pcb", type=Path, default=PCB)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = benchmark(args.modules, args.pcb)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
