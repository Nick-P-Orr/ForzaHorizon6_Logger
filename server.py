#!/usr/bin/env python3
"""Live web dashboard for Forza Horizon 6 "Data Out" telemetry.

Pure standard library (no pip installs). Threads:

  * UDP thread  - binds the game's Data Out port, parses each packet, encodes
                  it to JSON ONCE, and publishes it as the shared latest reading.
  * HTTP server - serves the dashboard page and a Server-Sent Events (SSE)
                  stream at /stream that pushes the latest reading to browsers.
  * Replay thread (optional) - plays a recorded .fhrec back through the same
                  publish path, so the whole dashboard works on recordings.

Run:
    python3 server.py --port 5300                 # listen for the game on 5300
    python3 server.py --port 5300 --demo          # no game needed; fake data
    python3 server.py --port 5300 --forward 192.168.1.50:5301
                                                  # rebroadcast raw packets
then open http://localhost:8000 in a browser.

In FH6: Settings > HUD and Gameplay > Telemetry
    Data Out          = On
    Data Out IP       = this machine's LAN IP   (Mac: ipconfig getifaddr en0)
    Data Out IP Port  = 5300   (must match --port)
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import forza_telemetry as ft

WEB_DIR = Path(__file__).resolve().parent / "web"
RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"

# Shared latest reading, published by the UDP/demo/replay thread and consumed
# by every SSE client. The JSON is encoded ONCE per packet here; SSE threads
# just compare the sequence number and write the shared bytes, so N open tabs
# cost N socket writes — not N json.dumps. seq lets clients skip pushes when
# nothing new arrived (game paused / closed).
_state_lock = threading.Lock()
_latest_json: bytes = b'{"connected": false, "is_racing": false}'
_latest_seq: int = 0


def _publish(payload: bytes) -> None:
    global _latest_json, _latest_seq
    with _state_lock:
        _latest_json = payload
        _latest_seq += 1


def _snapshot() -> tuple[int, bytes]:
    with _state_lock:
        return _latest_seq, _latest_json


# ---- Recording ---------------------------------------------------------------
# A telemetry session can be recorded to disk while the dashboard is running.
# Format: JSONL (one parsed packet per line, the same dict /stream sends), with
# an added `t` field = seconds since recording started, so the replayer can
# preserve the original cadence without absolute timestamps.

class Recorder:
    """Append-only JSONL recorder. Thread-safe (UDP thread writes; HTTP starts/stops)."""

    FLUSH_INTERVAL = 1.0  # seconds between forced flushes while recording

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fh = None              # open file handle while recording
        self._path: Path | None = None
        self._started_at: float = 0.0
        self._row_count: int = 0
        self._last_flush: float = 0.0

    @property
    def active(self) -> bool:
        with self._lock:
            return self._fh is not None

    def start(self, label: str | None = None) -> dict:
        with self._lock:
            if self._fh is not None:
                return self._status_locked()
            RECORDINGS_DIR.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = _safe_slug(label) if label else ""
            name = f"fh6_{ts}{('_' + slug) if slug else ''}.fhrec"
            self._path = RECORDINGS_DIR / name
            # Block-buffered (default): most writes are a memcpy into the
            # buffer, with a periodic flush below so a crash loses <=1s.
            self._fh = open(self._path, "w", encoding="utf-8")
            self._started_at = time.time()
            self._last_flush = self._started_at
            self._row_count = 0
            # First line is metadata so replayers don't need to guess.
            self._fh.write(json.dumps({
                "_meta": True, "version": 1, "started_at": self._started_at,
                "started_iso": datetime.now().isoformat(timespec="seconds"),
                "label": label or "",
            }) + "\n")
            return self._status_locked()

    def stop(self) -> dict:
        with self._lock:
            if self._fh is None:
                return self._status_locked()
            # Capture the live stats BEFORE closing so we can return a useful
            # summary (rows, duration, filename) alongside the "stopped" flag.
            summary = {
                "recording": False,
                "stopped": True,
                "rows": self._row_count,
                "duration": round(time.time() - self._started_at, 2),
                "started_at": self._started_at,
                "filename": self._path.name if self._path else None,
            }
            try:
                self._fh.close()
            finally:
                self._fh = None
            return summary

    def write_json(self, payload: bytes) -> None:
        # Hot path — called from the UDP thread on every packet. Cheap fast-exit
        # when not recording, no lock acquisition. `payload` is the already
        # encoded packet JSON; the recording-relative timestamp is spliced in
        # (the object always ends with '}') instead of re-encoding the dict.
        if self._fh is None:
            return
        with self._lock:
            if self._fh is None:                    # re-check after lock
                return
            now = time.time()
            t = round(now - self._started_at, 4)
            line = payload[:-1].decode("utf-8") + f',"t":{t}}}\n'
            try:
                self._fh.write(line)
                self._row_count += 1
                if now - self._last_flush >= self.FLUSH_INTERVAL:
                    self._fh.flush()
                    self._last_flush = now
            except (OSError, ValueError):
                # File closed under us or write failed; stop quietly.
                try: self._fh.close()
                except Exception: pass
                self._fh = None

    def status(self) -> dict:
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> dict:
        active = self._fh is not None
        return {
            "recording": active,
            "rows": self._row_count if active else 0,
            "started_at": self._started_at if active else 0,
            "duration": round(time.time() - self._started_at, 2) if active else 0,
            "filename": self._path.name if active else None,
        }


_recorder = Recorder()


class AutoRecord:
    """Start/stop the recorder on is_race_on edges.

    Starts as soon as a racing packet arrives; stops only after racing has been
    off for STOP_AFTER seconds, because pausing the game also drops is_race_on
    to 0 and we don't want every pause to split the recording. Only stops
    recordings it started itself — a manually started recording is left alone.
    """

    STOP_AFTER = 5.0

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder
        self._lock = threading.Lock()
        self.enabled = False
        self._auto_started = False
        self._last_racing: float = 0.0

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self.enabled = bool(on)

    def feed(self, racing: bool) -> None:
        with self._lock:
            if not self.enabled:
                return
            now = time.time()
            if racing:
                self._last_racing = now
                if not self._recorder.active:
                    self._recorder.start(label="auto")
                    self._auto_started = True
            elif (self._auto_started and self._recorder.active
                    and now - self._last_racing > self.STOP_AFTER):
                self._recorder.stop()
                self._auto_started = False


_autorec = AutoRecord(_recorder)


_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")
def _safe_slug(s: str) -> str:
    return _SLUG_RE.sub("-", s.strip())[:40]


def list_recordings() -> list[dict]:
    if not RECORDINGS_DIR.exists():
        return []
    out = []
    for p in sorted(RECORDINGS_DIR.glob("*.fhrec"), reverse=True):
        try:
            st = p.stat()
            out.append({"filename": p.name, "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            continue
    return out


# ---- Replay ------------------------------------------------------------------

class Replayer:
    """Plays a .fhrec back through the same publish path the live UDP uses.

    Rows are kept as raw JSON lines (no re-encoding on playback); only `t` is
    parsed up front for cadence/seeking. While a replay is active the live
    UDP/demo workers keep recording/auto-record but stop publishing, so the
    dashboard shows the replay.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._paused = False
        self._speed = 1.0
        self._seek_to: float | None = None
        self._rows: list[tuple[float, bytes]] = []   # (t, json line)
        self._times: list[float] = []                # t column, for bisect
        self._pos = 0
        self._filename: str | None = None
        self._meta: dict = {}

    @property
    def active(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self, filename: str, speed: float = 1.0) -> dict:
        name = Path(filename).name                   # no path traversal
        path = RECORDINGS_DIR / name
        if not name.endswith(".fhrec") or not path.is_file():
            return {"error": f"no such recording: {name}"}
        rows: list[tuple[float, bytes]] = []
        meta: dict = {}
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("_meta"):
                    meta = obj
                    continue
                rows.append((float(obj.get("t", 0.0)), line.encode("utf-8")))
        if not rows:
            return {"error": f"{name} contains no telemetry rows"}
        self.stop()
        with self._lock:
            self._rows = rows
            self._times = [t for t, _ in rows]
            self._pos = 0
            self._filename = name
            self._meta = meta
            self._paused = False
            self._speed = max(0.1, min(10.0, float(speed or 1.0)))
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> dict:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        return self.status()

    def control(self, paused: bool | None = None, seek: float | None = None,
                speed: float | None = None) -> dict:
        with self._lock:
            if paused is not None:
                self._paused = bool(paused)
            if seek is not None:
                self._seek_to = max(0.0, float(seek))
            if speed is not None:
                self._speed = max(0.1, min(10.0, float(speed)))
        return self.status()

    def status(self) -> dict:
        with self._lock:
            active = self.active
            dur = self._times[-1] if self._times else 0.0
            pos = min(self._pos, len(self._times) - 1) if self._times else 0
            return {
                "active": active,
                "filename": self._filename if active else None,
                "paused": self._paused if active else False,
                "speed": self._speed,
                "t": round(self._times[pos], 2) if (active and self._times) else 0,
                "duration": round(dur, 2),
                "rows": len(self._rows) if active else 0,
                "label": self._meta.get("label", "") if active else "",
            }

    def _run(self) -> None:
        prev_t: float | None = None
        while not self._stop_evt.is_set():
            with self._lock:
                if self._seek_to is not None:
                    self._pos = bisect.bisect_left(self._times, self._seek_to)
                    self._pos = min(self._pos, len(self._rows) - 1)
                    self._seek_to = None
                    prev_t = None                    # don't sleep across a seek
                paused = self._paused
                speed = self._speed
                pos = self._pos
            if paused:
                time.sleep(0.05)
                continue
            if pos >= len(self._rows):
                break                                # reached the end
            t_row, line = self._rows[pos]
            if prev_t is not None:
                delay = (t_row - prev_t) / speed
                if delay > 0:
                    # Cap so seek/stop/speed changes stay responsive even
                    # across a long gap in the recording.
                    time.sleep(min(delay, 0.25))
                    if delay > 0.25:
                        prev_t += 0.25 * speed
                        continue
            _publish(line)
            prev_t = t_row
            with self._lock:
                self._pos = pos + 1


