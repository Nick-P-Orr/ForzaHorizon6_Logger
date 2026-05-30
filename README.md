# ForzaHorizon6_Logger

Tools for reading Forza Horizon 6's **"Data Out"** UDP telemetry — the binary
struct the game streams every frame (~111 Hz, measured) to a configurable IP/port.

## Enable telemetry in-game

Settings → HUD and Gameplay → **Telemetry**

| Setting | Value |
| --- | --- |
| Data Out | **On** |
| Data Out IP Address | your computer's LAN IP |
| Data Out IP Port | a port, e.g. `5300` |

Find your IP — macOS: `ipconfig getifaddr en0` · Windows: `ipconfig`.
Use the same port number when running the tools below. No dependencies beyond
Python 3.8+ (standard library only).

## Repo layout

| File | What it does |
| --- | --- |
| [`server.py`](server.py) | Live browser dashboard (HTTP + Server-Sent Events). |
| [`web/index.html`](web/index.html) | Dashboard UI — configurable widgets, ☰ menu to toggle any value. |
| [`forza_telemetry.py`](forza_telemetry.py) | Packet parser + UDP listener; also a standalone console readout. |
| [`logger.py`](logger.py) | Decode every field and log it to CSV/JSONL. |
| [`capture.py`](capture.py) | Raw packet recorder for reverse-engineering the layout. |
| [`sample_telemetry.csv`](sample_telemetry.csv) | 275-row sample logged from a real FH6 session (driving + idle). |

## Live dashboard

Browser-based, fully configurable. Every Forza value is available as a widget;
pick which ones to show from the **☰ menu** (top-left). Your selection is saved in
the browser (localStorage), so it persists across reloads.

```sh
python3 server.py --port 5300        # listen for the game on UDP 5300
python3 server.py --port 5300 --demo # try it with fake data, no game needed
```

Then open <http://localhost:8000>.

Each value uses a visualization suited to it:

| Widget type | Used for |
| --- | --- |
| Circular gauge | Speed, RPM, power, torque |
| Rolling line graph | Speed and RPM over time (auto-scaling); throttle and brake as separate 30 s input traces |
| Vertical bar | Clutch, handbrake, fuel |
| Centered bidirectional bar | Steering, boost (signed values) |
| 2×2 wheel grid | Tire temps/slip, suspension, per-wheel flags (color-coded) |
| G-G plot | Lateral/longitudinal g-force |
| Track map | Top-down position trail (from world X/Z) |
| Large readout | Gear, lap times, race position, car class/PI, etc. |
| LED indicator | Booleans like "race active" |

The ☰ menu groups widgets (Speed & Engine, Drivetrain, Inputs, Tires & Wheels,
Suspension, Chassis/Motion, Position, Lap & Race, Car Info, Status) and has
**Defaults / All on / All off** shortcuts. Fourteen sensible widgets are on by
default; all ~49 are available.

## Console readout

Zero-frills live line — quickest way to confirm telemetry is flowing.

```sh
python3 forza_telemetry.py --port 5300
```

## Logging every value to a file

Decodes each packet into all its fields and writes one row per packet — the file
header is the complete list of available values. CSV by default (open it in any
spreadsheet); JSONL optional.

```sh
python3 logger.py --port 5300                 # -> forza_log_<timestamp>.csv
python3 logger.py --port 5300 --format jsonl  # newline-delimited JSON
python3 logger.py --port 5300 --racing-only   # skip menu/paused packets
python3 logger.py --list-fields               # print every field + notes, no logging
python3 logger.py --demo --duration 5         # log fake data, no game needed
```

Run `python3 logger.py --list-fields` to see all 91 values with short
descriptions, grouped into the stable **Sled** block, the auto-detected **Dash**
block, and the **derived** values this tool computes (speed in km/h/mph, gear
label, etc.). Log files you create (`forza_log_*.csv` / `.jsonl`) are git-ignored;
a small committed [`sample_telemetry.csv`](sample_telemetry.csv) shows the format.

## Available telemetry fields

Every value the logger writes (one column per field), with the example/range
columns taken from a real ~8-minute FH6 session (53,369 packets). The packet
splits into a stable **Sled** block (bytes 0–231, identical across all Forza
titles) and a **Dash** block (auto-detected at offset 244 for FH6). Per-wheel
fields appear four times with suffixes `_fl _fr _rl _rr` (front/rear, left/right).

### Metadata & derived

