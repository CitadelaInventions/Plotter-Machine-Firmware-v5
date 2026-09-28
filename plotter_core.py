"""Turn an image into four-pen, absolute-coordinate plotter instructions."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import base64
import colorsys
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from plotter_paths import join_strokes, order_for_travel, trace_color
from plotter_pixels import apply_layer_opacity, classify_grid


PALETTE = {
    "RED": (221, 66, 67),
    "BLUE": (99, 149, 158),  # Citadela logo blue.
    "GREEN": (45, 150, 105),
    "BLACK": (35, 42, 53),
}
PEN_ORDER = tuple(PALETTE)
DEFAULT_THRESHOLDS = {"RED": 20, "BLUE": 20, "GREEN": 20, "BLACK": 15}
MAX_CELLS = 2000000
MAX_COMMANDS = 600000
MAX_FEED = 24000  # Protocol v5 major-axis STEP pulses/s; actual motion needs hardware testing.
MICROSTEP_CHOICES = (2, 4, 5, 8, 10, 16, 20, 25, 32, 40, 50, 64, 100, 125, 128)
A4_SIZES = {"portrait": (210.0, 297.0), "landscape": (297.0, 210.0)}


@dataclass(frozen=True)
class Segment:
    color: str
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class PlotJob:
    commands: list[str]
    segments: list[Segment]
    preview: str
    counts: dict[str, int]
    width_mm: float
    height_mm: float
    cells: tuple[int, int]
    plot_ready: bool = True
    metrics: dict = field(default_factory=dict)


def _job_metrics(commands: list[str], settings: dict) -> dict:
    """Ideal movement time from logical zero plus configured servo settling."""
    x = y = 0
    down = False
    draw_mm = travel_mm = seconds = 0.0
    moves = 0
    for command in commands:
        parts = command.split()
        if parts[0] == "MOVE":
            nx, ny, feed = map(int, parts[1:])
            distance = math.hypot((nx - x) / settings["steps_x"], (ny - y) / settings["steps_y"])
            if down: draw_mm += distance
            else: travel_mm += distance
            seconds += max(abs(nx - x), abs(ny - y)) / feed
            x, y = nx, ny
            moves += 1
        elif parts[0] == "PEN":
            down = parts[1] != "UP"
            seconds += settings["pen_down_ms" if down else "pen_up_ms"] / 1000
        elif parts[0] == "SERVO":
            seconds += settings["pen_up_ms"] / 1000
    return {"draw_mm": round(draw_mm, 1), "travel_mm": round(travel_mm, 1),
            "estimated_seconds": round(seconds, 1), "moves": moves}


def _number(settings: dict, key: str, low: float, high: float) -> float:
    try:
        value = float(settings[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Enter a valid {key.replace('_', ' ')}.") from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key.replace('_', ' ').capitalize()} must be {low:g}–{high:g}.")
    return value


def validate_machine_settings(settings: dict) -> dict:
    fields = {
        "bed_width": (20, 1000), "bed_height": (20, 1000),
        "steps_x": (1, 20000), "steps_y": (1, 20000),
        "servo_up": (0, 180), "servo_red": (0, 180),
        "servo_blue": (0, 180), "servo_green": (0, 180),
        "servo_black": (0, 180),
    }
    out = {key: _number(settings, key, *limits) for key, limits in fields.items()}
    out["feed"] = _number({"feed": settings.get("feed", 1200)}, "feed", 1, MAX_FEED)
    for key in ("pen_down_ms", "pen_up_ms"):
        out[key] = _number({key: settings.get(key, 240)}, key, 20, 1000)
    out["ramp_start_rpm"] = _number(
        {"ramp_start_rpm": settings.get("ramp_start_rpm", 10)}, "ramp_start_rpm", 1, 60)
    out["ramp_ms"] = _number({"ramp_ms": settings.get("ramp_ms", 300)}, "ramp_ms", 50, 3000)
    out["corner_percent"] = _number(
        {"corner_percent": settings.get("corner_percent", 25)}, "corner_percent", 0, 100)
    motor_keys = ("rpm_x", "rpm_y", "full_steps_x", "full_steps_y",
                  "microsteps_x", "microsteps_y")
    if any(key in settings for key in motor_keys):
        for axis in ("x", "y"):
            out[f"rpm_{axis}"] = _number(
                {f"rpm_{axis}": settings.get(f"rpm_{axis}", 11.25)},
                f"rpm_{axis}", 1, 600)
            for field, default, high in (("full_steps", 200, 2000),
                                         ("microsteps", 32, 128)):
                key = f"{field}_{axis}"
                value = _number({key: settings.get(key, default)}, key, 1, high)
                if not value.is_integer():
                    raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a whole number.")
                if field == "microsteps" and value not in MICROSTEP_CHOICES:
                    raise ValueError("Choose a supported microstep setting matching the physical driver.")
                out[key] = value
    return out


def ramp_command(settings: dict) -> str:
    """Convert the configured gentle starting RPM to the Uno's pulse domain."""
    if "rpm_x" in settings:
        starting_rate = min(settings["ramp_start_rpm"] * settings[f"full_steps_{axis}"] *
                            settings[f"microsteps_{axis}"] / 60 for axis in ("x", "y"))
    else:
        starting_rate = min(800, settings["feed"])
    return f"RAMP {max(1, min(MAX_FEED, round(starting_rate)))} {round(settings['ramp_ms'])} {round(settings['corner_percent'])}"


