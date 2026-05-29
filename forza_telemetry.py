#!/usr/bin/env python3
"""Parser and UDP listener for Forza Horizon 6 "Data Out" telemetry.

Forza streams a fixed-size binary struct over UDP every frame (~60 Hz) to the
IP/port configured under Settings > HUD and Gameplay > Telemetry > Data Out.

The packet has two parts:

  * The "Sled" block (offsets 0..231) is IDENTICAL across every Forza title
    ever released (FM7, FM 2023, FH4, FH5, and almost certainly FH6). It holds
    engine RPM, the acceleration/velocity/angular-velocity vectors, per-wheel
    suspension/slip data, and the car's ordinal/class/PI. We treat these offsets
    as ground truth.

  * The "Dash" block that follows (position, speed, power, torque, tire temps,
    boost, fuel, lap times, gear, throttle/brake/steer) has historically shifted
    between titles. In the Horizon games it sits ~12 bytes later than in Forza
    Motorsport. Because FH6 is new and its exact layout is unconfirmed, we
    AUTO-DETECT where this block starts by scoring candidate offsets for
    plausibility (sane gear, speed, fuel, race position).

To stay robust to any Dash uncertainty, speed is ALSO derived directly from the
Sled velocity vector (speed_ms_from_velocity), which never depends on the Dash
offset. Prefer that value when you need a number you can trust on FH6 day one.
"""

from __future__ import annotations

import math
import socket
import struct
from dataclasses import dataclass, field
from typing import Iterator, Optional

# --- field type codes -> little-endian struct format -------------------------
_FMT = {
    "i": "<i",   # s32
    "I": "<I",   # u32
    "f": "<f",   # f32
    "H": "<H",   # u16
    "B": "<B",   # u8
    "b": "<b",   # s8
}
_SIZE = {"i": 4, "I": 4, "f": 4, "H": 2, "B": 1, "b": 1}

# --- Sled block: (name, type, absolute offset). STABLE across all titles. ----
SLED_FIELDS = [
    ("is_race_on", "i", 0),
    ("timestamp_ms", "I", 4),
    ("engine_max_rpm", "f", 8),
    ("engine_idle_rpm", "f", 12),
    ("current_engine_rpm", "f", 16),
    ("acceleration_x", "f", 20),
    ("acceleration_y", "f", 24),
    ("acceleration_z", "f", 28),
    ("velocity_x", "f", 32),
    ("velocity_y", "f", 36),
    ("velocity_z", "f", 40),
    ("angular_velocity_x", "f", 44),
    ("angular_velocity_y", "f", 48),
    ("angular_velocity_z", "f", 52),
    ("yaw", "f", 56),
    ("pitch", "f", 60),
    ("roll", "f", 64),
    ("norm_susp_travel_fl", "f", 68),
    ("norm_susp_travel_fr", "f", 72),
    ("norm_susp_travel_rl", "f", 76),
    ("norm_susp_travel_rr", "f", 80),
    ("tire_slip_ratio_fl", "f", 84),
    ("tire_slip_ratio_fr", "f", 88),
    ("tire_slip_ratio_rl", "f", 92),
    ("tire_slip_ratio_rr", "f", 96),
    ("wheel_rot_speed_fl", "f", 100),
    ("wheel_rot_speed_fr", "f", 104),
    ("wheel_rot_speed_rl", "f", 108),
    ("wheel_rot_speed_rr", "f", 112),
    ("wheel_on_rumble_fl", "i", 116),
    ("wheel_on_rumble_fr", "i", 120),
    ("wheel_on_rumble_rl", "i", 124),
    ("wheel_on_rumble_rr", "i", 128),
    ("wheel_in_puddle_fl", "f", 132),
    ("wheel_in_puddle_fr", "f", 136),
    ("wheel_in_puddle_rl", "f", 140),
    ("wheel_in_puddle_rr", "f", 144),
    ("surface_rumble_fl", "f", 148),
    ("surface_rumble_fr", "f", 152),
    ("surface_rumble_rl", "f", 156),
    ("surface_rumble_rr", "f", 160),
    ("tire_slip_angle_fl", "f", 164),
    ("tire_slip_angle_fr", "f", 168),
    ("tire_slip_angle_rl", "f", 172),
    ("tire_slip_angle_rr", "f", 176),
    ("tire_combined_slip_fl", "f", 180),
    ("tire_combined_slip_fr", "f", 184),
    ("tire_combined_slip_rl", "f", 188),
    ("tire_combined_slip_rr", "f", 192),
    ("susp_travel_m_fl", "f", 196),
    ("susp_travel_m_fr", "f", 200),
    ("susp_travel_m_rl", "f", 204),
    ("susp_travel_m_rr", "f", 208),
    ("car_ordinal", "i", 212),
    ("car_class", "i", 216),
    ("car_performance_index", "i", 220),
    ("drivetrain_type", "i", 224),
    ("num_cylinders", "i", 228),
]
SLED_END = 232  # first byte after the Sled block