_replayer = Replayer()


# ---- Telemetry sources ---------------------------------------------------------

def _handle_packet_dict(d: dict) -> None:
    """Common tail for live sources: encode once, publish, record, auto-record."""
    payload = json.dumps(d).encode("utf-8")
    if not _replayer.active:                       # replay owns the dashboard
        _publish(payload)
    _recorder.write_json(payload)
    _autorec.feed(bool(d.get("is_racing")))


def _udp_worker(host: str, port: int, forward: tuple[str, int] | None = None) -> None:
    fwd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if forward else None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    while True:
        data, _addr = sock.recvfrom(65535)
        if fwd_sock is not None:
            try:
                fwd_sock.sendto(data, forward)
            except OSError:
                pass                               # forward target unreachable
        pkt = ft.parse(data)
        d = pkt.to_dict()
        d["connected"] = True
        d["recv_time"] = time.time()
        _handle_packet_dict(d)


def _demo_worker() -> None:
    """Feed plausible, animated data so the dashboard can be seen without FH6."""
    t0 = time.time()
    lap_start = t0
    lap_num = 1
    best = 0.0
    last = 0.0
    dist = 0.0
    prev = t0
    while True:
        now = time.time()
        t = now - t0
        speed_ms = 20 + 18 * (0.5 + 0.5 * math.sin(t * 0.6))
        rpm = 1500 + 5500 * (0.5 + 0.5 * math.sin(t * 1.7))
        gear = 1 + int((math.sin(t * 0.6) + 1) / 2 * 6)
        throttle = int(128 + 127 * math.sin(t * 1.7))
        brake = max(0, int(-200 * math.sin(t * 1.7)))
        dist += speed_ms * (now - prev)
        prev = now
        # Fake a ~45s lap so lap widgets animate in demo mode.
        if now - lap_start >= 45.0:
            last = now - lap_start
            best = min(best or last, last)
            lap_num += 1
            lap_start = now
        pkt = ft.parse(ft.build_synthetic(
            current_engine_rpm=rpm,
            engine_max_rpm=7500.0,
            acceleration_x=25 * math.sin(t * 0.9),
            acceleration_z=20 * math.sin(t * 1.3),
            velocity_x=speed_ms, velocity_y=0.0, velocity_z=0.0,
            speed=speed_ms, gear=gear, accel=min(255, throttle),
            brake=min(255, brake),
            position_x=400 * math.cos(t * 0.15),
            position_z=250 * math.sin(t * 0.3),
            distance_traveled=dist,
            lap_number=lap_num,
            best_lap=best, last_lap=last,
            current_lap=now - lap_start,
            current_race_time=t,
            power=float(rpm * 40), torque=float(300 + 100 * math.sin(t)),
            tire_temp_fl=160 + 20 * math.sin(t),
            tire_temp_fr=164 + 20 * math.sin(t + 1),
            tire_temp_rl=176 + 20 * math.sin(t + 2),
            tire_temp_rr=172 + 20 * math.sin(t + 3),
        ))
        d = pkt.to_dict()
        d["connected"] = True
        d["recv_time"] = now
        _handle_packet_dict(d)
        time.sleep(1 / 60)