def feed_for_move(dx_steps: int, dy_steps: int, settings: dict) -> int:
    """Largest coordinated step rate that keeps both motors within their RPM limits."""
    if "rpm_x" not in settings:
        return round(settings["feed"])
    major = max(abs(dx_steps), abs(dy_steps))
    if major == 0:
        return 1
    limits = [float(MAX_FEED)]
    for axis, steps in (("x", abs(dx_steps)), ("y", abs(dy_steps))):
        if steps:
            pulses_per_second = (settings[f"rpm_{axis}"] *
                                 settings[f"full_steps_{axis}"] *
                                 settings[f"microsteps_{axis}"] / 60)
            limits.append(pulses_per_second * major / steps)
    return max(1, math.floor(min(limits)))


def validate_settings(settings: dict) -> dict:
    out = validate_machine_settings(settings)
    fields = {
        "spacing": (0.2, 5), "white_cutoff": (180, 255),
    }
    out.update({key: _number(settings, key, *limits) for key, limits in fields.items()})
    orientation = settings.get("orientation")
    canvas_mode = settings.get("canvas_mode", "paper")
    if canvas_mode not in ("machine", "paper"):
        raise ValueError("Choose a valid canvas mode.")
    out["canvas_mode"] = canvas_mode
    if canvas_mode == "machine":
        orientation = orientation or "portrait"
        if orientation not in A4_SIZES:
            raise ValueError("Canvas view must be normal or rotated.")
        width, height = out["bed_width"], out["bed_height"]
        if orientation == "landscape":
            width, height = height, width
        out.update(draw_width=width, draw_height=height, origin_x=0, origin_y=0,
                   orientation=orientation)
    else:
        out.update({key: _number(settings, key, *limits) for key, limits in {
            "draw_width": (5, 1000), "draw_height": (5, 1000),
            "origin_x": (0, 1000), "origin_y": (0, 1000),
        }.items()})
    if canvas_mode == "paper" and orientation is not None:
        if orientation not in A4_SIZES:
            raise ValueError("Paper orientation must be portrait or landscape.")
        width, height = A4_SIZES[orientation]
        out.update(draw_width=width, draw_height=height,
                   orientation=orientation)
    mode = settings.get("palette_mode", "four")
    if mode not in ("four", "mono"):
        raise ValueError("Palette mode must be four colors or monochrome.")
    mono_pen = str(settings.get("mono_pen", "BLACK")).upper()
    if mono_pen not in PEN_ORDER:
        raise ValueError("Choose a valid monochrome pen.")
    out["palette_mode"] = mode
    out["mono_pen"] = mono_pen
    out["mono_threshold"] = _number({"mono_threshold": settings.get("mono_threshold", 210)},
                                    "mono_threshold", 0, 255)
    out["path_tolerance"] = _number({"path_tolerance": settings.get("path_tolerance", 0.1)},
                                    "path_tolerance", 0, 0.5)
    for color in PEN_ORDER:
        key = f"threshold_{color.lower()}"
        out[key] = _number({key: settings.get(key, DEFAULT_THRESHOLDS[color])}, key, 0, 100)
    for color in PEN_ORDER:
        for axis in ("x", "y"):
            key = f"offset_{color.lower()}_{axis}"
            out[key] = _number(settings, key, -100, 100)
    return out


