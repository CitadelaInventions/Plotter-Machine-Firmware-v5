# Citadela

A desktop image-to-plotter app for an Arduino Uno, two STEP/DIR stepper drivers, and four independent pen servos. The desktop window is HTML/CSS/JavaScript hosted by **pywebview**; Python handles images and serial streaming. The **Calibration** page is part of the same app. Its name, window icon, and blue interface theme use the supplied Citadela logo.

The earlier `stepper_app.py` and `serial_stepper_controller.ino` are kept as a reference for the original single-axis machine. They use a different serial protocol and cannot run the new four-pen job. Upload `plotter_firmware/plotter_firmware.ino` for this app.

## Wiring

| Function | Uno pin |
| --- | --- |
| X STEP / DIR / ENABLE | D2 / D3 / D10 |
| Y STEP / DIR / ENABLE | A0 / A1 / A2 |
| Red / blue / green / black servo signal | D6 / D7 / D8 / D11 |
| Cooling fan relay signal (active HIGH) | D9 |

Both driver enable inputs are active LOW and are switched together by `ENABLE ON` / `ENABLE OFF`. The drivers start disabled, are enabled only while a jog or plot needs them, and turn off after each action or plot. Firmware also releases them after five seconds idle or immediately on `STOP`. The manual **Enable drivers** control lasts at most five seconds without activity. When the drivers are off, the carriage has no motor holding force; if it shifts, move it back to the physical origin and press **Zero current position** before plotting.
On the Uno, A0, A1, and A2 are also digital GPIO pins. Power off the stepper driver supply before moving the Y control wires from D4/D5/D9 to A0/A1/A2. Leave X and the four servo signals on their existing pins. Upload the new plotter sketch; its `STATUS` line reports `y_pins=A0,A1,A2` so you can verify the active pin map. A pin change alone does not prove the old pins were defective: if Y still does not move after rewiring, check driver power, the shared ground, active-low enable, and STEP/DIR wiring.

The stepper drivers and servos need suitable **external power supplies**. Connect the Uno, driver, and servo supply grounds together. Do not power motors or four servos from Uno I/O pins. The firmware has software coordinate limits and a 2.5-second serial heartbeat; it has **no physical homing or limit-switch support**. Provide a physical emergency power cutoff and do a low-speed pen-up test before mounting pens.

## Install and run

