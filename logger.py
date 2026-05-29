#!/usr/bin/env python3
"""Decode and log every Forza Horizon 6 "Data Out" field to a file.

Unlike capture.py (which records raw bytes for reverse-engineering), this logs
the *parsed* values: one row per packet, one column per field. The CSV header is
the complete list of available values, so opening the file (or running
`--list-fields`) shows you everything the telemetry exposes.

Run:
    python3 logger.py --port 5300                  # -> forza_log_<timestamp>.csv
    python3 logger.py --port 5300 --format jsonl   # newline-delimited JSON
    python3 logger.py --port 5300 --racing-only    # skip menu/paused packets
    python3 logger.py --list-fields                # print all fields, don't log
    python3 logger.py --demo --duration 5          # log fake data, no game

In FH6: Settings > HUD and Gameplay > Telemetry
    Data Out = On, IP = this machine's LAN IP, Port = matching --port.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import signal
import socket
import time
from datetime import datetime

import forza_telemetry as ft

# Column order for logs: receive time, then every Sled field, every Dash field,
# then the derived convenience values to_dict() adds. This list IS the full set
# of available values.
SLED_NAMES = [n for n, _, _ in ft.SLED_FIELDS]
DASH_NAMES = [n for n, _, _ in ft.DASH_FIELDS]
DERIVED_NAMES = ["speed_kmh", "speed_mph", "gear_label", "is_racing", "dash_base"]
COLUMNS = ["recv_time"] + SLED_NAMES + DASH_NAMES + DERIVED_NAMES

# Short, human-readable notes for --list-fields output.
FIELD_NOTES = {
    "recv_time": "Unix time the packet was received (added by logger)",
    "is_race_on": "1 while driving, 0 in menus/paused",
    "timestamp_ms": "Game timestamp (ms), wraps around",
    "engine_max_rpm": "Redline RPM",
    "engine_idle_rpm": "Idle RPM",
    "current_engine_rpm": "Current RPM",
    "acceleration_x": "Accel right/left (m/s^2), car-relative",
    "acceleration_y": "Accel up/down (m/s^2)",
    "acceleration_z": "Accel forward/back (m/s^2)",
    "velocity_x": "Velocity right/left (m/s)",
    "velocity_y": "Velocity up/down (m/s)",
    "velocity_z": "Velocity forward/back (m/s)",
    "angular_velocity_x": "Roll rate (rad/s)",
    "angular_velocity_y": "Yaw rate (rad/s)",
    "angular_velocity_z": "Pitch rate (rad/s)",
    "yaw": "Yaw orientation (rad)",
    "pitch": "Pitch orientation (rad)",
    "roll": "Roll orientation (rad)",
    "norm_susp_travel_fl": "Normalized suspension travel 0..1 (front-left)",
    "norm_susp_travel_fr": "Normalized suspension travel 0..1 (front-right)",
    "norm_susp_travel_rl": "Normalized suspension travel 0..1 (rear-left)",
    "norm_susp_travel_rr": "Normalized suspension travel 0..1 (rear-right)",
    "tire_slip_ratio_fl": "Tire slip ratio, 0=grip (front-left)",
    "tire_slip_ratio_fr": "Tire slip ratio, 0=grip (front-right)",
    "tire_slip_ratio_rl": "Tire slip ratio, 0=grip (rear-left)",
    "tire_slip_ratio_rr": "Tire slip ratio, 0=grip (rear-right)",
    "wheel_rot_speed_fl": "Wheel angular speed rad/s (front-left)",
    "wheel_rot_speed_fr": "Wheel angular speed rad/s (front-right)",
    "wheel_rot_speed_rl": "Wheel angular speed rad/s (rear-left)",
    "wheel_rot_speed_rr": "Wheel angular speed rad/s (rear-right)",
    "wheel_on_rumble_fl": "On rumble strip 0/1 (front-left)",
    "wheel_on_rumble_fr": "On rumble strip 0/1 (front-right)",
    "wheel_on_rumble_rl": "On rumble strip 0/1 (rear-left)",
    "wheel_on_rumble_rr": "On rumble strip 0/1 (rear-right)",
    "wheel_in_puddle_fl": "Depth in puddle 0..1 (front-left)",
    "wheel_in_puddle_fr": "Depth in puddle 0..1 (front-right)",
    "wheel_in_puddle_rl": "Depth in puddle 0..1 (rear-left)",
    "wheel_in_puddle_rr": "Depth in puddle 0..1 (rear-right)",
    "surface_rumble_fl": "Surface rumble feedback (front-left)",
    "surface_rumble_fr": "Surface rumble feedback (front-right)",
    "surface_rumble_rl": "Surface rumble feedback (rear-left)",
    "surface_rumble_rr": "Surface rumble feedback (rear-right)",
    "tire_slip_angle_fl": "Tire slip angle, 0=grip (front-left)",
    "tire_slip_angle_fr": "Tire slip angle, 0=grip (front-right)",
    "tire_slip_angle_rl": "Tire slip angle, 0=grip (rear-left)",
    "tire_slip_angle_rr": "Tire slip angle, 0=grip (rear-right)",
    "tire_combined_slip_fl": "Combined slip, 0=grip (front-left)",
    "tire_combined_slip_fr": "Combined slip, 0=grip (front-right)",
    "tire_combined_slip_rl": "Combined slip, 0=grip (rear-left)",
    "tire_combined_slip_rr": "Combined slip, 0=grip (rear-right)",
    "susp_travel_m_fl": "Suspension travel meters (front-left)",
    "susp_travel_m_fr": "Suspension travel meters (front-right)",
    "susp_travel_m_rl": "Suspension travel meters (rear-left)",
    "susp_travel_m_rr": "Suspension travel meters (rear-right)",
    "car_ordinal": "Unique car id",
    "car_class": "Class 0..7 (D..X)",
    "car_performance_index": "PI 100..999",
    "drivetrain_type": "0=FWD, 1=RWD, 2=AWD",
    "num_cylinders": "Engine cylinder count",
    "position_x": "World position X (Dash block)",
    "position_y": "World position Y",
    "position_z": "World position Z",
    "speed": "Game-reported speed (m/s)",
    "power": "Engine power (watts)",
    "torque": "Engine torque (Nm)",
    "tire_temp_fl": "Tire temp degC (front-left)",
    "tire_temp_fr": "Tire temp degC (front-right)",
    "tire_temp_rl": "Tire temp degC (rear-left)",
    "tire_temp_rr": "Tire temp degC (rear-right)",
    "boost": "Boost pressure",
    "fuel": "Fuel fraction 0..1",
    "distance_traveled": "Distance this session (m)",
    "best_lap": "Best lap time (s)",
    "last_lap": "Last lap time (s)",
    "current_lap": "Current lap time (s)",
    "current_race_time": "Total race time (s)",
    "lap_number": "Current lap number",
    "race_position": "Race position",
    "accel": "Throttle 0..255",
    "brake": "Brake 0..255",
    "clutch": "Clutch 0..255",
    "handbrake": "Handbrake 0..255",
    "gear": "Gear (0=reverse)",
    "steer": "Steering -127..127",
    "normalized_driving_line": "Driving-line indicator",
    "normalized_ai_brake_difference": "AI brake-difference indicator",
    "speed_kmh": "Speed km/h (derived from velocity vector)",
    "speed_mph": "Speed mph (derived from velocity vector)",
    "gear_label": "Gear as text (R, 1..10)",
    "is_racing": "Bool form of is_race_on",
    "dash_base": "Detected Dash-block offset (244/232)",
}


def list_fields() -> None:
    def show(title, names):
        print(f"\n{title} ({len(names)} fields)")
        print("-" * len(title))
        for n in names:
            print(f"  {n:<32} {FIELD_NOTES.get(n, '')}")
    print(f"All {len(COLUMNS)} logged values:")
    show("Logger", ["recv_time"])
    show("Sled block (stable across all Forza titles)", SLED_NAMES)
    show("Dash block (auto-detected offset)", DASH_NAMES)
    show("Derived (computed by this tool)", DERIVED_NAMES)


def _open_writer(path: str, fmt: str):
    f = open(path, "w", newline="")
    if fmt == "csv":
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        return f, w
    return f, None  # jsonl writes raw lines


def _demo_socket(port: int):
    """Spawn a thread that streams synthetic packets to localhost:port."""
    import threading

    def pump():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t0 = time.time()
        while True:
            t = time.time() - t0
            s.sendto(ft.build_synthetic(
                current_engine_rpm=1500 + 5500 * (0.5 + 0.5 * math.sin(t * 1.7)),
                velocity_x=20 + 18 * (0.5 + 0.5 * math.sin(t * 0.6)),
                velocity_y=0.0, velocity_z=0.0,
                gear=1 + int((math.sin(t * 0.6) + 1) / 2 * 6),
            ), ("127.0.0.1", port))
            time.sleep(1 / 60)
    threading.Thread(target=pump, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0", help="UDP bind interface")
    ap.add_argument("--port", type=int, default=5300, help="UDP port (match the game)")
    ap.add_argument("--output", help="output path (default forza_log_<timestamp>.<ext>)")
    ap.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    ap.add_argument("--racing-only", action="store_true",
                    help="only log packets where is_race_on is set")
    ap.add_argument("--duration", type=float, default=0,
                    help="auto-stop after N seconds (0 = until Ctrl+C)")
    ap.add_argument("--list-fields", action="store_true",
                    help="print every available field and exit")
    ap.add_argument("--demo", action="store_true",
                    help="generate fake telemetry locally (no game needed)")
    args = ap.parse_args()

    if args.list_fields:
        list_fields()
        return 0

    ext = "csv" if args.format == "csv" else "jsonl"
    path = args.output or f"forza_log_{datetime.now():%Y%m%d_%H%M%S}.{ext}"

    if args.demo:
        _demo_socket(args.port)
        print("DEMO mode: streaming synthetic telemetry to self.")

    f, writer = _open_writer(path, args.format)
    print(f"Logging {len(COLUMNS)} values per packet to {path}")
    print(f"Listening on {args.host}:{args.port} (UDP). Ctrl+C to stop.\n")

    count = written = 0
    start = time.time()
    last_report = start
    running = {"go": True}
    signal.signal(signal.SIGINT, lambda *_: running.__setitem__("go", False))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.settimeout(0.5)

    try:
        while running["go"]:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                if args.duration and time.time() - start >= args.duration:
                    break
                continue

            now = time.time()
            count += 1
            pkt = ft.parse(data)
            if args.racing_only and not pkt.is_racing:
                continue

            row = pkt.to_dict()
            row["recv_time"] = round(now, 3)
            if args.format == "csv":
                writer.writerow(row)
            else:
                f.write(json.dumps(row) + "\n")
            written += 1

            if now - last_report >= 1.0:
                rate = written / (now - start) if now > start else 0
                print(f"\r{written} rows logged  ({count} packets seen)  "
                      f"{rate:5.1f}/s   ", end="", flush=True)
                last_report = now

            if args.duration and now - start >= args.duration:
                break
    finally:
        sock.close()
        f.close()

    print(f"\n\nDone. {written} rows written to {path}")
    if args.racing_only and count > written:
        print(f"  ({count - written} non-racing packets skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
