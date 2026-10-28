"""Ties receivers, decoders, NMEA inputs/outputs and the vessel tracker together."""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque

import numpy as np

import synth
from ais import NmeaAssembler, Tracker, decode, to_nmea
from cat import CHANNELS, Rig, make_rig
from demod import AisDemod
from io_ports import AudioInput, NmeaOutput, NmeaSerialInput, NmeaUdpInput, WavInput


class Receiver:
    def __init__(self, station: "Station", cfg: dict, fs: int) -> None:
        self.station = station
        self.name = cfg.get("name", "Receiver")
        self.cfg = cfg
        self.mode = (cfg.get("channel") or "A").upper()  # A, B or SCAN
        self.scan_s = int(cfg.get("scan_seconds", 30))
        self.channel = "A" if self.mode == "SCAN" else self.mode
        self.last_switch = time.time()
        self.demod = AisDemod(fs, self._frame)
        self.last_frame = None
        self.error = None
        try:
            self.rig: Rig = make_rig(cfg)
        except Exception as e:  # noqa: BLE001
            self.rig = Rig(self.name)
            self.error = f"CAT: {e}"
        self.tune()

    def tune(self) -> None:
        try:
            self.rig.tune(CHANNELS[self.channel])
        except Exception as e:  # noqa: BLE001
            self.rig.status = f"CAT error: {e}"

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        if mode in CHANNELS:
            self.channel = mode
            self.tune()
        self.last_switch = time.time()

    def tick(self) -> None:
        if self.mode == "SCAN" and time.time() - self.last_switch >= self.scan_s:
            self.channel = "B" if self.channel == "A" else "A"
            self.last_switch = time.time()
            self.tune()

    def feed(self, samples) -> None:
        self.demod.process(samples)

    def _frame(self, bits) -> None:
        self.last_frame = time.time()
        self.station.on_bits(bits, self.name, self.channel)

    def status(self) -> dict:
        return {"name": self.name, "rig": self.rig.kind, "rig_status": self.error or self.rig.status,
                "cat_ok": self.rig.ok and not self.error, "mode": self.mode, "channel": self.channel,
                "mhz": CHANNELS[self.channel] / 1e6, "level_db": self.demod.level_db,
                "frames": self.demod.frames, "bad": self.demod.crc_errors, "last_frame": self.last_frame}


class Station:
    def __init__(self, cfg: dict) -> None:
        st = cfg.get("station", {})
        self.cfg = cfg
        self.name = st.get("name", "AIS station")
        self.lat, self.lon = st.get("lat"), st.get("lon")
        self.tracker = Tracker(self.lat, self.lon, int(cfg.get("vessel_timeout_min", 60)) * 60)
        self.out = NmeaOutput(cfg.get("nmea_udp", []), cfg.get("nmea_tcp_port"), cfg.get("log_file"))
        self.asm = NmeaAssembler()
        self.receivers: list[Receiver] = []
        self.inputs: list[str] = []
        self.log: deque = deque(maxlen=60)
        self.lock = threading.Lock()
        self.started = time.time()
        self.rate: deque = deque(maxlen=600)

    # -------------------------------------------------- inputs
    def add_audio_receiver(self, rcfg: dict) -> Receiver:
        fs = int(rcfg.get("sample_rate", 48000))
        rx = Receiver(self, rcfg, fs)
        self.receivers.append(rx)
        if rcfg.get("wav"):
            WavInput(rcfg["wav"], rx.feed, loop=bool(rcfg.get("loop")))
        elif rcfg.get("audio_device") is not None:
            try:
                AudioInput(rcfg["audio_device"], int(rcfg.get("audio_channel", 0)), fs, rx.feed)
            except Exception as e:  # noqa: BLE001
                rx.error = f"Audio: {e}"
        return rx

    def add_nmea_inputs(self) -> None:
        for p in self.cfg.get("nmea_in_udp", []):
            try:
                self.inputs.append(NmeaUdpInput(int(p), self.on_nmea).name)
            except OSError as e:
                print(f"NMEA UDP {p}: {e}")
        for s in self.cfg.get("nmea_in_serial", []):
            try:
                self.inputs.append(NmeaSerialInput(s["port"], int(s.get("baud", 38400)), self.on_nmea,
                                                   s.get("name", "")).name)
            except Exception as e:  # noqa: BLE001
                print(f"NMEA serial {s.get('port')}: {e}")

    # -------------------------------------------------- messages
    def on_bits(self, bits, source: str, channel: str) -> None:
        sentences = to_nmea(bits, channel)
        self.out.send(sentences)
        self._handle(bits, source, channel, sentences)

    def on_nmea(self, line: str, source: str) -> None:
        res = self.asm.feed(line)
        if not res:
            return
        bits, chan, own = res
        self.out.send([line.strip()])
        self._handle(bits, source + (" (own)" if own else ""), chan, [line.strip()])

    def _handle(self, bits, source, channel, sentences) -> None:
        m = decode(bits)
        if not m:
            return
        self.tracker.update(m, source, channel)
        now = time.time()
        self.rate.append(now)
        with self.lock:
            for s in sentences:
                self.log.append({"t": now, "src": source, "type": m["type"], "mmsi": m["mmsi"], "nmea": s})

    def tick(self) -> None:
        for rx in self.receivers:
            rx.tick()

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            log = list(self.log)[-40:]
        return {
            "station": {"name": self.name, "lat": self.lat, "lon": self.lon},
            "receivers": [r.status() for r in self.receivers],
            "inputs": self.inputs,
            "vessels": self.tracker.snapshot(),
            "stats": {"messages": self.tracker.total, "per_min": sum(1 for t in self.rate if now - t < 60),
                      "max_range_nm": round(self.tracker.max_range, 1), "uptime": int(now - self.started),
                      "by_type": self.tracker.by_type},
            "outputs": {"udp": self.cfg.get("nmea_udp", []), "tcp": self.cfg.get("nmea_tcp_port")},
            "log": log, "time": now, "demo": bool(self.cfg.get("_demo")),
        }


