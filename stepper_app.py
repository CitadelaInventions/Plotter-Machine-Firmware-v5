"""Slider controls and an animated position estimate for the Uno stepper sketch."""

from __future__ import annotations

from collections import deque
import math
import re
import time
import tkinter as tk
from tkinter import messagebox, ttk

import serial
from serial.tools import list_ports


BAUD_RATE = 115200
MICROSTEP_VALUES = (1, 2, 4, 8, 16, 32, 64, 128, 256)
UNITS_PER_REV = 200 * 256  # Firmware counts in 1/256-full-step units.


class RotorView:
    """Project a small 3D motor/rotor model into a Tk canvas."""

    def __init__(self, parent: ttk.Frame) -> None:
        self.canvas = tk.Canvas(parent, height=390, background="#101923", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.base_units = 0
        self.anchor_time = time.monotonic()
        self.running = False
        self.mode = "IDLE"
        self.rpm = 0.0
        self.microsteps = 32
        self.remaining = 0
        self.direction = 1
        self.display_divisor = 4.0
        self.canvas.bind("<Configure>", lambda _event: self.draw())
        self.canvas.after(33, self.animate)

    def set_status(self, values: dict[str, str]) -> None:
        try:
            self.base_units = int(values["units"])
            self.rpm = float(values["rpm"])
            self.microsteps = int(values["microsteps"])
            self.remaining = int(values.get("remaining", "0"))
            self.running = values["running"] == "1"
            self.direction = 1 if values["direction"] == "CW" else -1
            self.mode = values["mode"]
            self.display_divisor = float(values.get("display_divisor", "4"))
            self.anchor_time = time.monotonic()
        except (KeyError, ValueError):
            return

    def estimated_units(self) -> float:
        if not self.running:
            return float(self.base_units)
        elapsed = max(0.0, time.monotonic() - self.anchor_time)
        travel = elapsed * self.rpm * UNITS_PER_REV / 60.0
        if self.mode in ("MOVE", "OSC") and self.microsteps:
            travel = min(travel, self.remaining * (256 / self.microsteps))
        return self.base_units + self.direction * travel

    def project(self, x: float, y: float, z: float) -> tuple[float, float]:
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        scale = min(width * 0.30, height * 0.32)
        return (
            width / 2 + scale * (0.91 * x + 0.41 * y),
            height / 2 + 20 + scale * (-0.27 * x + 0.50 * y - 0.75 * z),
        )

    def disc(self, radius: float, z: float, color: str, outline: str = "") -> None:
        points: list[float] = []
        for index in range(64):
            angle = 2 * math.pi * index / 64
            points.extend(self.project(radius * math.cos(angle), radius * math.sin(angle), z))
        self.canvas.create_polygon(points, fill=color, outline=outline, width=2)

    def cylinder(self, radius: float, back: float, front: float,
                 face: str, side_dark: str, side_light: str) -> None:
        self.disc(radius, back, side_dark)
        for index in range(48):
            a = 2 * math.pi * index / 48
            b = 2 * math.pi * (index + 1) / 48
            corners = (
                self.project(radius * math.cos(a), radius * math.sin(a), back),
                self.project(radius * math.cos(b), radius * math.sin(b), back),
                self.project(radius * math.cos(b), radius * math.sin(b), front),
                self.project(radius * math.cos(a), radius * math.sin(a), front),
            )
            flat = [coordinate for point in corners for coordinate in point]
            shade = side_light if math.sin(a + 0.7) > 0 else side_dark
            self.canvas.create_polygon(flat, fill=shade, outline=shade)
        self.disc(radius, front, face, outline=side_light)

    def draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        self.cylinder(1.22, -0.75, -0.34, "#344b5c", "#172a39", "#263d4d")
        self.cylinder(0.96, -0.31, 0.25, "#516c7d", "#223848", "#3a5667")

        angle = (self.estimated_units() / self.display_divisor % UNITS_PER_REV) * 2 * math.pi / UNITS_PER_REV
        for index in range(6):
            spoke_angle = angle + index * math.pi / 3
            p1 = self.project(0.26 * math.cos(spoke_angle), 0.26 * math.sin(spoke_angle), 0.265)
            p2 = self.project(0.78 * math.cos(spoke_angle), 0.78 * math.sin(spoke_angle), 0.265)
            canvas.create_line(*p1, *p2, fill="#9fb4bf", width=5, capstyle="round")

        self.disc(0.27, 0.28, "#a8bec9", outline="#d8e6eb")
        center = self.project(0, 0, 0.31)
        tip = self.project(0.86 * math.cos(angle), 0.86 * math.sin(angle), 0.31)
        canvas.create_line(*center, *tip, fill="#ffbd54", width=7, capstyle="round")
        canvas.create_oval(tip[0] - 8, tip[1] - 8, tip[0] + 8, tip[1] + 8,
                           fill="#fff2c2", outline="#ffbd54", width=2)
        canvas.create_text(14, 18, anchor="w", fill="#cfe3ec",
                           font=("Segoe UI", 11, "bold"), text="3D ROTOR POSITION")
        canvas.create_text(14, 42, anchor="w", fill="#83a5b8",
                           font=("Segoe UI", 10), text="Yellow tip = estimated shaft angle")

    def animate(self) -> None:
        if self.canvas.winfo_exists():
            self.draw()
            self.canvas.after(33, self.animate)


class StepperApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Serial Stepper Controller")
        root.geometry("1080x750")
        root.minsize(940, 640)
        self.serial_port: serial.Serial | None = None
        self.connected_port = ""
        self.receive_buffer = bytearray()
        self.commands: deque[str] = deque()
        self.awaiting: str | None = None
        self.command_deadline = 0.0
        self.priority_replies = 0
        self.priority_deadline = 0.0
        self.ready = False
        self.first_status = True
        self.syncing_sliders = False
        self.config_dirty = False
        self.last_status: dict[str, str] = {}
        self.log_lines: deque[str] = deque(maxlen=120)

        self.port_var = tk.StringVar()
        self.rpm_var = tk.IntVar(value=80)
        self.microstep_index = tk.IntVar(value=5)
        self.jog_var = tk.DoubleVar(value=5.0)
        self.move_var = tk.DoubleVar(value=90.0)
        self.osc_var = tk.DoubleVar(value=90.0)
        self.dwell_var = tk.IntVar(value=200)
        self.display_divisor_var = tk.DoubleVar(value=4.0)
        self.connection_var = tk.StringVar(value="Disconnected")
        self.notice_var = tk.StringVar(value="Connect to the Arduino to begin.")
        self.motion_var = tk.StringVar(value="Motion: unknown")
        self.steps_var = tk.StringVar(value="Signed STEP pulses: 0")
        self.position_var = tk.StringVar(value="Position from zero: 0.00°")
        self.tip_var = tk.StringVar(value="Tip angle: 0.00°")

        self.action_buttons: list[ttk.Button] = []
        self.build_ui()
        self.refresh_ports()
        self.set_connected(False)
        self.root.after(40, self.poll_serial)
        self.root.after(100, self.poll_status)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=12)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        connection = ttk.LabelFrame(shell, text="Connection", padding=8)
        connection.grid(row=0, column=0, sticky="ew")
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="Serial port").grid(row=0, column=0, padx=(0, 8))
        self.port_box = ttk.Combobox(connection, textvariable=self.port_var, width=22)
        self.port_box.grid(row=0, column=1, sticky="ew")
        self.refresh_button = ttk.Button(connection, text="Refresh", command=self.refresh_ports)
        self.refresh_button.grid(row=0, column=2, padx=6)
        self.connect_button = ttk.Button(connection, text="Connect", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=3)
        ttk.Label(connection, textvariable=self.connection_var).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

        body = ttk.Frame(shell)
        body.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        settings = ttk.LabelFrame(left, text="Settings · release a slider to apply", padding=8)
        settings.grid(row=0, column=0, sticky="ew")
        settings.columnconfigure(0, weight=1)
        self.add_slider(settings, 0, "Speed (RPM)", self.rpm_var, 5, 200, 1,
                        lambda: f"{self.rpm_var.get()} RPM")
        self.add_slider(settings, 1, "Microsteps", self.microstep_index, 0, 8, 1,
                        lambda: f"1/{MICROSTEP_VALUES[self.microstep_index.get()]}")
        self.add_slider(settings, 2, "Jog angle", self.jog_var, 0.1, 90, 0.1,
                        lambda: f"{self.jog_var.get():.1f}°")
        self.add_slider(settings, 3, "Move angle", self.move_var, 0.1, 360, 0.1,
                        lambda: f"{self.move_var.get():.1f}°")
        self.add_slider(settings, 4, "Oscillation angle", self.osc_var, 0.1, 360, 0.1,
                        lambda: f"{self.osc_var.get():.1f}°")
        self.add_slider(settings, 5, "Oscillation dwell", self.dwell_var, 0, 5000, 10,
                        lambda: f"{self.dwell_var.get()} ms")
        self.add_slider(settings, 6, "Tip display divisor", self.display_divisor_var, 1, 16, 0.25,
                        lambda: f"÷{self.display_divisor_var.get():.2f}")
        ttk.Label(settings, text="Match microsteps to the driver. Tip divisor changes the display only.",
                  wraplength=440).grid(row=14, column=0, sticky="w", pady=(7, 0))

        controls = ttk.LabelFrame(left, text="Motion", padding=8)
        controls.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for col in range(3):
            controls.columnconfigure(col, weight=1)
        self.add_action(controls, "Run CW", lambda: self.motion("RUN CW"), 0, 0)
        self.add_action(controls, "Run CCW", lambda: self.motion("RUN CCW"), 0, 1)
        self.add_action(controls, "Jog CW", lambda: self.motion(f"MOVE {self.jog_var.get():g} CW"), 1, 0)
        self.add_action(controls, "Jog CCW", lambda: self.motion(f"MOVE {self.jog_var.get():g} CCW"), 1, 1)
        self.add_action(controls, "Move CW", lambda: self.motion(f"MOVE {self.move_var.get():g} CW"), 2, 0)
        self.add_action(controls, "Move CCW", lambda: self.motion(f"MOVE {self.move_var.get():g} CCW"), 2, 1)
        self.add_action(controls, "Start oscillation", self.start_oscillation, 3, 0, 2)
        self.stop_button = ttk.Button(controls, text="STOP", command=self.stop)
        self.stop_button.grid(row=0, column=2, rowspan=2, sticky="nsew", padx=4, pady=3)
        self.add_action(controls, "Enable driver", lambda: self.enable(True), 2, 2)
        self.disable_button = ttk.Button(controls, text="Disable driver", command=lambda: self.enable(False))
        self.disable_button.grid(row=3, column=2, sticky="ew", padx=4, pady=3)
        self.zero_button = ttk.Button(controls, text="Zero position", command=self.zero_position)
        self.zero_button.grid(row=4, column=0, columnspan=3, sticky="ew", padx=4, pady=3)

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        model = ttk.LabelFrame(right, text="Estimated motor position", padding=6)
        model.grid(row=0, column=0, sticky="nsew")
        self.rotor = RotorView(model)
        readings = ttk.Frame(right, padding=(8, 12))
        readings.grid(row=1, column=0, sticky="ew")
        for row, variable in enumerate((self.motion_var, self.steps_var,
                                        self.position_var, self.tip_var)):
            ttk.Label(readings, textvariable=variable, font=("Segoe UI", 11)).grid(
                row=row, column=0, sticky="w", pady=2
            )
        ttk.Label(readings,
                  text="Pulse-count estimate only; missed motor steps need an encoder to detect.",
                  wraplength=450).grid(row=4, column=0, sticky="w", pady=(8, 0))

        bottom = ttk.Frame(shell)
        bottom.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.notice_var, wraplength=1000).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(bottom, text="Serial log", command=self.show_log).grid(
            row=0, column=1, sticky="e", padx=(8, 0)
        )

    def add_slider(self, parent: ttk.LabelFrame, index: int, title: str,
                   variable: tk.Variable, low: float, high: float,
                   resolution: float, formatted) -> None:
        row = 2 * index
        heading = ttk.Frame(parent)
        heading.grid(row=row, column=0, sticky="ew", pady=(3, 0))
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text=title).grid(row=0, column=0, sticky="w")
        value_label = ttk.Label(heading, text=formatted(), width=12, anchor="e")
        value_label.grid(row=0, column=1, sticky="e")
        scale = tk.Scale(parent, from_=low, to=high, resolution=resolution,
                         orient="horizontal", showvalue=False, variable=variable,
                         highlightthickness=0, borderwidth=0, sliderlength=18,
                         command=lambda _value: self.slider_changed(value_label, formatted))
        scale.grid(row=row + 1, column=0, sticky="ew")
        scale.bind("<ButtonRelease-1>", self.slider_released)
        scale.bind("<KeyRelease>", self.slider_released)

    def slider_changed(self, label: ttk.Label, formatted) -> None:
        label.configure(text=formatted())
        if not self.syncing_sliders:
            self.config_dirty = True

    def slider_released(self, _event=None) -> None:
        if not self.ready:
            return
        self.apply_configuration()

    def add_action(self, parent: ttk.LabelFrame, text: str, callback,
                   row: int, col: int, span: int = 1) -> None:
        button = ttk.Button(parent, text=text, command=callback)
        button.grid(row=row, column=col, columnspan=span, sticky="ew", padx=4, pady=3)
        self.action_buttons.append(button)

    def refresh_ports(self) -> None:
        ports = [port.device for port in list_ports.comports()]
        self.port_box["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def set_connected(self, connected: bool) -> None:
        self.connect_button.configure(text="Disconnect" if connected else "Connect")
        self.port_box.configure(state="disabled" if connected else "normal")
        self.refresh_button.configure(state="disabled" if connected else "normal")
        state = "normal" if connected and self.ready and not self.priority_replies else "disabled"
        for button in (*self.action_buttons, self.disable_button, self.zero_button):
            button.configure(state=state)
        self.stop_button.configure(state="normal" if connected else "disabled")

    def toggle_connection(self) -> None:
        if self.serial_port:
            self.disconnect(send_stop=True)
            return
        port_name = self.port_var.get().strip()
        if not port_name:
            messagebox.showerror("No serial port", "Select a serial port first.")
            return
        try:
            self.serial_port = serial.Serial(port_name, BAUD_RATE, timeout=0, write_timeout=1)
        except serial.SerialException as exc:
            messagebox.showerror("Connection failed", str(exc))
            return
        self.connected_port = port_name
        self.receive_buffer.clear()
        self.ready = False
        self.first_status = True
        self.connection_var.set(f"Connected to {port_name}; waiting for Arduino...")
        self.notice_var.set("Arduino Uno normally resets when the serial port opens.")
        self.set_connected(True)

    def disconnect(self, send_stop: bool = False) -> None:
        if self.serial_port:
            if send_stop:
                try:
                    self.serial_port.write(b"STOP\n")
                    self.serial_port.flush()
                except serial.SerialException:
                    pass
            self.serial_port.close()
        self.serial_port = None
        self.commands.clear()
        self.awaiting = None
        self.priority_replies = 0
        self.ready = False
        self.connection_var.set("Disconnected")
        self.motion_var.set("Motion: unknown")
        self.set_connected(False)

    def log_line(self, line: str) -> None:
        self.log_lines.append(line)

    def show_log(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Serial log")
        window.geometry("650x320")
        log = tk.Text(window, wrap="none")
        log.pack(fill="both", expand=True)
        log.insert("1.0", "\n".join(self.log_lines))
        log.configure(state="disabled")

    def write_line(self, command: str, log: bool = True) -> bool:
        if not self.serial_port:
            return False
        try:
            self.serial_port.write((command + "\n").encode("ascii"))
        except (serial.SerialException, serial.SerialTimeoutException) as exc:
            self.notice_var.set(f"Serial write failed: {exc}")
            self.disconnect()
            return False
        if log:
            self.log_line("> " + command)
        return True

    def configuration_command(self) -> str:
        rpm = self.rpm_var.get()
        microsteps = MICROSTEP_VALUES[self.microstep_index.get()]
        jog = self.jog_var.get()
        move = self.move_var.get()
        osc = self.osc_var.get()
        dwell = self.dwell_var.get()
        display_divisor = self.display_divisor_var.get()
        step_hz = rpm * 200 * microsteps / 60
        if not 16 <= step_hz <= 25000:
            raise ValueError(
                f"{rpm} RPM at 1/{microsteps} microsteps needs {step_hz:,.0f} "
                "steps/s. Choose a combination from 16 to 25,000 steps/s."
            )
        return f"CONFIG {rpm:g} {microsteps} {jog:g} {move:g} {osc:g} {dwell} {display_divisor:g}"

    def send_sequence(self, *commands: str) -> bool:
        if not self.serial_port or not self.ready:
            return False
        if self.awaiting or self.commands or self.priority_replies:
            self.notice_var.set("Wait for the previous command, or press STOP.")
            return False
        self.commands.extend(commands)
        self.send_next()
        return True

    def send_next(self) -> None:
        if not self.commands:
            self.notice_var.set("Command accepted.")
            if self.config_dirty:
                self.apply_configuration()
            return
        command = self.commands.popleft()
        if self.write_line(command):
            self.awaiting = command
            self.command_deadline = time.monotonic() + 3.0

    def apply_configuration(self) -> None:
        try:
            command = self.configuration_command()
        except ValueError as exc:
            self.notice_var.set(str(exc))
            return
        if self.awaiting or self.commands or self.priority_replies:
            self.config_dirty = True
            self.notice_var.set("Slider changed; applying after the current command.")
            return
        self.config_dirty = False
        self.send_sequence(command)

    def motion(self, command: str) -> None:
        try:
            configuration = self.configuration_command()
        except ValueError as exc:
            messagebox.showerror("Invalid motor settings", str(exc))
            return
        if self.send_sequence(configuration, command):
            self.config_dirty = False

    def start_oscillation(self) -> None:
        self.motion(f"OSC {self.osc_var.get():g} {self.dwell_var.get()}")

    def zero_position(self) -> None:
        if self.last_status.get("mode") != "IDLE":
            self.notice_var.set("Press STOP before zeroing the position.")
            return
        self.send_sequence("ZERO")

    def stop(self) -> None:
        outstanding = bool(self.awaiting)
        self.commands.clear()
        self.awaiting = None
        self.send_priority("STOP", outstanding)

    def enable(self, enabled: bool) -> None:
        if enabled:
            self.send_sequence("ENABLE ON")
        else:
            outstanding = bool(self.awaiting)
            self.commands.clear()
            self.awaiting = None
            self.send_priority("ENABLE OFF", outstanding)

    def send_priority(self, command: str, outstanding: bool) -> None:
        if self.write_line(command):
            self.priority_replies += 1 + int(outstanding)
            self.priority_deadline = time.monotonic() + 3.0
            self.set_connected(True)
            self.notice_var.set(f"{command} sent; waiting for acknowledgement.")

    def poll_status(self) -> None:
        if self.serial_port and not self.awaiting and not self.commands and not self.priority_replies:
            self.write_line("STATUS", log=False)
        self.root.after(100, self.poll_status)

    def sync_sliders(self, values: dict[str, str]) -> None:
        self.syncing_sliders = True
        try:
            self.rpm_var.set(round(float(values["rpm"])))
            microsteps = int(values["microsteps"])
            if microsteps in MICROSTEP_VALUES:
                self.microstep_index.set(MICROSTEP_VALUES.index(microsteps))
            self.jog_var.set(float(values.get("jog", "5")))
            self.move_var.set(float(values.get("move", "90")))
            self.osc_var.set(float(values.get("osc", "90")))
            self.dwell_var.set(int(values.get("dwell", "200")))
            self.display_divisor_var.set(float(values.get("display_divisor", "4")))
        except (KeyError, ValueError):
            pass
        finally:
            self.syncing_sliders = False

    def update_status(self, values: dict[str, str]) -> None:
        self.last_status = values
        if self.first_status and "units" in values:
            self.sync_sliders(values)
            self.first_status = False
            self.ready = True
            self.set_connected(True)
            self.connection_var.set(f"Connected to {self.connected_port}")
        self.rotor.set_status(values)
        try:
            steps = int(values["steps"])
            units = int(values["units"])
            divisor = float(values.get("display_divisor", "4"))
            degrees = units * 360.0 / UNITS_PER_REV / divisor
            tip = degrees % 360.0
            self.steps_var.set(f"Signed STEP pulses: {steps:,}")
            self.position_var.set(f"Estimated tip position: {degrees:,.2f}°")
            self.tip_var.set(f"Tip angle: {tip:.2f}°  |  Full turns: {degrees / 360:,.3f}")
            self.motion_var.set(
                f"Motion: {values['mode']}  |  {values['rpm']} RPM  |  "
                f"{values['direction']}  |  "
                f"Driver {'enabled' if values['enabled'] == '1' else 'disabled'}"
            )
        except (KeyError, ValueError):
            pass

    def handle_line(self, line: str) -> None:
        if not line.startswith("STATUS "):
            self.log_line("< " + line)
        if line == "ERR host_timeout":
            self.notice_var.set("Arduino stopped: serial heartbeat timed out.")
        elif line == "OK":
            if self.priority_replies:
                self.priority_replies -= 1
                if not self.priority_replies:
                    self.set_connected(True)
                    self.notice_var.set("Stop or driver command acknowledged.")
            elif self.awaiting:
                self.awaiting = None
                self.send_next()
        elif line.startswith("ERR "):
            if self.priority_replies:
                self.priority_replies -= 1
                if not self.priority_replies:
                    self.set_connected(True)
            else:
                self.commands.clear()
                self.awaiting = None
            self.notice_var.set("Arduino rejected command: " + line[4:])
        elif line.startswith("STATUS "):
            values = dict(re.findall(r"([a-z_]+)=([^ ]+)", line))
            self.update_status(values)
        elif line.startswith("READY"):
            self.notice_var.set("Arduino is ready.")

    def poll_serial(self) -> None:
        if self.serial_port:
            try:
                data = self.serial_port.read(min(self.serial_port.in_waiting, 2048))
                self.receive_buffer.extend(data)
                while b"\n" in self.receive_buffer:
                    raw, _, remainder = self.receive_buffer.partition(b"\n")
                    self.receive_buffer = bytearray(remainder)
                    line = raw.decode("ascii", errors="replace").strip()
                    if line:
                        self.handle_line(line)
                if len(self.receive_buffer) > 4096:
                    self.receive_buffer.clear()
            except serial.SerialException as exc:
                self.notice_var.set(f"Connection lost: {exc}")
                self.disconnect()
            if self.awaiting and time.monotonic() > self.command_deadline:
                self.notice_var.set(f"No response to {self.awaiting}; command queue cleared.")
                self.awaiting = None
                self.commands.clear()
            if self.priority_replies and time.monotonic() > self.priority_deadline:
                self.priority_replies = 0
                self.set_connected(True)
                self.notice_var.set("No acknowledgement for STOP or driver command; check connection.")
        self.root.after(40, self.poll_serial)

    def close(self) -> None:
        self.disconnect(send_stop=True)
        self.root.destroy()


if __name__ == "__main__":
    window = tk.Tk()
    StepperApp(window)
    window.mainloop()