def _classify(rgb: tuple[int, int, int], settings: dict) -> str | None:
    r, g, b = rgb
    luminance = (0.2126 * r + 0.7152 * g + 0.0722 * b)
    if settings["palette_mode"] == "mono":
        return settings["mono_pen"] if luminance <= settings["mono_threshold"] else None
    # Preserve saturated pale colors while leaving near-white paper blank.
    if (r + g + b) / 3 >= settings["white_cutoff"] and max(rgb) - min(rgb) < 42:
        return None
    hue, saturation, value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    angle = hue * 360
    hue_centers = {"RED": 0, "GREEN": 120, "BLUE": 205}
    candidates = []
    for color, center in hue_centers.items():
        distance = abs(angle - center)
        distance = min(distance, 360 - distance)
        score = saturation * max(0.0, 1.0 - distance / 100.0) * min(1.0, value / 0.15)
        if score > 0 and score * 100 >= settings[f"threshold_{color.lower()}"]:
            candidates.append((score, color))
    if candidates:
        return max(candidates)[1]
    darkness = 100 * (1 - luminance / 255)
    return "BLACK" if darkness >= settings["threshold_black"] else None


def compose_page(layers: list[dict], sources: dict[str, Path], settings: dict) -> Image.Image:
    """Composite placed images across the selected canvas at plot resolution."""
    s = validate_settings(settings)
    if "orientation" not in s:
        raise ValueError("Choose a canvas orientation.")
    if not 1 <= len(layers) <= 50:
        raise ValueError("Add between 1 and 50 images to the page.")
    page_width, page_height = s["draw_width"], s["draw_height"]
    cols = max(1, round(page_width / s["spacing"]))
    rows = max(1, round(page_height / s["spacing"]))
    if cols * rows > MAX_CELLS:
        minimum = math.ceil(math.sqrt(page_width * page_height / MAX_CELLS) * 100) / 100
        raise ValueError(f"This canvas needs line spacing of about {minimum:g} mm or more "
                         "to stay within two million plot cells.")
    paper = Image.new("RGBA", (cols, rows), "white")
    for layer in layers:
        if not isinstance(layer, dict) or layer.get("id") not in sources:
            raise ValueError("An image in the composition is missing. Import it again.")
        x = _number(layer, "x_mm", -5000, 5000)
        y = _number(layer, "y_mm", -5000, 5000)
        width = _number(layer, "width_mm", 0.1, 5000)
        height = _number(layer, "height_mm", 0.1, 5000)
        rotation = _number({"rotation": layer.get("rotation", 0)}, "rotation", 0, 270)
        if rotation not in (0, 90, 180, 270):
            raise ValueError("Image rotation must be a quarter turn.")
        overall_alpha = _number(layer, "alpha", 0, 100) / 100
        color_alpha = layer.get("color_alpha")
        if not isinstance(color_alpha, dict):
            raise ValueError("An image is missing its color alpha settings.")
        opacity = {color: overall_alpha * _number(color_alpha, color, 0, 100) / 100
                   for color in PEN_ORDER}
        visible_left, visible_top = max(0.0, x), max(0.0, y)
        visible_right = min(page_width, x + width)
        visible_bottom = min(page_height, y + height)
        if visible_right <= visible_left or visible_bottom <= visible_top:
            continue
        left = round(visible_left * cols / page_width)
        top = round(visible_top * rows / page_height)
        right = round(visible_right * cols / page_width)
        bottom = round(visible_bottom * rows / page_height)
        if right <= left or bottom <= top:
            continue
        with Image.open(sources[layer["id"]]) as source:
            source = ImageOps.exif_transpose(source)
            if rotation:
                source = source.transpose({90: Image.Transpose.ROTATE_270,
                                           180: Image.Transpose.ROTATE_180,
                                           270: Image.Transpose.ROTATE_90}[rotation])
            source_left = min(source.width - 1, max(0, round((visible_left - x) * source.width / width)))
            source_top = min(source.height - 1, max(0, round((visible_top - y) * source.height / height)))
            source_right = min(source.width, max(source_left + 1,
                round((visible_right - x) * source.width / width)))
            source_bottom = min(source.height, max(source_top + 1,
                round((visible_bottom - y) * source.height / height)))
            crop = (source_left, source_top, source_right, source_bottom)
            image = source.convert("RGBA").crop(crop).resize(
                (right - left, bottom - top), Image.Resampling.LANCZOS)
        image = apply_layer_opacity(image, s, opacity, _classify)
        paper.alpha_composite(image, (left, top))
    return paper.convert("RGB")