# ---- HTTP --------------------------------------------------------------------

# Static files the server is allowed to serve (no path traversal).
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript",
    "style.css": "text/css",
    "manifest.json": "application/manifest+json",
    "sw.js": "application/javascript",
    "icon.svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence per-request console spam
        pass

    def do_GET(self):
        if self.path == "/stream":
            return self._serve_stream()
        if self.path == "/record/status":
            st = _recorder.status()
            st["auto"] = _autorec.enabled
            return self._send_json(st)
        if self.path == "/recordings":
            return self._send_json({"recordings": list_recordings()})
        if self.path == "/replay/status":
            return self._send_json(_replayer.status())
        if self.path in ("/", "/index.html"):
            return self._serve_file("index.html")
        name = self.path.lstrip("/")
        if name in STATIC_FILES:
            return self._serve_file(name)
        self.send_error(404)

    def do_POST(self):
        body = self._read_json_body()
        if self.path.startswith("/record/start"):
            label = (body.get("label") or "").strip()
            return self._send_json(_recorder.start(label or None))
        if self.path.startswith("/record/stop"):
            return self._send_json(_recorder.stop())
        if self.path.startswith("/record/auto"):
            _autorec.set_enabled(bool(body.get("enabled")))
            st = _recorder.status()
            st["auto"] = _autorec.enabled
            return self._send_json(st)
        if self.path.startswith("/replay/start"):
            return self._send_json(_replayer.start(
                str(body.get("filename") or ""), body.get("speed") or 1.0))
        if self.path.startswith("/replay/stop"):
            st = _replayer.stop()
            # Hand the dashboard back to the live feed in a clean state.
            _publish(b'{"connected": false, "is_racing": false}')
            return self._send_json(st)
        if self.path.startswith("/replay/ctl"):
            return self._send_json(_replayer.control(
                paused=body.get("paused"), seek=body.get("seek"),
                speed=body.get("speed")))
        self.send_error(404)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            obj = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            return obj if isinstance(obj, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send_json(self, payload: dict | list):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, name: str):
        path = WEB_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", STATIC_FILES[name])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_seq = -1
        last_aux = 0.0
        try:
            while True:
                seq, payload = _snapshot()
                wrote = False
                if seq != last_seq:                # skip pushes when idle
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    last_seq = seq
                    wrote = True
                now = time.time()
                if now - last_aux >= 1.0:
                    # Side-channel status (recording + replay) rides the same
                    # stream — no separate polling — and doubles as keepalive.
                    rec = _recorder.status()
                    rec["auto"] = _autorec.enabled
                    aux = json.dumps({"rec": rec, "replay": _replayer.status()})
                    self.wfile.write(b"event: aux\ndata: " + aux.encode("utf-8") + b"\n\n")
                    last_aux = now
                    wrote = True
                if wrote:
                    self.wfile.flush()
                time.sleep(1 / 30)  # 30 Hz is plenty for a browser UI
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser tab closed


