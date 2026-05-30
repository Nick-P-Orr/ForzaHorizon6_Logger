#!/usr/bin/env python3
"""Live web dashboard for Forza Horizon 6 "Data Out" telemetry.

Pure standard library (no pip installs). Two threads:

  * UDP thread  - binds the game's Data Out port, parses each packet, and stores
                  the latest reading in shared state.
  * HTTP server - serves the dashboard page and a Server-Sent Events (SSE)
                  stream at /stream that pushes the latest reading to the browser.

Run:
    python3 server.py --port 5300                 # listen for the game on 5300
    python3 server.py --port 5300 --demo          # no game needed; fake data
then open http://localhost:8000 in a browser.

In FH6: Settings > HUD and Gameplay > Telemetry
    Data Out          = On
    Data Out IP       = this machine's LAN IP   (Mac: ipconfig getifaddr en0)
    Data Out IP Port  = 5300   (must match --port)
"""

from __future__ import annotations

import argparse
import json
import math
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import forza_telemetry as ft

WEB_DIR = Path(__file__).resolve().parent / "web"
RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"

# Shared latest-reading state, written by the UDP/demo thread, read by HTTP.
_state_lock = threading.Lock()
_latest: dict = {"connected": False, "is_racing": False}


def _set_latest(d: dict) -> None:
    with _state_lock:
        _latest.clear()
        _latest.update(d)


def _get_latest() -> dict:
    with _state_lock:
        return dict(_latest)


# ---- Recording ---------------------------------------------------------------
# A telemetry session can be recorded to disk while the dashboard is running.
# Format: JSONL (one parsed packet per line, the same dict /stream sends), with
# an added `t` field = seconds since recording started, so a future replayer can
# preserve the original cadence without absolute timestamps.

class Recorder:
    """Append-only JSONL recorder. Thread-safe (UDP thread writes; HTTP starts/stops)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fh = None              # open file handle while recording
        self._path: Path | None = None
        self._started_at: float = 0.0
        self._row_count: int = 0

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
            self._fh = open(self._path, "w", encoding="utf-8", buffering=1)  # line-buffered
            self._started_at = time.time()
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

    def write(self, d: dict) -> None:
        # Hot path — called from the UDP thread on every packet. Cheap fast-exit
        # when not recording, no lock acquisition.
        if self._fh is None:
            return
        with self._lock:
            if self._fh is None:                    # re-check after lock
                return
            row = dict(d)
            row["t"] = round(time.time() - self._started_at, 4)
            try:
                self._fh.write(json.dumps(row) + "\n")
                self._row_count += 1
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


def _udp_worker(host: str, port: int) -> None:
    for pkt in ft.listen(host, port):
        d = pkt.to_dict()
        d["connected"] = True
        d["recv_time"] = time.time()
        _set_latest(d)
        _recorder.write(d)


def _demo_worker() -> None:
    """Feed plausible, animated data so the dashboard can be seen without FH6."""
    t0 = time.time()
    while True:
        t = time.time() - t0
        speed_ms = 20 + 18 * (0.5 + 0.5 * math.sin(t * 0.6))
        rpm = 1500 + 5500 * (0.5 + 0.5 * math.sin(t * 1.7))
        gear = 1 + int((math.sin(t * 0.6) + 1) / 2 * 6)
        throttle = int(128 + 127 * math.sin(t * 1.7))
        brake = max(0, int(-200 * math.sin(t * 1.7)))
        pkt = ft.parse(ft.build_synthetic(
            current_engine_rpm=rpm,
            engine_max_rpm=7500.0,
            velocity_x=speed_ms, velocity_y=0.0, velocity_z=0.0,
            speed=speed_ms, gear=gear, accel=min(255, throttle),
            brake=min(255, brake),
            tire_temp_fl=80 + 10 * math.sin(t),
            tire_temp_fr=82 + 10 * math.sin(t + 1),
            tire_temp_rl=88 + 10 * math.sin(t + 2),
            tire_temp_rr=86 + 10 * math.sin(t + 3),
        ))
        d = pkt.to_dict()
        d["connected"] = True
        d["recv_time"] = time.time()
        _set_latest(d)
        _recorder.write(d)
        time.sleep(1 / 60)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence per-request console spam
        pass

    def do_GET(self):
        if self.path == "/stream":
            return self._serve_stream()
        if self.path == "/record/status":
            return self._send_json(_recorder.status())
        if self.path == "/recordings":
            return self._send_json({"recordings": list_recordings()})
        if self.path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        name = self.path.lstrip("/")
        if name in ("app.js", "style.css"):
            ctype = "application/javascript" if name.endswith(".js") else "text/css"
            return self._serve_file(name, ctype)
        self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/record/start"):
            length = int(self.headers.get("Content-Length") or 0)
            label = ""
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                    label = (body.get("label") or "").strip()
                except (ValueError, UnicodeDecodeError):
                    pass
            return self._send_json(_recorder.start(label or None))
        if self.path.startswith("/record/stop"):
            return self._send_json(_recorder.stop())
        self.send_error(404)

    def _send_json(self, payload: dict | list):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, name: str, ctype: str):
        path = WEB_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(_get_latest())
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(1 / 30)  # 30 Hz is plenty for a browser UI
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser tab closed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0", help="UDP bind interface")
    ap.add_argument("--port", type=int, default=5300, help="UDP port (match the game)")
    ap.add_argument("--http-port", type=int, default=8000, help="dashboard web port")
    ap.add_argument("--demo", action="store_true",
                    help="generate fake telemetry (no game required)")
    args = ap.parse_args()

    if args.demo:
        threading.Thread(target=_demo_worker, daemon=True).start()
        print("DEMO mode: generating synthetic telemetry.")
    else:
        threading.Thread(target=_udp_worker, args=(args.host, args.port),
                         daemon=True).start()
        print(f"Listening for Forza Data Out on UDP {args.host}:{args.port}")

    httpd = ThreadingHTTPServer(("0.0.0.0", args.http_port), Handler)
    print(f"Dashboard: http://localhost:{args.http_port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
