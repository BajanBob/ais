#!/usr/bin/env python3
"""Receive-only VHF AIS station.

    python aisrx.py --list-audio          show sound inputs (find your radio's USB audio)
    python aisrx.py                       run with config.json
    python aisrx.py --demo                simulated ships through the real decoder
    python aisrx.py --wav recording.wav   decode a recording of the radio's 9600 audio

The radios are only ever tuned (frequency + FM mode). This program has no
transmit or PTT function.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        example = HERE / "config.example.json"
        if example.exists() and path == "config.json":
            shutil.copy(example, p)
            print(f"Created {p} from the example. Edit it for your radios and station position, then restart.")
        else:
            sys.exit(f"Config file {p} not found.")
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Receive-only VHF AIS station")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--list-audio", action="store_true", help="List sound inputs and exit")
    ap.add_argument("--demo", action="store_true", help="Simulated ships (no radio needed)")
    ap.add_argument("--wav", help="Decode a .wav recording instead of live audio")
    ap.add_argument("--port", type=int, help="Web display port (default from config, 8100)")
    ap.add_argument("--lan", action="store_true", help="Allow other computers on your network to view the display")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    from io_ports import list_audio_devices
    if args.list_audio:
        print(list_audio_devices())
        return 0

    from server import serve
    from station import DemoTraffic, Station

    if args.demo:
        cfg = {"station": {"name": "DEMO STATION", "lat": 13.0975, "lon": -59.6167},
               "receivers": [], "nmea_udp": [], "nmea_tcp_port": None, "_demo": True}
    else:
        cfg = load_config(args.config)
    st = Station(cfg)

    if args.demo:
        rx = st.add_audio_receiver({"name": "Demo receiver", "rig": "manual", "channel": "A"})
        DemoTraffic(rx, cfg["station"]["lat"], cfg["station"]["lon"])
    elif args.wav:
        st.add_audio_receiver({"name": Path(args.wav).name, "rig": "manual", "wav": args.wav})
    else:
        for rcfg in cfg.get("receivers", []):
            if rcfg.get("enabled", True):
                rx = st.add_audio_receiver(rcfg)
                print(f"  {rx.name}: {rx.error or rx.rig.status}")
        st.add_nmea_inputs()

    def ticker():
        while True:
            st.tick()
            time.sleep(1)
    threading.Thread(target=ticker, daemon=True).start()

    port = args.port or int(cfg.get("web_port", 8100))
    try:
        httpd = serve(st, port, lan=args.lan or bool(cfg.get("web_lan")))
    except OSError:
        print(f"Port {port} is busy. Try --port {port + 1}")
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  AIS station running (receive-only). Display: {url}")
    if cfg.get("nmea_udp"):
        print(f"  NMEA to: {', '.join(cfg['nmea_udp'])} (UDP)")
    if cfg.get("nmea_tcp_port"):
        print(f"  NMEA TCP server on port {cfg['nmea_tcp_port']}")
    print("  Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
