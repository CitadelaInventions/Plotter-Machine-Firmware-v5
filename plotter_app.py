"""pywebview desktop host for the four-pen Arduino plotter."""

from __future__ import annotations

import base64
import binascii
from collections import deque
import errno
from io import BytesIO
from pathlib import Path
import shutil
import sys
import tempfile
import termios
import threading
import time
import uuid

from PIL import Image, ImageOps
import serial
from serial.tools import list_ports
import webview

from plotter_core import MAX_FEED, PlotJob, build_job, compose_page, feed_for_move, ramp_command, validate_machine_settings


BAUD = 115200
MOVE_WINDOW = 32  # Never exceed the Uno's advertised queued-move capacity.


class PlotterAPI:
    def __init__(self) -> None:
        self.port: serial.Serial | None = None
        self.images: dict[str, Path] = {}
        self.pasted_images: tempfile.TemporaryDirectory | None = None
        self.job: PlotJob | None = None
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.fan_read_lock = threading.Lock()
        self.state = {
            "connected": False, "port": "", "phase": "idle", "progress": 0,
            "total": 0, "message": "Choose an image to begin.", "position": {"x": 0, "y": 0},
            "pen": "UP", "enabled": False, "log": [],
            "fan_mode": "AUTO", "fan_on": False, "fan_count": 0,
            "fan_active": False, "fan_pending": False,
        }
        self.log = deque(maxlen=80)
        self.pen_down_delay_s = 0.24
        self.pen_up_delay_s = 0.24

    def _set(self, **changes) -> None:
        with self.state_lock:
            self.state.update(changes)

    def _log(self, message: str) -> None:
        self.log.append(message)
        self._set(log=list(self.log))

    def get_state(self) -> dict:
        # A fan command issued at the very end of a plot may outlive its reader.
        # Let the UI poll consume its deferred report once that worker has exited.
        if (self.state.get("fan_pending") and self.port and
                not (self.worker and self.worker.is_alive()) and
                self.fan_read_lock.acquire(blocking=False)):
            try:
                while getattr(self.port, "in_waiting", 0):
                    line = self._read_line()
                    if line.startswith("FAN "):
                        self._fan_report(line)
                    elif line.startswith("STATUS "):
                        self._status(line)
                    elif line.startswith("POS "):
                        self._position(line)
                    elif line.startswith("ERR "):
                        self._set(fan_pending=False, message="Arduino: " + line[4:])
            except (OSError, serial.SerialException):
                self._set(fan_pending=False)
            finally:
                self.fan_read_lock.release()
        with self.state_lock:
            return dict(self.state)

    def ports(self) -> list[str]:
        return [entry.device for entry in list_ports.comports()]

    def _write(self, line: str) -> None:
        if not self.port or not self.port.is_open:
            raise RuntimeError("Connect to the Arduino first.")
        with self.write_lock:
            self.port.write((line + "\n").encode("ascii"))
            self.port.flush()
        if not line.startswith(("STATUS", "POS")):
            self._log("> " + line)

    def _read_line(self, timeout: float = 0.15) -> str:
        if not self.port:
            raise RuntimeError("Arduino disconnected.")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            raw = self.port.readline()
            if raw:
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    if not line.startswith(("STATUS", "POS")):
                        self._log("< " + line)
                    return line
        return ""

    def _status(self, line: str) -> None:
        values = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
        try:
            position = {"x": int(values["x"]), "y": int(values["y"])}
            self._set(position=position, pen=values.get("pen", "UP"),
                      enabled=values.get("enabled") == "1")
        except (KeyError, ValueError):
            pass
        if "fan_mode" in values:
            self._fan_report("FAN " + " ".join(
                f"{key}={values['fan_' + key]}" for key in ("mode", "on", "count", "active")
                if "fan_" + key in values))

    def _fan_report(self, line: str) -> None:
        values = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
        try:
            if values["mode"] not in ("AUTO", "MANUAL"):
                raise ValueError("Unknown fan mode")
            self._set(fan_mode=values["mode"], fan_on=values["on"] == "1",
                      fan_count=int(values["count"]), fan_active=values["active"] == "1",
                      fan_pending=False)
        except (KeyError, ValueError) as exc:
            raise RuntimeError("Invalid fan report from Arduino: " + line) from exc

    def set_fan(self, mode: str, on: bool = False) -> dict:
        """FAN has a distinct reply so it can share an active move stream safely."""
        if mode not in ("AUTO", "MANUAL") or not isinstance(on, bool):
            raise ValueError("Choose automatic or manual fan mode and an on/off state.")
        if not self.get_state()["connected"]:
            raise RuntimeError("Connect to the Arduino first.")
        command = "FAN AUTO" if mode == "AUTO" else "FAN MANUAL " + ("ON" if on else "OFF")
        with self.fan_read_lock:
            self._set(fan_pending=True)
            try:
                self._write(command)
                if self.worker and self.worker.is_alive():
                    # The plotting worker owns reads and consumes FAN alongside ACK/DONE.
                    return self.get_state()
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    line = self._read_line()
                    if line.startswith("FAN "):
                        self._fan_report(line)
                        if self.state["fan_mode"] == mode and (mode == "AUTO" or self.state["fan_on"] == on):
                            return self.get_state()
                    elif line.startswith("STATUS "):
                        self._status(line)
                    elif line.startswith("POS "):
                        self._position(line)
                    elif line.startswith("ERR "):
                        raise RuntimeError("Arduino: " + line[4:])
                raise RuntimeError("No fan acknowledgement from the Arduino.")
            except Exception:
                self._set(fan_pending=False)
                raise

    def _position(self, line: str) -> None:
        try:
            _, x, y = line.split()
            self._set(position={"x": int(x), "y": int(y)})
        except ValueError as exc:
            raise RuntimeError("Invalid position report from Arduino: " + line) from exc

    def connect(self, port_name: str) -> dict:
        if self.port:
            return self.get_state()
        if not port_name:
            raise ValueError("Choose a serial port.")
        try:
            selected = serial.Serial(port_name, BAUD, timeout=0.08, write_timeout=1)
        except (OSError, termios.error) as exc:
            # The macOS CH340 driver can expose a port yet reject tcsetattr,
            # including when asked to keep its current serial settings.
            if getattr(exc, "errno", None) == errno.EINVAL or exc.args[:1] == (errno.EINVAL,):
                raise RuntimeError(
                    f"macOS cannot configure {port_name} (error 22). "
                    "Unplug and reconnect the Arduino USB cable, then refresh the port list and connect again."
                ) from exc
            raise
        self.port = selected
        try:
            # Opening an Uno resets it. Require our plotter protocol, so the
            # original single-axis firmware cannot be moved by mistake.
            deadline = time.monotonic() + 4.0
            next_query = 0.0
            verified = False
            while time.monotonic() < deadline:
                if time.monotonic() >= next_query:
                    self._write("STATUS")
                    next_query = time.monotonic() + 0.6
                line = self._read_line()
                if line.startswith("STATUS "):
                    if "y_pins=A0,A1,A2" not in line:
                        raise RuntimeError(
                            "The Uno still has the old Y pin layout. Upload the updated "
                            "plotter_firmware/plotter_firmware.ino sketch before connecting."
                        )
                    if "max_feed=16000" not in line and f"max_feed={MAX_FEED}" not in line:
                        raise RuntimeError(
                            f"The Uno needs the {MAX_FEED:,}-step/s motion firmware. Upload the latest "
                            "plotter_firmware/plotter_firmware.ino sketch before connecting."
                        )
                    if "queue=32" not in line or "idle_ms=5000" not in line:
                        raise RuntimeError(
                            "The Uno needs the idle-release plotter firmware. Upload the latest "
                            "plotter_firmware/plotter_firmware.ino sketch before connecting."
                        )
                    if ("proto=4" not in line and "proto=5" not in line) or "fan_pin=9" not in line:
                        raise RuntimeError(
                            "The Uno needs the fan-control plotter firmware v5. Upload the latest "
                            "plotter_firmware/plotter_firmware.ino sketch before connecting."
                        )
                    if "proto=5" not in line or f"max_feed={MAX_FEED}" not in line or "accel=1" not in line:
                        raise RuntimeError(
                            "The Uno needs eased-motion firmware v5. Upload the latest "
                            "plotter_firmware/plotter_firmware.ino sketch before connecting."
                        )
                    self._status(line)
                    verified = True
                    break
            if not verified:
                raise RuntimeError("The board did not report plotter firmware v5. Upload the included Arduino sketch.")
            self._set(connected=True, port=port_name, message="Arduino ready. Set the physical origin before plotting.")
            return self.get_state()
        except Exception:
            selected.close()
            self.port = None
            raise

    def disconnect(self) -> dict:
        self.stop()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=2)
        if self.port:
            self.port.close()
        self.port = None
        self._set(connected=False, port="", phase="idle", fan_active=False,
                  fan_pending=False, message="Disconnected.")
        return self.get_state()

    def _register_images(self, paths: list[Path], names: list[str] | None = None) -> list[dict]:
        if self.worker and self.worker.is_alive():
            raise RuntimeError("Stop the current plot before adding images.")
        if len(self.images) + len(paths) > 50:
            raise ValueError("A composition can contain at most 50 images.")
        added = []
        new_images = {}
        for index, path in enumerate(paths):
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source)
                original_width, original_height = image.size
                if original_width * original_height > 50_000_000:
                    raise ValueError(f"{path.name} is too large to import (50 megapixel limit).")
                image.thumbnail((900, 900))
                thumb = image.convert("RGBA")
            buffer = BytesIO()
            thumb.save(buffer, format="PNG")
            image_id = uuid.uuid4().hex
            new_images[image_id] = path
            added.append({"id": image_id, "name": names[index] if names else path.name,
                          "width": original_width, "height": original_height,
                          "preview": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")})
        self.images.update(new_images)
        self.job = None
        self._set(phase="idle", progress=0, total=0,
                  message=f"Added {len(added)} image{'s' if len(added) != 1 else ''}. Arrange the machine canvas.")
        return added

    def pick_images(self) -> list[dict]:
        paths = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True,
            file_types=("Image files (*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.tif;*.tiff;*.gif)",),
        )
        if not paths:
            return []
        return self._register_images([Path(selected) for selected in paths])

    def paste_image(self, data_url: str, name: str = "Pasted image.png") -> dict:
        if not isinstance(data_url, str) or not data_url.startswith("data:image/") or ";base64," not in data_url:
            raise ValueError("Copy an image to the clipboard before pasting.")
        if len(data_url) > 40_000_000:
            raise ValueError("The pasted image is too large to import.")
        try:
            raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("The clipboard image could not be read.") from exc
        if self.pasted_images is None:
            self.pasted_images = tempfile.TemporaryDirectory(prefix="citadela-images-")
        path = Path(self.pasted_images.name) / f"{uuid.uuid4().hex}.png"
        try:
            with Image.open(BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source)
                if image.width * image.height > 50_000_000:
                    raise ValueError("The pasted image is too large to import (50 megapixel limit).")
                image.convert("RGBA").save(path)
        except OSError as exc:
            raise ValueError("The clipboard does not contain a supported image.") from exc
        display_name = Path(str(name)).name[:100] or "Pasted image.png"
        return self._register_images([path], [display_name])[0]

    def generate(self, settings: dict, layers: list[dict]) -> dict:
        if self.worker and self.worker.is_alive():
            raise RuntimeError("Stop the current plot before regenerating.")
        began = time.monotonic()
        page = compose_page(layers, self.images, settings)
        self.job = build_job(page, settings, full_page=True, strict_bounds=False)
        job = self.job
        machine = validate_machine_settings(settings)
        self.pen_down_delay_s = machine["pen_down_ms"] / 1000
        self.pen_up_delay_s = machine["pen_up_ms"] / 1000
        message = (f"Ready: {sum(job.counts.values()):,} paths, {len(job.commands):,} instructions."
                   if job.plot_ready else
                   "Preview ready. A pen tip offset would move the carriage beyond its travel; move artwork inward or correct travel and pen offsets in Calibration.")
        self._set(phase="ready" if job.plot_ready else "preview", progress=0,
                  total=len(job.commands), message=message)
        return {"preview": job.preview, "counts": job.counts,
                "strokes": sum(job.counts.values()), "instructions": len(job.commands),
                "width_mm": round(job.width_mm, 1), "height_mm": round(job.height_mm, 1),
                "cells": list(job.cells), "plot_ready": job.plot_ready, "message": message,
                "metrics": {**job.metrics, "generation_seconds": round(time.monotonic() - began, 2)}}

    def save_job(self) -> str | None:
        if not self.job:
            raise ValueError("Generate a plot first.")
        if not self.job.plot_ready:
            raise ValueError("Some strokes exceed machine travel. Adjust calibration and regenerate before exporting.")
        selected = webview.windows[0].create_file_dialog(
            webview.FileDialog.SAVE, save_filename="citadela_job.txt",
            directory=str(Path.home() / "Downloads"),
            file_types=("Plotter instructions (*.txt)",),
        )
        if not selected:
            return None
        # Cocoa returns a single path string for SAVE; other backends use a tuple.
        chosen = selected if isinstance(selected, (str, Path)) else selected[0]
        path = Path(chosen).expanduser()
        if path.is_dir():
            raise ValueError("Choose a filename for the exported instructions.")
        if not path.suffix:
            path = path.with_suffix(".txt")
        path.write_text("# Plotter protocol v5; eased motion; absolute X/Y step coordinates\nJOB BEGIN\n" +
                        "\n".join(self.job.commands) + "\nJOB END\n", encoding="utf-8")
        return str(path)

    def _execute(self, command: str) -> None:
        start_position = self.get_state()["position"].copy() if command.startswith("JOG ") else None
        self._write(command)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                raise RuntimeError("Plot stopped.")
            line = self._read_line()
            if line == "OK":
                break
            if line.startswith("ERR "):
                if command.startswith("LIMIT ") and line == "ERR invalid_limit":
                    raise RuntimeError(
                        "The Arduino's current step position is outside the new travel limits. "
                        "Changing microsteps or steps/mm leaves the old position counts in place. "
                        "With pens raised and drivers disabled, return the carriage to the physical "
                        "machine origin, then press Zero current position and regenerate the plot. "
                        "Check that steps/mm matches the physical driver switch setting."
                    )
                raise RuntimeError("Arduino: " + line[4:])
            if line.startswith("STATUS "):
                self._status(line)
            if line.startswith("POS "):
                self._position(line)
            if line.startswith("FAN "):
                self._fan_report(line)
        else:
            raise RuntimeError(f"No acknowledgement for {command}.")
        if command.startswith(("MOVE ", "JOG ")):
            # Compact POS reports keep the heartbeat and live cursor current.
            next_heartbeat = time.monotonic() + 0.5
            while True:
                if self.cancel.is_set():
                    raise RuntimeError("Plot stopped.")
                line = self._read_line()
                if line == "DONE":
                    parts = command.split()
                    if parts[0] == "MOVE":
                        self._set(position={"x": int(parts[1]), "y": int(parts[2])})
                    else:
                        assert start_position is not None
                        self._set(position={"x": start_position["x"] + int(parts[1]),
                                            "y": start_position["y"] + int(parts[2])})
                    return
                if line == "ABORT" or line.startswith("ERR "):
                    raise RuntimeError("Move interrupted: " + line)
                if line.startswith("STATUS "):
                    self._status(line)
                if line.startswith("POS "):
                    self._position(line)
                if line.startswith("FAN "):
                    self._fan_report(line)
                if time.monotonic() >= next_heartbeat:
                    self._write("POS")
                    next_heartbeat = time.monotonic() + 0.5
        elif command.startswith("PEN ") or command.startswith("SERVO "):
            pen = command[4:] if command.startswith("PEN ") else "UP"
            self._set(pen=pen)
            time.sleep(self.pen_up_delay_s if pen == "UP" else self.pen_down_delay_s)
        elif command.startswith("ENABLE "):
            self._set(enabled=command == "ENABLE ON")

    def _run_move_batch(self, commands: list[str], first_index: int) -> None:
        """Keep several consecutive MOVE commands ready on the Uno.

        ACK confirms admission to its bounded queue; DONE confirms actual
        completion. Pen and setup commands are sent only after every DONE.
        """
        pending: dict[int, tuple[int, int, int]] = {}
        sent = completed = 0
        awaiting_ack: int | None = None
        ack_deadline = 0.0
        last_response = time.monotonic()
        next_heartbeat = last_response + 0.5
        while completed < len(commands):
            if self.cancel.is_set():
                raise RuntimeError("Plot stopped.")
            if awaiting_ack is None and sent < len(commands) and len(pending) < MOVE_WINDOW:
                parts = commands[sent].split()
                if len(parts) != 4 or parts[0] != "MOVE":
                    raise RuntimeError("Invalid move in generated job.")
                move_id = first_index + sent + 1
                x, y = int(parts[1]), int(parts[2])
                pending[move_id] = (x, y, move_id)
                awaiting_ack = move_id
                ack_deadline = time.monotonic() + 4.0
                self._write(f"QUEUE {move_id} {x} {y} {parts[3]}")
                sent += 1

            line = self._read_line()
            if line:
                last_response = time.monotonic()
            if line.startswith("STATUS "):
                self._status(line)
            elif line.startswith("POS "):
                self._position(line)
            elif line.startswith("FAN "):
                self._fan_report(line)
            elif line.startswith("ACK "):
                if line != f"ACK {awaiting_ack}":
                    raise RuntimeError("Unexpected queue acknowledgement: " + line)
                awaiting_ack = None
            elif line.startswith("DONE "):
                try:
                    done_id = int(line.split()[1])
                except (IndexError, ValueError) as exc:
                    raise RuntimeError("Invalid queue completion: " + line) from exc
                if not pending or done_id != next(iter(pending)) or done_id == awaiting_ack:
                    raise RuntimeError("Out-of-order queue completion: " + line)
                x, y, progress = pending.pop(done_id)
                self._set(position={"x": x, "y": y}, progress=progress)
                completed += 1
            elif line.startswith("ERR ") or line.startswith("ABORT"):
                raise RuntimeError("Queued move interrupted: " + line)
            elif line.startswith("READY "):
                raise RuntimeError("Arduino reset during the plot; re-establish machine zero.")
            elif line:
                raise RuntimeError("Unexpected Arduino response: " + line)

            now = time.monotonic()
            if awaiting_ack is not None and now > ack_deadline:
                raise RuntimeError("No acknowledgement for queued move.")
            if now - last_response > 4.0:
                raise RuntimeError("Arduino stopped responding during the plot.")
            if now >= next_heartbeat:
                self._write("POS")
                next_heartbeat = now + 0.5

    def start_job(self) -> dict:
        if not self.port or not self.state["connected"]:
            raise RuntimeError("Connect to the Arduino first.")
        if not self.job:
            raise ValueError("Generate a plot first.")
        if not self.job.plot_ready:
            raise ValueError("Some strokes exceed machine travel. Adjust calibration and regenerate before plotting.")
        if self.worker and self.worker.is_alive():
            raise RuntimeError("A plot is already running.")
        self.cancel.clear()
        self.port.reset_input_buffer()
        self.worker = threading.Thread(target=self._run_job, daemon=True)
        self.worker.start()
        return self.get_state()

    def _run_job(self) -> None:
        assert self.job
        self._set(phase="plotting", progress=0,
                  message="Plotting with up to 32 moves buffered on the Uno.")
        try:
            self._execute("JOB BEGIN")
            index = 0
            commands = self.job.commands
            while index < len(commands):
                if self.cancel.is_set():
                    raise RuntimeError("Plot stopped.")
                if commands[index].startswith("MOVE "):
                    end = index + 1
                    while end < len(commands) and commands[end].startswith("MOVE "):
                        end += 1
                    self._run_move_batch(commands[index:end], index)
                    index = end
                else:
                    self._execute(commands[index])
                    index += 1
                    self._set(progress=index)
            if not commands or commands[-1] != "ENABLE OFF":
                self._execute("ENABLE OFF")
            self._execute("JOB END")
            self._set(phase="complete", enabled=False,
                      message="Plot complete. Pens raised and stepper drivers released.")
        except Exception as exc:
            self._set(phase="stopped" if self.cancel.is_set() else "error",
                      enabled=False, pen="UP", message=str(exc))
            try:
                self._write("STOP")
            except Exception:
                pass
            self._end_fan_job()

    def _end_fan_job(self) -> None:
        changes = {"fan_active": False}
        if self.get_state()["fan_mode"] == "AUTO":
            changes["fan_on"] = False
        self._set(**changes)

    def stop(self) -> dict:
        self.cancel.set()
        if self.port and self.port.is_open:
            try:
                self._write("STOP")
            except (serial.SerialException, serial.SerialTimeoutException):
                pass
        if self.state["phase"] == "plotting":
            self._set(phase="stopped", pen="UP", enabled=False,
                      message="Stop sent. Drivers off and pens raised; position may need re-zeroing.")
        elif self.port:
            self._set(pen="UP", enabled=False,
                      message="Stop sent. Drivers off and pens raised; position may need re-zeroing.")
        self._end_fan_job()
        return self.get_state()

    def calibrate(self, action: str, settings: dict, distance_mm: float = 1) -> dict:
        if not self.port or not self.state["connected"]:
            raise RuntimeError("Connect to the Arduino first.")
        if self.worker and self.worker.is_alive():
            raise RuntimeError("Calibration is unavailable while plotting.")
        s = validate_machine_settings(settings)
        self.pen_down_delay_s = s["pen_down_ms"] / 1000
        self.pen_up_delay_s = s["pen_up_ms"] / 1000
        allowed = {"zero", "x+", "x-", "y+", "y-", "pen_red", "pen_blue",
                   "pen_green", "pen_black", "pen_up", "enable", "disable"}
        if action not in allowed:
            raise ValueError("Unknown calibration command.")
        self.cancel.clear()
        self.port.reset_input_buffer()
        try:
            if action == "zero":
                self._execute("ZERO")
            elif action in ("enable", "disable"):
                self._execute("ENABLE " + ("ON" if action == "enable" else "OFF"))
            elif action.startswith("pen_"):
                self._execute("SERVO " + " ".join(str(round(s[key])) for key in
                    ("servo_up", "servo_red", "servo_blue", "servo_green", "servo_black")))
                self._execute("PEN " + action[4:].upper())
            else:
                if not 0.1 <= float(distance_mm) <= 50:
                    raise ValueError("Jog distance must be 0.1–50 mm.")
                self._execute("PEN UP")
                self._execute(f"LIMIT {round(s['bed_width'] * s['steps_x'])} {round(s['bed_height'] * s['steps_y'])}")
                dx = round(float(distance_mm) * s["steps_x"]) if action[0] == "x" else 0
                dy = round(float(distance_mm) * s["steps_y"]) if action[0] == "y" else 0
                if action.endswith("-"):
                    dx, dy = -dx, -dy
                self._execute(ramp_command(s))
                self._execute("ENABLE ON")
                self._execute(f"JOG {dx} {dy} {feed_for_move(dx, dy, s)}")
            if action not in ("enable", "disable"):
                self._execute("ENABLE OFF")
        except Exception:
            try:
                self._write("STOP")
            except Exception:
                pass
            self._set(enabled=False, pen="UP")
            raise
        self._write("STATUS")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            line = self._read_line()
            if line.startswith("STATUS "):
                self._status(line)
                break
            if line.startswith("FAN "):
                self._fan_report(line)
        return self.get_state()


def main() -> None:
    api = PlotterAPI()
    web_dir = Path(__file__).parent / "web"
    index = (web_dir / "index.html").resolve().as_uri()
    icon = (web_dir / "citadela-logo.png").resolve()
    storage = Path.home() / ".citadela" / "webview"
    old_storage = Path.home() / ".chromatic_plotter" / "webview"
    if not storage.exists() and old_storage.is_dir():
        shutil.copytree(old_storage, storage)
    storage.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        from AppKit import NSApplication, NSImage
        from Foundation import NSProcessInfo

        NSProcessInfo.processInfo().setProcessName_("Citadela")
        NSApplication.sharedApplication().setApplicationIconImage_(
            NSImage.alloc().initWithContentsOfFile_(str(icon))
        )
    webview.create_window("Citadela", index, js_api=api, width=1450,
                          height=900, min_size=(1050, 680), background_color="#10151f")
    try:
        webview.start(debug=False, private_mode=False, storage_path=str(storage), icon=str(icon))
    finally:
        api.disconnect()
        if api.pasted_images is not None:
            api.pasted_images.cleanup()


if __name__ == "__main__":
    main()
