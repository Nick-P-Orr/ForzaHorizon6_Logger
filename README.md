# ForzaHorizon6_Logger

Tools for reading Forza Horizon 6's **"Data Out"** UDP telemetry — the binary
struct the game streams once per frame (~60 Hz) to a configurable IP/port.

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
| [`web/index.html`](web/index.html) | Dashboard UI — gauges, gear, throttle/brake, tire temps, charts. |
| [`forza_telemetry.py`](forza_telemetry.py) | Packet parser + UDP listener; also a standalone console readout. |
| [`capture.py`](capture.py) | Raw packet recorder for reverse-engineering the layout. |

## Live dashboard

Browser-based gauges, gear/throttle/brake, tire temps, and rolling speed/RPM charts.

```sh
python3 server.py --port 5300        # listen for the game on UDP 5300
python3 server.py --port 5300 --demo # try it with fake data, no game needed
```

Then open <http://localhost:8000>.

## Console readout

Zero-frills live line — quickest way to confirm telemetry is flowing.

```sh
python3 forza_telemetry.py --port 5300
```

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

> FH6's exact layout is unconfirmed until checked against a real capture. If it
> differs from the known layouts, the Sled fields (including speed and RPM) stay
> correct; use `capture.py` to pin down the new Dash offset.
