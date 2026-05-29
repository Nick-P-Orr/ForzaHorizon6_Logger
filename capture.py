#!/usr/bin/env python3
"""Raw UDP capture tool for Forza Horizon 6 "Data Out" telemetry.

Forza streams a fixed-size binary struct over UDP every frame (~60 Hz) to the
IP/port you configure under Settings > HUD and Gameplay > Telemetry > Data Out.
This tool listens, records every packet verbatim, and reports the packet size so
we can identify which Forza data format FH6 uses before writing a parser.

Setup:
  1. Find this machine's LAN IP:  (Mac) ipconfig getifaddr en0
                                  (Win) ipconfig
  2. In FH6: Data Out = On, Data Out IP Address = that IP, Data Out IP Port = e.g. 5300
  3. Run:  python3 capture.py --port 5300
  4. Drive. Ctrl+C to stop.

Output:
  A capture file (default capture.fhcap) where each record is:
    [8 bytes  double  little-endian]  receive timestamp (time.time())
    [4 bytes  uint32  little-endian]  payload length
    [N bytes]                          raw payload
  Replay/parse it later with a separate script once we know the layout.
"""

import argparse
import collections
import signal
import socket
import struct
import sys
import time

# Known Forza "Data Out" packet sizes -> format name. FH6 should match one of
# these (most likely the Horizon "Dash" 324-byte layout) or be a close variant.
KNOWN_SIZES = {
    232: "Sled (V1, position-only fields)",
    311: "Car Dash (FM7)",
    324: "Car Dash + Horizon HUD extension (FH4 / FH5)",
    331: "Car Dash (Forza Motorsport 2023)",
}

RECORD_HEADER = struct.Struct("<dI")  # double timestamp, uint32 length


def human_size_guess(size: int) -> str:
    if size in KNOWN_SIZES:
        return KNOWN_SIZES[size]
    return "UNKNOWN — new/variant FH6 layout (this is what we want to capture)"


def hexdump(data: bytes, width: int = 16, max_bytes: int = 128) -> str:
    lines = []
    shown = data[:max_bytes]
    for off in range(0, len(shown), width):
        chunk = shown[off:off + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {off:04x}  {hex_part:<{width * 3}}  {asc_part}")
    if len(data) > max_bytes:
        lines.append(f"  ... ({len(data) - max_bytes} more bytes)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0",
                        help="Interface to bind (default: 0.0.0.0 = all interfaces)")
    parser.add_argument("--port", type=int, default=5300,
                        help="UDP port to listen on; must match the game's Data Out port")
    parser.add_argument("--output", default="capture.fhcap",
                        help="Capture file path (default: capture.fhcap)")
    parser.add_argument("--duration", type=float, default=0,
                        help="Auto-stop after N seconds (default: 0 = until Ctrl+C)")
    parser.add_argument("--max-packets", type=int, default=0,
                        help="Auto-stop after N packets (default: 0 = unlimited)")
    parser.add_argument("--no-save", action="store_true",
                        help="Print stats only; do not write a capture file")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as exc:
        print(f"ERROR: could not bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        return 1
    sock.settimeout(0.5)

    out = None if args.no_save else open(args.output, "wb")

    print(f"Listening on {args.host}:{args.port} (UDP) ...")
    print("In FH6, set Data Out = On and point the IP/port at this machine.")
    print("Press Ctrl+C to stop.\n")

    sizes = collections.Counter()
    count = 0
    first_seen = False
    start = time.time()
    last_report = start

    running = {"go": True}

    def stop(*_):
        running["go"] = False
    signal.signal(signal.SIGINT, stop)

    try:
        while running["go"]:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                if args.duration and (time.time() - start) >= args.duration:
                    break
                continue

            now = time.time()
            count += 1
            sizes[len(data)] += 1

            if out is not None:
                out.write(RECORD_HEADER.pack(now, len(data)))
                out.write(data)

            if not first_seen:
                first_seen = True
                print(f"First packet from {addr[0]}:{addr[1]} — {len(data)} bytes")
                print(f"  Format guess: {human_size_guess(len(data))}\n")
                print("First packet hexdump (first 128 bytes):")
                print(hexdump(data))
                print()

            if now - last_report >= 1.0:
                elapsed = now - start
                rate = count / elapsed if elapsed else 0
                size_str = ", ".join(f"{s}B x{n}" for s, n in sizes.most_common())
                print(f"\r{count} packets  {rate:5.1f}/s  sizes: {size_str}   ", end="", flush=True)
                last_report = now

            if args.duration and (now - start) >= args.duration:
                break
            if args.max_packets and count >= args.max_packets:
                break
    finally:
        sock.close()
        if out is not None:
            out.close()

    elapsed = time.time() - start
    print("\n\n=== Capture summary ===")
    print(f"Packets:   {count}")
    print(f"Duration:  {elapsed:.1f} s")
    if elapsed:
        print(f"Avg rate:  {count / elapsed:.1f} packets/s")
    print("Sizes seen:")
    for size, n in sizes.most_common():
        print(f"  {size:>5} bytes  x{n:<8}  {human_size_guess(size)}")
    if out is not None and count:
        print(f"\nSaved to: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