class DemoTraffic:
    """Simulated ships around the station, turned into audio and run through the real decoder."""

    NAMES = [("OCEAN SPIRIT", "J8AB2", 70, "BRIDGETOWN"), ("CARIB TRADER", "V4XT9", 79, "PORT OF SPAIN"),
             ("SEA BREEZE", "8PXY1", 37, None), ("ISLAND PRIDE", "8PQR4", 60, "ST VINCENT"),
             ("BLUE MARLIN", "8PFS2", 30, None), ("STAR TANKER", "C6ZZ7", 80, "POINT LISAS")]

    def __init__(self, rx: Receiver, lat: float, lon: float) -> None:
        self.rx, self.rng = rx, np.random.default_rng()
        self.ships = []
        for i, (name, call, st, dest) in enumerate(self.NAMES):
            ang = random.uniform(0, 2 * math.pi)
            r = random.uniform(2, 18) / 60
            self.ships.append({"mmsi": (377 if st != 37 and st != 30 else 314) * 1000000 + 100 + i,
                               "lat": lat + r * math.cos(ang), "lon": lon + r * math.sin(ang),
                               "sog": random.uniform(4, 16) if st != 30 else 3.0, "cog": random.uniform(0, 360),
                               "name": name, "call": call, "type": st, "dest": dest,
                               "classB": st in (30, 37)})
        self.base = (3140001, lat, lon)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        n = 0
        while True:
            msgs = []
            for s in self.ships:
                dt = 2 / 3600
                s["lat"] += s["sog"] * dt / 60 * math.cos(math.radians(s["cog"]))
                s["lon"] += s["sog"] * dt / 60 * math.sin(math.radians(s["cog"])) / math.cos(math.radians(s["lat"]))
                s["cog"] = (s["cog"] + random.uniform(-2, 2)) % 360
                if random.random() < 0.6:
                    f = synth.type18 if s["classB"] else synth.type1
                    msgs.append(f(s["mmsi"], s["lat"], s["lon"], s["sog"], s["cog"], int(s["cog"])))
                if n % 10 == 0:
                    if s["classB"]:
                        msgs.append(synth.type24a(s["mmsi"], s["name"]))
                        msgs.append(synth.type24b(s["mmsi"], s["type"], s["call"]))
                    else:
                        msgs.append(synth.type5(s["mmsi"], s["name"], s["call"], s["type"], s["dest"] or ""))
            if n % 5 == 0:
                msgs.append(synth.type4(*self.base))
            sig = np.concatenate([synth.audio(synth.frame_levels(m), noise=0.05, rng=self.rng) for m in msgs] or
                                 [np.zeros(4800)])
            for i in range(0, len(sig), 4800):
                self.rx.feed(sig[i:i + 4800])
            n += 1
            time.sleep(2)