| Field | Type | Example | Notes |
| --- | --- | --- | --- |
| `recv_time` | float | `1780064700.637` | Unix time packet was received (added by logger) |
| `is_race_on` | int | `1` | 1 while driving, 0 in menus/paused |
| `timestamp_ms` | uint | `49896812` | Game clock (ms), wraps around |
| `dash_base` | int | `244` | Detected Dash-block offset (244 = Horizon layout) |
| `is_racing` | bool | `True` | Boolean form of `is_race_on` |
| `speed_kmh` | float | `143.50` | Speed from velocity vector — range `0 … 304` |
| `speed_mph` | float | `89.16` | Same, mph — range `0 … 189` |
| `gear_label` | str | `3` | Gear as text (`R`, `1`…`10`) |

### Engine

| Field | Type | Example | Range / notes |
| --- | --- | --- | --- |
| `engine_max_rpm` | float | `8600.0` | Redline |
| `engine_idle_rpm` | float | `800.0` | Idle RPM |
| `current_engine_rpm` | float | `7072.2` | `0` at idle/inactive, up to `7970` observed (redline `8600`) |
| `power` | float | `331733.5` | Watts; `-161913 … 338456` (negative = engine braking) |
| `torque` | float | `448.0` | Nm; `-195 … 501` |
| `boost` | float | `14.49` | Boost pressure; `-14.7 … 14.5` |

### Motion (vectors are car-relative: x = lateral, y = vertical, z = forward)

| Field | Type | Example | Notes |
| --- | --- | --- | --- |
| `acceleration_x/y/z` | float | `0.18 / -0.27 / 4.06` | m/s² |
| `velocity_x/y/z` | float | `0.03 / -0.30 / 39.86` | m/s |
| `angular_velocity_x/y/z` | float | `0.009 / -0.003 / -0.018` | Roll/yaw/pitch rate (rad/s) |
| `yaw` / `pitch` / `roll` | float | `1.599 / -0.025 / -0.0001` | Orientation (rad) |
| `speed` | float | `39.86` | Game-reported speed (m/s) |
| `position_x/y/z` | float | `-6958.97 / 159.37 / -1863.32` | World coordinates |

### Per-wheel (each ×4: `_fl _fr _rl _rr`)

| Field | Type | Example (FL) | Notes |
| --- | --- | --- | --- |
| `norm_susp_travel_*` | float | `0.425` | Suspension travel, normalized 0–1 |
| `susp_travel_m_*` | float | `-0.0029` | Suspension travel (meters) |
| `tire_slip_ratio_*` | float | `0.148` | Longitudinal slip (0 = full grip) |
| `tire_slip_angle_*` | float | `-0.0033` | Lateral slip angle (0 = full grip) |
| `tire_combined_slip_*` | float | `0.148` | Combined slip magnitude |
| `wheel_rot_speed_*` | float | `126.4` | Wheel angular speed (rad/s) |
| `tire_temp_*` | float | `166.1` | Tire temp; `0 … 224` observed |
| `wheel_on_rumble_*` | int | `0` | On rumble strip (0/1) |
| `wheel_in_puddle_*` | float | `0.0` | Depth in puddle (0–1) |
| `surface_rumble_*` | float | `0.0` | Surface rumble feedback |

### Car spec

| Field | Type | Example | Notes |
| --- | --- | --- | --- |
| `car_ordinal` | int | `269` | Unique car ID |
| `car_class` | int | `3` | Class 0–7 (D, C, B, A, S1, S2, X) → `3` = A |
| `car_performance_index` | int | `661` | PI, 100–999 |
| `drivetrain_type` | int | `2` | 0 = FWD, 1 = RWD, 2 = AWD |
| `num_cylinders` | int | `6` | Engine cylinder count |

### Driver inputs

| Field | Type | Example | Notes |
| --- | --- | --- | --- |
| `accel` | uint8 | `255` | Throttle, 0–255 |
| `brake` | uint8 | `0` | Brake, 0–255 |
| `clutch` | uint8 | `0` | Clutch, 0–255 |
| `handbrake` | uint8 | `0` | Handbrake, 0–255 |
| `gear` | uint8 | `3` | Gear (0 = reverse); `0 … 11` observed |
| `steer` | int8 | `0` | Steering, -127 … 127 |
| `normalized_driving_line` | int8 | `63` | Driving-line indicator |
| `normalized_ai_brake_difference` | int8 | `0` | AI brake-difference indicator |
| `fuel` | float | `1.0` | Fuel fraction, 0–1 |

### Lap & race (zeros in free-roam; populated during races)

| Field | Type | Example | Notes |
| --- | --- | --- | --- |
| `distance_traveled` | float | `0.0` | Distance this session (m) |
| `best_lap` / `last_lap` / `current_lap` | float | `0.0` | Lap times (s) |
| `current_race_time` | float | `1669.26` | Total session/race time (s) |
| `lap_number` | uint16 | `0` | Current lap |
| `race_position` | uint8 | `0` | Race position |