1. Upload `plotter_firmware/plotter_firmware.ino` to an Arduino Uno using Arduino IDE or `arduino-cli`. Select the Uno board and its serial port.
2. Install Python 3.9+ dependencies: `python3 -m pip install -r requirements.txt`.
3. Run `python3 citadela.py` from this folder. Close Arduino Serial Monitor before connecting.
4. In **Calibration**, enter the measured X/Y carriage travel, X/Y steps per millimeter, motor full steps per revolution, and each driver's physical microstep setting. The driver settings default to 200 full steps/revolution and 1/32 microstepping, matching the reported hardware. The default 300 × 300 mm travel is only a starting value; set it to the machine's actual limits before moving. For example, a 200-step motor, 1/16 microstepping, and a 20-tooth GT2 pulley gives `200 × 16 / (20 × 2) = 80 steps/mm`.
5. Move the carriage physically to the desired top-left machine origin with all pens raised, connect, and press **Zero current position**. A reset or power loss clears this origin.
6. Set each servo's up/down angles and test one pen at a time. Enter each pen tip's measured X/Y offset from the reference carriage position. A positive physical X offset makes the carriage move farther in negative X to align that tip; verify with a small test mark.
7. In **Studio**, use **Add images**, drop image files onto the canvas, or paste a copied image with Command-V. The canvas starts empty and covers the full calibrated X/Y travel from machine zero. Drag images, use the lower-right handle or **Scale** control to resize, or choose **Fit to canvas** for the selected image. Use **Send back**/**Bring front** to set overlap order. Images can extend past the canvas edges; only the part within machine travel is plotted. Select an image on the canvas or Layers list to adjust its position, overall alpha, and red/blue/green/black alpha separately.
8. The canvas dimensions come from **Calibration → X travel / Y travel**. Choose **X → / Y ↓** or **Rotate 90°** to rotate the view; existing artwork and the live cursor retain their physical machine coordinates. Select a resolution preset or set line spacing from 0.2 to 5 mm; smaller spacing creates a finer grid and more plot instructions. Set the X and Y maximum motor RPM, choose **Four pens** or **Monochrome**, then **Generate plot preview**. Inspect the result before **Export** or **Start plotting**. Regenerate after changing RPM; calibration jogs use the RPM setting immediately.

The canvas represents the entire machine, with no A4 size requirement and no page-origin offset. For example, X travel of 250 mm and Y travel of 500 mm produces a 250 × 500 mm canvas, or 500 × 250 mm when the view is rotated. Travel changes resize the canvas immediately. Place actual paper within the machine and arrange artwork over it. Image regions outside the canvas are cropped. Pen tip offsets are still applied to carriage commands: a nonzero tip offset can put edge artwork beyond carriage travel. The app shows the preview and asks you to move that artwork inward or correct the calibration before plotting or exporting. White and transparent image areas do not cover lower layers. An image's color alpha changes its contribution to the composition before the final four-color conversion; the pens themselves do not print translucent ink. In **Four pens** mode, red, blue, and green each have a minimum color-match threshold; higher values use that pen less. The black threshold sets the minimum darkness for pixels not matched to a color. In **Monochrome**, choose one pen and a brightness cutoff: pixels at or below the cutoff are drawn with that pen, and lighter pixels stay blank. Quick presets are **Portrait Pictures (120)**, **PCB Sketching (225)**, and **Objects (170)**. Selecting a preset saves the threshold and invalidates the generated instructions; regenerate to see its final effect. A manually selected exact preset value also highlights its button. The blue preview color matches the logo's `#63959E`.

While connected, the machine canvas and plot preview show a crosshair at the estimated carriage position and a colored dot at the active pen tip. The coordinates below the page use the calibrated steps/mm, active pen offset, and view rotation. A marker beyond machine travel is hidden and its readout says **outside travel**. The Uno reports commanded step counts, so these markers cannot reveal missed steps or a motor that is electrically enabled but not moving.

The planner classifies the canvas at the selected resolution and preserves the colored cell centers. It follows nested contours through filled regions and treats diagonal neighbors as continuous ink, so a thin diagonal is a stroke instead of many separate pen dabs. Branches start from available endpoints; pen settling times and motor speed guide how much short retracing is allowed to avoid a lift. The connector search has bounded length and work. Its centerline never enters a blank cell interior; exact contact at a shared diagonal cell corner is allowed. Filled regions stay filled and white holes remain blank.

**Maximum smoothing error** controls path simplification in millimeters (default 0.1 mm). Every removed cell-center corner must remain within that distance of its replacement segment, and the error is additionally capped at 0.45 of a plot cell. Smaller values retain more corners and generate more MOVE commands. Existing strokes are simplified once, so joining does not compound their error. This describes the requested geometry; actual pen width, mechanical accuracy, and missed steps still affect the print. Features smaller than the selected sampling pitch may disappear during image resampling. Use **Detail** (0.25 mm) for finer sampling; the full 250 × 500 mm canvas then uses 1,000 × 2,000 cells.

A spatial endpoint index selects nearby strokes for all job sizes, including drawings with thousands of disconnected details. Open strokes can reverse to use the nearer end. NumPy, when installed, accelerates palette classification and layer alpha processing in bounded chunks with the same threshold and pixel results; a scalar fallback keeps it optional. Faint grayscale detail accepted by the monochrome threshold survives composition even when the four-color black threshold would reject it.

The plot preview shows the final planned paths, drawing distance, pen-up travel, and minimum plotting duration. This minimum assumes a start at logical machine zero and immediate motion at the configured RPM, plus pen settling times; easing, corner braking, serial delays, and mechanical limits add time. Generation stops with a clear message if a canvas exceeds 2,000,000 cells or a job exceeds 600,000 commands. The exported instructions use absolute machine coordinates and per-pen tip offsets. Regenerate after changing the canvas, an image, alpha, smoothing, or palette settings. Export opens a native save dialog in Downloads and accepts both macOS single-string paths and tuple paths; files without an extension receive `.txt`.

For developer comparisons, run `python3 tests/benchmark_planner.py --modules <module-folder> --output <results.json>`. It measures fixed diagonal, trace/text, filled-hole, dot, and PCB fixtures without connecting to hardware. Its duration calculation assumes 120 RPM, 800 steps/mm, 200 full steps/revolution, 1/32 microstepping, and 240 ms pen settling.

## Cooling fan

In **Calibration → Cooling fan**, choose **Automatic** or **Manual**. The relay signal is Uno D9: HIGH turns it on, LOW turns it off. The fan starts off after a reset. The selected mode is saved by the app; reconnecting in manual mode starts with fan power off.

Automatic mode repeats **2,000 completed plot instructions OFF, then 3,000 ON**. It switches on at instruction 2,000, off at 5,000, on again at 7,000, and continues the cycle. Each plot resets the counter. Finishing, stopping, or aborting a plot turns automatic cooling off. Calibration actions and serial queries do not advance the cycle. Queue admission does not count: a queued move counts only when it finishes. Setup and pen commands in the generated job count once when executed.

Manual mode provides an on/off switch that works while idle or plotting. Its power setting survives plot completion and Stop until changed or the Uno resets. Switching back to automatic resumes the current plot's cycle; outside a plot it turns the fan off. The panel displays relay state and cycle progress. This state reports the output command, not a physical fan speed sensor.

## Serial protocol

The host uses 115200 baud, ASCII lines ending in `\n`. The firmware identifies itself with `STATUS proto=5 ... queue=32 idle_ms=5000 fan_pin=9 ...`; the app refuses to connect to an older plotter sketch. Successful ordinary commands return `OK`. `MOVE` and `JOG` then return `DONE` when motion finishes; interrupted moves return `ABORT`. Rejected commands return `ERR reason`. `STATUS` and the compact `POS` position report are allowed during a move and serve as heartbeats.

`JOB BEGIN` resets and starts instruction counting; `JOB END` ends the cycle. Citadela wraps streaming and exported jobs in these commands. `FAN AUTO` and `FAN MANUAL ON|OFF` work during queued moves and return `FAN mode=AUTO|MANUAL on=0|1 count=<completed> active=0|1` instead of `OK`, so fan controls cannot be mistaken for move acknowledgements. `STATUS` also includes `fan_mode`, `fan_on`, `fan_count`, and `fan_active`. Fan transitions use deferred reports and add no delay or cooling pause to the plot.

Example:

```text
FAN AUTO
ZERO
JOB BEGIN
LIMIT 16800 23760
SERVO 45 95 95 95 95
RAMP 1067 300 25
ENABLE ON
PEN UP
MOVE 1600 1600 1200
PEN RED
MOVE 4800 1600 1200
PEN UP
ENABLE OFF
JOB END
```

`MOVE` uses **absolute step coordinates**. `JOG` uses signed relative step coordinates. `LIMIT` gives the maximum allowed X/Y step coordinates. For plotting, Citadela sends consecutive moves as `QUEUE <id> <x> <y> <feed>` commands. The Uno replies `ACK <id>` when a move is admitted and `DONE <id>` after it completes. Citadela keeps at most 32 moves outstanding, then waits for every completion before a pen or setup command. `STOP` discards queued moves, interrupts motion, raises all pens, and disables both drivers. `ENABLE OFF` only releases the stepper drivers, so servo calibration can leave a pen lowered. The firmware feed is major-axis steps per second, 1–24000. **RPM limits** accept 1–600 RPM, with the actual requested speed capped by STEP throughput. At 200 full steps/revolution, the 24,000-pulse/s ceiling corresponds to 225 RPM at 1/32 microstepping, 450 RPM at 1/16, and 900 RPM at 1/8 (the app limit remains 600). These are target pulse rates, not a measured guarantee that the motor can reach them. Citadela converts each motor's RPM limit to STEP pulses per second as `RPM × full steps/revolution × microsteps/full step ÷ 60`, then chooses a coordinated feed that keeps both axes within their RPM limits. At 200 full steps/revolution and 1/32 microstepping, 120 RPM requires 12,800 pulses/s and 225 RPM requires 24,000 pulses/s. Motion firmware v5 provides eased acceleration and braking, a dedicated Timer2 pulse scheduler, and braking lookahead through compatible queued moves; the Servo library continues to use Timer1. Consecutive compatible segments retain velocity instead of restarting each MOVE. Sharp turns slow according to **Corner speed**, reversals and queue exhaustion return to the gentle start rate, and STOP still raises pens and releases drivers. The scheduler keeps the STEP pulse HIGH for at least 10 microseconds and allows DIR setup time. Pulse delivery and high-speed torque need physical verification before selecting higher RPM on the loaded machine.

**Calibration → Eased acceleration** sets the gentle starting RPM (default 10), ramp duration (default 300 ms), and corner speed percentage (default 25). The idle-only `RAMP <start_pulses_per_second> <duration_ms> <corner_percent>` command configures the board for both generated jobs and jogs. Short moves may never reach the configured RPM. Exported jobs include RAMP. The app requires `STATUS proto=5 max_feed=24000 accel=1` with the existing queue/fan features; upload the matching sketch before connecting the updated app.

The 24,000-pulse/s ceiling was selected from the compiled Uno interrupt budget. The worst reviewed ordinary rise/fall pair uses 435 cycles, about 65% of the 16 MHz CPU at that rate, leaving time for the ramp controller, queued commands, serial traffic, and servo interrupts. Rate division occurs outside interrupt locks; the controller updates every 4 ms and avoids recalculating unchanged cruise intervals. Native tests cover the actual queue/ISR logic, endpoints, eased continuity, reversal braking, STOP, minimum pulse/setup intervals, and clock wrap. This analysis does not measure physical pulse frequency under load or motor RPM. The uploaded board was checked using STATUS and RAMP with drivers disabled; no motion was requested during that verification.

### DM556 microstepping

The reported driver is a **DM556**. Its PUL/DIR/ENA inputs accept motion and enable commands; they do not select microstep resolution. The genuine Leadshine model uses SW5–SW8 DIP switches or a separate ProTuner programming connection, as described in the [DM556 manual, sections 3 and 7](https://www.leadshine.com/upfiles/downloads/2c28eb55103c7f769867f3338c14808f_1660188467317.pdf). Citadela does not change the driver's internal settings through the present STEP/DIR/ENABLE wiring. Variant and clone switch tables may differ: match the table printed on the actual driver. Some full-step/software modes require a programming interface rather than a dedicated DIP combination.

The calibration dropdowns now match the table photographed on the user's driver. These are **input pulses per motor revolution for a 200-full-step motor**. The lowest printed option is half stepping, 400 pulses/revolution; this driver's table has no full-step DIP option. SW5–SW8 are resolution switches, not RPM switches. Use the switch bank's printed ON label to identify the ON direction.

| Pulses/revolution | Microsteps/full step | SW5 | SW6 | SW7 | SW8 |
| --- | --- | --- | --- | --- | --- |
| 400 | 2 | OFF | ON | ON | ON |
| 800 | 4 | ON | OFF | ON | ON |
| 1,600 | 8 | OFF | OFF | ON | ON |
| 3,200 | 16 | ON | ON | OFF | ON |
| 6,400 | 32 | OFF | ON | OFF | ON |
| 12,800 | 64 | ON | OFF | OFF | ON |
| 25,600 | 128 | OFF | OFF | OFF | ON |
| 1,000 | 5 | ON | ON | ON | OFF |
| 2,000 | 10 | OFF | ON | ON | OFF |
| 4,000 | 20 | ON | OFF | ON | OFF |
| 5,000 | 25 | OFF | OFF | ON | OFF |
| 8,000 | 40 | ON | ON | OFF | OFF |
| 10,000 | 50 | OFF | ON | OFF | OFF |
| 20,000 | 100 | ON | OFF | OFF | OFF |
| 25,000 | 125 | OFF | OFF | OFF | OFF |

With the driver power off, change both drivers to the chosen physical setting, then record that same setting for each axis in Calibration. The app scales that axis's steps/mm proportionally: the existing 800 steps/mm at 6,400 pulses/revolution becomes 50 at 400, 100 at 800, or 200 at 1,600. Their respective requested position increments are 0.02, 0.01, and 0.005 mm; these are pulse increments, not measured mechanical accuracy. At 600 RPM they require only 4,000, 8,000, and 16,000 input pulses/s. **400 pulses/revolution provides the most controller speed headroom; 800 retains twice its input resolution.** Keep the gentle start and acceleration ramp enabled. Achievable RPM depends on motor torque, supply voltage, carriage load, and resonance; fewer input pulses do not establish a safe mechanical maximum.

Steps/mm supports up to 20,000; firmware coordinate limits support up to 20,000,000 steps per axis. Changing only the app field while leaving the physical driver unchanged changes drawing size and makes its coordinates incorrect. Return the carriage to the physical origin, zero it, and regenerate after a resolution change. The app cannot flip the physical switches during a plot.

If the Uno rejects the new travel limits after changing resolution, its old pulse position may exceed the new maximum pulse coordinates. For example, X=92,164 cannot fit a new X limit of 50,000. With pens raised and drivers disabled, return the carriage to the physical machine origin, press **Zero current position**, and regenerate. Zero clears the counters without moving the carriage; pressing it at an arbitrary location would shift the physical canvas origin. Calibration position readouts are pulse counts labeled **steps**; the canvas cursor uses millimeters.

The photographed current table starts at **1.4 A peak**, while the motor was reported as **0.62 A**. Keep SW1–SW3 unchanged and verify the motor's phase-current rating and wiring against the driver's output rating before a loaded speed test; even the lowest printed peak setting may exceed the stated motor rating. SW4 OFF selects half standstill current according to this particular label, and does not select half stepping or reduce the moving-current setting. Firmware still disables both drivers when idle. Increasing current is not part of this speed update.

In **Calibration**, the pen down and pen up settling times default to 240 ms each. These waits are paid once per path, so they can dominate detailed plots. Lower them only after checking that each pen reaches the paper and fully clears it before carriage travel. Upload `plotter_firmware/plotter_firmware.ino` after updating the app; protocol v5 requires the matching sketch.

## Files

- `citadela.py` — branded desktop launcher.
- `plotter_app.py` — desktop window, serial connection, job streaming, calibration API.
- `plotter_core.py` — image conversion, preview, instruction generation, and time/distance estimates.
- `plotter_paths.py` — connected contour trails, bounded ink-only joins, and detail-preserving simplification.
- `plotter_routing.py` — spatial stroke endpoint routing.
- `plotter_pixels.py` — optional accelerated palette classification.
- `web/` — HTML, CSS, and JavaScript interface.
- `web/citadela-logo.png` — supplied logo used for the app and window icon.
- `plotter_firmware/plotter_firmware.ino` — Uno firmware for both axes, four servos, and the D9 fan relay.
- `plotter_firmware/MotionProfile.h` — eased profile, queue junction math, and coordinated step geometry shared with native tests.
- `plotter_firmware/FanCycle.h` — instruction-based cooling cycle shared with the firmware tests.
- `stepper_app.py`, `serial_stepper_controller.ino` — original single-axis calibration project, retained for reference.