# --- Dash block: (name, type, offset RELATIVE to the detected dash base) ------
DASH_FIELDS = [
    ("position_x", "f", 0),
    ("position_y", "f", 4),
    ("position_z", "f", 8),
    ("speed", "f", 12),            # meters/second (game-reported)
    ("power", "f", 16),            # watts
    ("torque", "f", 20),           # newton-meters
    ("tire_temp_fl", "f", 24),
    ("tire_temp_fr", "f", 28),
    ("tire_temp_rl", "f", 32),
    ("tire_temp_rr", "f", 36),
    ("boost", "f", 40),
    ("fuel", "f", 44),             # 0.0 .. 1.0 fraction of tank
    ("distance_traveled", "f", 48),
    ("best_lap", "f", 52),
    ("last_lap", "f", 56),
    ("current_lap", "f", 60),
    ("current_race_time", "f", 64),
    ("lap_number", "H", 68),
    ("race_position", "B", 70),
    ("accel", "B", 71),            # 0..255 throttle
    ("brake", "B", 72),            # 0..255 brake
    ("clutch", "B", 73),           # 0..255 clutch
    ("handbrake", "B", 74),        # 0..255 handbrake
    ("gear", "B", 75),            # 0 = reverse, 1..10 forward gears
    ("steer", "b", 76),           # -127..127
    ("normalized_driving_line", "b", 77),
    ("normalized_ai_brake_difference", "b", 78),
]
DASH_SPAN = 79  # bytes from dash base through the last Dash field (inclusive)

# Candidate starting offsets for the Dash block, tried in order during
# auto-detection. 244 = Horizon (FH4/FH5) layout; 232 = Forza Motorsport layout.
DASH_BASE_CANDIDATES = (244, 232)


def _read(data: bytes, type_code: str, offset: int):
    return struct.unpack_from(_FMT[type_code], data, offset)[0]


def _score_dash_base(data: bytes, base: int) -> int:
    """Heuristic plausibility score for a candidate Dash base offset.

    Higher is better. Used to auto-detect FH6's layout without prior knowledge.
    """
    if base + DASH_SPAN > len(data):
        return -1
    try:
        speed = _read(data, "f", base + 12)
        fuel = _read(data, "f", base + 44)
        gear = _read(data, "B", base + 75)
        race_pos = _read(data, "B", base + 70)
        accel = _read(data, "B", base + 71)
        brake = _read(data, "B", base + 72)
    except struct.error:
        return -1

    score = 0
    if math.isfinite(speed) and 0.0 <= speed <= 200.0:
        score += 2
    if math.isfinite(fuel) and 0.0 <= fuel <= 1.0:
        score += 2
    if 0 <= gear <= 10:
        score += 2
    if 0 <= race_pos <= 64:
        score += 1
    # When parked at a menu, throttle/brake are 0; that's still plausible.
    if accel <= 255 and brake <= 255:
        score += 0  # u8 is always in range; kept for documentation only
    return score


def detect_dash_base(data: bytes) -> Optional[int]:
    """Pick the most plausible Dash base offset for this packet, or None."""
    best_base, best_score = None, 0
    for base in DASH_BASE_CANDIDATES:
        s = _score_dash_base(data, base)
        if s > best_score:
            best_base, best_score = base, s
    return best_base


