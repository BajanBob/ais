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
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent


def load_config(path: str) -> tuple[dict, bool]:
    """Returns (config, created_now)."""
    p = Path(path)
    if not p.exists():
        example = HERE / "config.example.json"
        if not example.exists():
            example = APP_DIR / "config.example.json"
        if example.exists() and path == "config.json":
            shutil.copy(example, p)
            return json.loads(p.read_text(encoding="utf-8")), True
        raise FileNotFoundError(f"Config file {p.resolve()} not found.")
    try:
        return json.loads(p.read_text(encoding="utf-8")), False
    except ValueError as e:
        raise ValueError(f"config.json has a typing mistake (line {getattr(e, 'lineno', '?')}): {e}. "
                         "Check for a missing comma or quote, or delete config.json to start fresh.") from e


def selftest() -> int:
    """Checks that every part of the program is present and the decoder works (used by the build)."""
    import numpy as np
    import synth
    from ais import decode
    from demod import AisDemod
    got = []
    d = AisDemod(48000, got.append)
    sig = synth.audio(synth.frame_levels(synth.type1(377123456, 13.1, -59.6, 10.0, 90.0, 90)), noise=0.03,
                      rng=np.random.default_rng(1))
    for i in range(0, len(sig), 4800):
        d.process(sig[i:i + 4800])
    ok = len(got) == 1 and decode(got[0])["mmsi"] == 377123456
    print(f"Decoder: {'OK' if ok else 'FAILED'}")
    import server  # noqa: F401
    import station  # noqa: F401
    print(f"Display page: {'OK' if (HERE / 'web' / 'index.html').exists() else 'MISSING'}")
    try:
        import serial  # noqa: F401
        print("Serial/CAT: OK")
    except ImportError as e:
        print(f"Serial/CAT: MISSING ({e})")
        ok = False
    try:
        import sounddevice  # noqa: F401
        print("Audio: OK")
    except Exception as e:  # noqa: BLE001 - no sound system on a build server is fine
        print(f"Audio: library present, no sound system here ({type(e).__name__}: {e})")
    return 0 if ok and (HERE / "web" / "index.html").exists() else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Receive-only VHF AIS station")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--list-audio", action="store_true", help="List sound inputs and exit")
    ap.add_argument("--demo", action="store_true", help="Simulated ships (no radio needed)")
    ap.add_argument("--wav", help="Decode a .wav recording instead of live audio")
    ap.add_argument("--port", type=int, help="Web display port (default from config, 8100)")
    ap.add_argument("--lan", action="store_true", help="Allow other computers on your network to view the display")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="Check the installation and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    from io_ports import list_audio_devices
    if args.list_audio:
        print(list_audio_devices())
        return 0

    from server import serve
    from station import DemoTraffic, Station

    if not args.demo and not args.wav:
        cfg, created = load_config(args.config)
        if created:
            print(f"\n  First run: created {Path(args.config).resolve()}")
            print("  Open it in Notepad and set your radios' COM ports, audio device and station position,")
            print("  then start AIS Station again. Starting the DEMO now so you can see how it works.\n")
            args.demo = True
    if args.demo:
        cfg = {"station": {"name": "DEMO STATION", "lat": 13.0975, "lon": -59.6167},
               "receivers": [], "nmea_udp": [], "nmea_tcp_port": None, "_demo": True}
    elif args.wav:
        cfg = {"station": {"name": "WAV FILE"}, "nmea_udp": [], "nmea_tcp_port": None}
    st = Station(cfg)
    st.config_path = args.config  # the Settings page edits this file

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


def run() -> int:
    """Run main(); if anything goes wrong, show the error and keep the window open."""
    if FROZEN:
        import os
        os.chdir(APP_DIR)  # keep config.json and logs next to the program, however it was started
    try:
        code = main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (print(e.code) or 1)
    except KeyboardInterrupt:
        code = 0
    except Exception as e:  # noqa: BLE001
        import traceback
        detail = traceback.format_exc()
        print(f"\n  AIS Station stopped because of an error:\n  {e}\n")
        try:
            (APP_DIR / "crash.log").write_text(time.strftime("%Y-%m-%d %H:%M:%S\n") + detail, encoding="utf-8")
            print(f"  Details were saved to {APP_DIR / 'crash.log'}. Send that file for help.")
        except OSError:
            print(detail)
        code = 1
    if FROZEN and code not in (0, None) and "--selftest" not in sys.argv:
        try:
            input("\n  Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code or 0


if __name__ == "__main__":
    sys.exit(run())