> Note on units: in this FH6 session `tire_temp_*` ranged `0 … 224` — higher than
> the ~80–120 °C typical of earlier Forza titles, so FH6 may report tire temp on
> a different scale. The value is real and tracks driving; treat the unit as
> unconfirmed.

## Raw capture (format reverse-engineering)

Records every packet verbatim and reports its size, so we can confirm FH6's
exact layout against a real session.

```sh
python3 capture.py --port 5300
```

Output is a `.fhcap` file (git-ignored). Each record is a little-endian header —
`double` receive timestamp + `uint32` payload length — followed by the raw packet
bytes. Common Forza packet sizes and their layouts:

| Bytes | Layout |
| --- | --- |
| 232 | Sled only |
| 311 | Car Dash (Forza Motorsport 7) |
| 324 | Car Dash + Horizon HUD extension (FH4 / FH5) |
| 331 | Car Dash (Forza Motorsport 2023) |

## How the parser handles FH6

The packet has two parts:

* **Sled block** (bytes 0–231) — engine RPM, the acceleration/velocity/
  angular-velocity vectors, per-wheel suspension and slip, car ordinal/class/PI.
  This block is **identical across every Forza title**, so these fields are
  trusted as-is. Speed is derived from the velocity vector, making it reliable
  on FH6 even before anything else is confirmed.
* **Dash block** (position, speed, power, torque, tire temps, boost, fuel, lap
  times, gear, throttle/brake/steer) — its starting offset has shifted between
  titles (244 in Horizon, 232 in Motorsport). The parser **auto-detects** it by
  scoring candidate offsets for plausibility, so it adapts if FH6 differs. The
  detected offset is shown in the dashboard and console output (e.g. `dash@244`).

> **Confirmed against a real FH6 session:** packets arrive at ~111 Hz (≈9 ms apart) and parse
> cleanly with the Dash block auto-detected at offset **244** (the FH4/FH5 Horizon
> layout). All fields above were validated against ~53k real packets. If a future
> update shifts the layout, the Sled fields (including speed and RPM) stay correct;
> use `capture.py` to pin down the new Dash offset.

## Tech stack

Guiding constraint: **Python 3.8+ standard library only, no pip installs, no
frontend toolchain.** Clone it and run with the Python that already ships on
your machine.

### Backend (Python stdlib)

| Module | Role |
| --- | --- |
| `socket` | Binds the UDP port Forza streams Data Out to. |
| `struct` | Unpacks the fixed-size binary C-struct at known byte offsets into named fields. |
| `http.server` (`ThreadingHTTPServer`) | Serves the dashboard page and the SSE stream — no Flask/FastAPI. |
| `threading` | One thread receives UDP and writes the latest reading into shared state (lock-guarded); HTTP threads read it. Decoupled so a slow browser never stalls packet capture. |
| `csv` / `json` | The logger writes CSV (default) or JSONL, one row per packet. |
| `argparse`, `dataclasses`, `pathlib` | CLI flags, the `TelemetryPacket` dataclass, file paths. |

### Frontend (vanilla, no libraries)

| Tech | Role |
| --- | --- |
| One self-contained `web/index.html` | All markup, CSS, and JS inline. No React, no Chart.js, no bundler. |
| **Server-Sent Events** (`EventSource`) | Browser opens `/stream` and the server pushes JSON updates at 30 Hz over a single long-lived HTTP connection. Simpler than WebSockets, one-directional, which is all we need (server → browser). |
| **`<canvas>` 2D** | Every gauge, line trace, G-G plot, and track map is hand-drawn with the canvas API. |
| **`localStorage`** | Remembers which widgets you've toggled on (`fh6_widgets` key). |

### Data flow

```
Forza Horizon 6
   │  UDP binary packets (~111 Hz)
   ▼
forza_telemetry.py  ── parse() unpacks the C-struct ──┐
   │                                                  │
   ├──► server.py ──► /stream (SSE, 30 Hz JSON) ──► web/index.html (canvas widgets)
   │
   └──► logger.py ──► CSV / JSONL on disk
```

### Why these choices

The stdlib-only constraint means it runs anywhere Python does with no setup;
binary packet parsing is exactly what `struct` is built for; and SSE + canvas
gives a real-time UI without a JS toolchain. The tradeoff: hand-drawn canvas
widgets are more code than dropping in a charting library, and SSE is one-way
(fine for live readout; a future record/replay control channel may want a
second endpoint).

## TODOs

- Make this usable on an iPad
- ~~Make values persist when the game pauses or a race ends~~ (done)
- Make the "last lap time" save the last five laps
- Overlay the braking and throttle traces onto the track map
- Telemetry record feature — save a race of telemetry, add a replay feature later
- G-forces are inverted?
