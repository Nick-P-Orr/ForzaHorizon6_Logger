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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import forza_telemetry as ft

WEB_DIR = Path(__file__).resolve().parent / "web"

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


def _udp_worker(host: str, port: int) -> None:
    for pkt in ft.listen(host, port):
        d = pkt.to_dict()
        d["connected"] = True
        d["recv_time"] = time.time()
        _set_latest(d)


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
        time.sleep(1 / 60)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence per-request console spam
        pass

    def do_GET(self):
        if self.path == "/stream":
            return self._serve_stream()
        if self.path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        name = self.path.lstrip("/")
        if name in ("app.js", "style.css"):
            ctype = "application/javascript" if name.endswith(".js") else "text/css"
            return self._serve_file(name, ctype)
        self.send_error(404)

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