def build_job(image_path: str | Path | Image.Image, settings: dict,
              *, full_page: bool = False, strict_bounds: bool = True) -> PlotJob:
    s = validate_settings(settings)
    if isinstance(image_path, Image.Image):
        rgb = image_path.convert("RGB")
    else:
        path = Path(image_path)
        if not path.is_file():
            raise ValueError("Choose an image first.")
        with Image.open(path) as source:
            source = ImageOps.exif_transpose(source)
            source.thumbnail((4000, 4000))
            image = source.convert("RGBA")
        paper = Image.new("RGBA", image.size, "white")
        paper.alpha_composite(image)
        rgb = paper.convert("RGB")

    if full_page:
        width_mm, height_mm = s["draw_width"], s["draw_height"]
        cols, rows = rgb.size
    else:
        scale = min(s["draw_width"] / rgb.width, s["draw_height"] / rgb.height)
        width_mm, height_mm = rgb.width * scale, rgb.height * scale
        cols = max(1, round(width_mm / s["spacing"]))
        rows = max(1, round(height_mm / s["spacing"]))
    if cols * rows > MAX_CELLS:
        raise ValueError("Too many plot cells. Increase line spacing or reduce drawing size.")
    grid = rgb if full_page else rgb.resize((cols, rows), Image.Resampling.LANCZOS)
    labels = classify_grid(grid, s, _classify)
    paths = []
    last_point = (cols - 1, 0) if s["canvas_mode"] == "machine" and \
        s["orientation"] == "landscape" else (0, 0)
    pitch_x, pitch_y = width_mm / cols, height_mm / rows
    pitch = min(pitch_x, pitch_y)
    # The larger pitch bounds physical deviation even for non-square cells.
    tolerance = min(0.45, s["path_tolerance"] / max(pitch_x, pitch_y))
    # Retracing ink competes with 2 servo waits and a pen-up move. Use the
    # slower motor's linear speed so the joining allowance stays conservative.
    pulses = [s[f"rpm_{axis}"] * s[f"full_steps_{axis}"] * s[f"microsteps_{axis}"] / 60
              if "rpm_x" in s else s["feed"] for axis in ("x", "y")]
    speed = min(min(MAX_FEED, pulses[i]) / s[f"steps_{axis}"]
                for i, axis in enumerate(("x", "y")))
    join_budget = min(64.0, speed * (s["pen_up_ms"] + s["pen_down_ms"]) / 1000 / pitch)
    for color in PEN_ORDER:
        candidates = trace_color(labels, color, tolerance=tolerance, join_budget=join_budget)
        ordered = order_for_travel(candidates, last_point)
        for points in join_strokes(ordered, labels, color, join_budget, tolerance):
            paths.append((color, points))
            last_point = points[-1]
    if not paths:
        raise ValueError("No drawable color found. Adjust the palette thresholds or choose another image.")
    # A solitary plot cell is a pen dab; every other path visits its colored
    # cells through connected moves. The preview renders these exact paths.
    preview_scale = min(8.0, 1400 / cols, 1400 / rows)
    preview_width = max(1, round(cols * preview_scale))
    preview_height = max(1, round(rows * preview_scale))
    enlarged = Image.new("RGB", (preview_width, preview_height), "white")
    draw = ImageDraw.Draw(enlarged)
    stroke_width = max(1, round(min(preview_width / cols, preview_height / rows) * 0.7))
    for color, points in paths:
        plotted = [((x + 0.5) * preview_width / cols,
                    (y + 0.5) * preview_height / rows) for x, y in points]
        if len(plotted) == 1:
            x, y = plotted[0]
            radius = max(0.5, stroke_width / 2)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=PALETTE[color])
        else:
            draw.line(plotted, fill=PALETTE[color], width=stroke_width, joint="curve")
    output = BytesIO()
    enlarged.save(output, format="PNG")
    preview_url = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    segments: list[Segment] = []
    commands = [
        f"LIMIT {round(s['bed_width'] * s['steps_x'])} {round(s['bed_height'] * s['steps_y'])}",
        "SERVO " + " ".join(str(round(s[key])) for key in
            ("servo_up", "servo_red", "servo_blue", "servo_green", "servo_black")),
        ramp_command(s),
        "ENABLE ON",
    ]
    counts = {color: 0 for color in PEN_ORDER}
    plot_ready = True
    previous_target: tuple[int, int] | None = None
    for color, path in paths:
        if len(commands) + len(path) + 3 > MAX_COMMANDS:
            raise ValueError("Too many path instructions. Increase line spacing or simplify the artwork.")
        points = [(width_mm * (x + 0.5) / cols,
                   height_mm * (y + 0.5) / rows) for x, y in path]
        if len(points) == 1:
            x, y = points[0]
            segments.append(Segment(color, x, y, x, y))
        else:
            segments.extend(Segment(color, *start, *end)
                            for start, end in zip(points, points[1:]))
        # A tip mounted +X from the reference needs the carriage moved -X
        # to place that tip at the same point on the paper.
        ox = s["origin_x"] - s[f"offset_{color.lower()}_x"]
        oy = s["origin_y"] - s[f"offset_{color.lower()}_y"]
        if s["canvas_mode"] == "machine" and s["orientation"] == "landscape":
            machine_points = [(y + ox, s["bed_height"] - x + oy) for x, y in points]
        else:
            machine_points = [(x + ox, y + oy) for x, y in points]
        if any(x < 0 or x > s["bed_width"] or y < 0 or y > s["bed_height"]
               for x, y in machine_points):
            if strict_bounds:
                raise ValueError(f"{color.title()} strokes exceed machine travel. Move artwork inward or adjust the pen offset.")
            plot_ready = False
        def command(x: float, y: float) -> str:
            nonlocal previous_target
            target = (round(x * s["steps_x"]), round(y * s["steps_y"]))
            # The first move starts wherever the carriage currently is, so use
            # the slower axis cap until the job has a known prior target.
            feed = feed_for_move(*(target[i] - previous_target[i] for i in (0, 1)), s) \
                if previous_target is not None else feed_for_move(1, 1, s)
            previous_target = target
            return f"MOVE {target[0]} {target[1]} {feed}"
        commands.append(command(*machine_points[0]))
        commands.append(f"PEN {color}")
        commands.extend(command(x, y) for x, y in machine_points[1:])
        commands.append("PEN UP")
        counts[color] += 1
    commands.append("ENABLE OFF")
    if len(commands) > MAX_COMMANDS:
        raise ValueError("Job has too many instructions. Increase line spacing.")
    return PlotJob(commands, segments, preview_url, counts, width_mm, height_mm,
                   (cols, rows), plot_ready, _job_metrics(commands, s))