@dataclass
class TelemetryPacket:
    """A single parsed Forza Data Out packet."""

    raw_len: int
    dash_base: Optional[int]
    values: dict = field(default_factory=dict)

    def __getattr__(self, name):
        # Allow packet.current_engine_rpm style access into `values`.
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @property
    def is_racing(self) -> bool:
        return bool(self.values.get("is_race_on", 0))

    @property
    def speed_ms_from_velocity(self) -> float:
        """Speed magnitude from the Sled velocity vector — Dash-offset-proof."""
        vx = self.values.get("velocity_x", 0.0)
        vy = self.values.get("velocity_y", 0.0)
        vz = self.values.get("velocity_z", 0.0)
        return math.sqrt(vx * vx + vy * vy + vz * vz)

    @property
    def speed_kmh(self) -> float:
        return self.speed_ms_from_velocity * 3.6

    @property
    def speed_mph(self) -> float:
        return self.speed_ms_from_velocity * 2.2369362921

    @property
    def gear_label(self) -> str:
        g = self.values.get("gear")
        if g is None:
            return "-"
        if g == 0:
            return "R"
        return str(g)

    def to_dict(self) -> dict:
        d = dict(self.values)
        d["speed_kmh"] = round(self.speed_kmh, 2)
        d["speed_mph"] = round(self.speed_mph, 2)
        d["gear_label"] = self.gear_label
        d["is_racing"] = self.is_racing
        d["dash_base"] = self.dash_base
        return d


def parse(data: bytes, dash_base: Optional[int] = None) -> TelemetryPacket:
    """Parse a raw Data Out packet into a TelemetryPacket.

    dash_base: force a specific Dash offset, or leave None to auto-detect.
    """
    values: dict = {}
    if len(data) >= SLED_END:
        for name, tc, off in SLED_FIELDS:
            values[name] = _read(data, tc, off)

    if dash_base is None:
        dash_base = detect_dash_base(data)

    if dash_base is not None and dash_base + DASH_SPAN <= len(data):
        for name, tc, rel in DASH_FIELDS:
            values[name] = _read(data, tc, dash_base + rel)

    return TelemetryPacket(raw_len=len(data), dash_base=dash_base, values=values)


def build_synthetic(**overrides) -> bytes:
    """Build a well-formed 324-byte FH5-style packet (for tests / demos).

    Defaults to a plausible "driving" state; pass field=value to override.
    Dash fields are written at base 244 (Horizon layout).
    """
    buf = bytearray(324)
    base_vals = {
        "is_race_on": 1,
        "timestamp_ms": 123456,
        "engine_max_rpm": 7500.0,
        "engine_idle_rpm": 900.0,
        "current_engine_rpm": 4200.0,
        "velocity_x": 30.0,
        "velocity_y": 0.0,
        "velocity_z": 12.0,
    }
    base_vals.update({k: v for k, v in overrides.items() if k in dict(
        (n, t) for n, t, _ in SLED_FIELDS)})
    for name, tc, off in SLED_FIELDS:
        if name in base_vals:
            struct.pack_into(_FMT[tc], buf, off, base_vals[name])

    dash_vals = {
        "speed": 32.31,
        "power": 95000.0,
        "torque": 320.0,
        "tire_temp_fl": 85.0,
        "tire_temp_fr": 86.0,
        "tire_temp_rl": 88.0,
        "tire_temp_rr": 87.0,
        "boost": 0.5,
        "fuel": 0.83,
        "gear": 4,
        "accel": 200,
        "brake": 0,
        "race_position": 1,
        "steer": 10,
    }
    dash_vals.update({k: v for k, v in overrides.items() if k in dict(
        (n, t) for n, t, _ in DASH_FIELDS)})
    for name, tc, rel in DASH_FIELDS:
        if name in dash_vals:
            struct.pack_into(_FMT[tc], buf, 244 + rel, dash_vals[name])
    return bytes(buf)


def listen(host: str = "0.0.0.0", port: int = 5300,
           dash_base: Optional[int] = None) -> Iterator[TelemetryPacket]:
    """Yield parsed packets from the given UDP address until interrupted."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    try:
        while True:
            data, _addr = sock.recvfrom(65535)
            yield parse(data, dash_base=dash_base)
    finally:
        sock.close()


def _console(host: str, port: int) -> int:
    """Minimal zero-dependency live readout — confirms telemetry is flowing."""
    print(f"Listening on {host}:{port} (UDP). Ctrl+C to stop.\n")
    try:
        for pkt in listen(host, port):
            if not pkt.is_racing:
                print("\r[paused / in menu]                              ", end="", flush=True)
                continue
            print(
                f"\rGear {pkt.gear_label:>2}  "
                f"{pkt.speed_kmh:6.1f} km/h  "
                f"RPM {pkt.values.get('current_engine_rpm', 0):6.0f}  "
                f"thr {pkt.values.get('accel', 0):3d}  "
                f"brk {pkt.values.get('brake', 0):3d}  "
                f"(dash@{pkt.dash_base})   ",
                end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Forza Data Out console readout")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5300)
    raise SystemExit(_console(*vars(ap.parse_args()).values()))