def _parse_forward(spec: str) -> tuple[str, int]:
    host, _, port = spec.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("expected HOST:PORT, e.g. 192.168.1.50:5301")
    return host, int(port)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0", help="UDP bind interface")
    ap.add_argument("--port", type=int, default=5300, help="UDP port (match the game)")
    ap.add_argument("--http-port", type=int, default=8000, help="dashboard web port")
    ap.add_argument("--demo", action="store_true",
                    help="generate fake telemetry (no game required)")
    ap.add_argument("--forward", type=_parse_forward, metavar="HOST:PORT",
                    help="rebroadcast raw packets to another consumer "
                         "(e.g. logger.py on a second port)")
    ap.add_argument("--auto-record", action="store_true",
                    help="start recording when a race starts, stop after it ends")
    args = ap.parse_args()

    if args.auto_record:
        _autorec.set_enabled(True)

    if args.demo:
        threading.Thread(target=_demo_worker, daemon=True).start()
        print("DEMO mode: generating synthetic telemetry.")
    else:
        threading.Thread(target=_udp_worker, args=(args.host, args.port, args.forward),
                         daemon=True).start()
        print(f"Listening for Forza Data Out on UDP {args.host}:{args.port}")
        if args.forward:
            print(f"Forwarding raw packets to {args.forward[0]}:{args.forward[1]}")

    httpd = ThreadingHTTPServer(("0.0.0.0", args.http_port), Handler)
    httpd.daemon_threads = True   # SSE threads shouldn't block Ctrl+C shutdown
    print(f"Dashboard: http://localhost:{args.http_port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
