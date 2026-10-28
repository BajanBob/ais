"""Ties receivers, decoders, NMEA inputs/outputs and the vessel tracker together."""

from __future__ import annotations

import json
import math
import random
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

import synth
from ais import NmeaAssembler, Tracker, decode, to_nmea
from cat import CHANNELS, Rig, make_rig
from demod import AisDemod
from io_ports import AudioInput, Feed, NmeaOutput, NmeaSerialInput, NmeaUdpInput, WavInput


class Receiver:
    def __init__(self, station: "Station", cfg: dict, fs: int) -> None:
        self.station = station
        self.name = cfg.get("name", "Receiver")
        self.cfg = cfg
        self.mode = (cfg.get("channel") or "A").upper()  # A, B or SCAN
        self.scan_s = max(10, int(cfg.get("scan_seconds", 600)))
        self.channel = "A" if self.mode == "SCAN" else self.mode
        self.last_switch = time.time()
        self.demod = AisDemod(fs, self._frame)
        self.last_frame = None
        self.error = None
        self.audio = None
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

    def close(self) -> None:
        if self.audio:
            self.audio.close()
        self.rig.close()

    def status(self) -> dict:
        nxt = None
        if self.mode == "SCAN":
            nxt = max(0, int(self.scan_s - (time.time() - self.last_switch)))
        return {"name": self.name, "rig": self.rig.kind, "rig_status": self.error or self.rig.status,
                "cat_ok": self.rig.ok and not self.error, "mode": self.mode, "channel": self.channel,
                "mhz": CHANNELS[self.channel] / 1e6, "level_db": self.demod.level_db,
                "frames": self.demod.frames, "bad": self.demod.crc_errors, "last_frame": self.last_frame,
                "scan_s": self.scan_s, "next_switch_s": nxt}


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
        self.config_path: str | None = None
        self.callsign = st.get("callsign", "")
        self.feeds: list[Feed] = []
        self._start_feeds()

    def _start_feeds(self) -> None:
        for f in self.feeds:
            f.close()
        self.feeds = []
        if self.cfg.get("_demo"):
            return  # never send simulated ships to real networks
        for s in self.cfg.get("share", []):
            if s.get("enabled", True) and s.get("host") and s.get("port"):
                try:
                    self.feeds.append(Feed(s.get("name") or s["host"], s["host"], int(s["port"]), s.get("protocol", "udp")))
                except (ValueError, OSError) as e:
                    print(f"Sharing {s.get('name')}: {e}")

    def _share(self, sentences) -> None:
        for f in self.feeds:
            f.send(sentences)

    # -------------------------------------------------- inputs
    def add_audio_receiver(self, rcfg: dict) -> Receiver:
        fs = int(rcfg.get("sample_rate", 48000))
        rx = Receiver(self, rcfg, fs)
        self.receivers.append(rx)
        if rcfg.get("wav"):
            WavInput(rcfg["wav"], rx.feed, loop=bool(rcfg.get("loop")))
        elif rcfg.get("audio_device") not in (None, ""):
            try:
                rx.audio = AudioInput(rcfg["audio_device"], int(rcfg.get("audio_channel", 0)), fs, rx.feed)
            except Exception as e:  # noqa: BLE001
                rx.error = f"Audio: {e}"
        return rx

    # -------------------------------------------------- settings page
    EDITABLE = ("name", "enabled", "rig", "port", "baud", "civ_address", "stopbits", "audio_device",
                "audio_channel", "sample_rate", "channel", "scan_seconds")

    def settings(self) -> dict:
        from io_ports import list_audio_inputs, list_serial_ports
        st = self.cfg.get("station", {})
        rx = [{k: r.get(k) for k in self.EDITABLE if k in r} for r in self.cfg.get("receivers", [])]
        share = [{k: s.get(k) for k in ("name", "enabled", "host", "port", "protocol")} for s in self.cfg.get("share", [])]
        return {"station": {"name": st.get("name", ""), "callsign": st.get("callsign", ""), "lat": st.get("lat"), "lon": st.get("lon")},
                "receivers": rx, "share": share, "ports": list_serial_ports(), "audio": list_audio_inputs(),
                "config_path": str(Path(self.config_path).resolve()) if self.config_path else None,
                "demo": bool(self.cfg.get("_demo"))}

    @staticmethod
    def _clean_receiver(r: dict) -> dict:
        rig = str(r.get("rig", "manual")).lower()
        if rig not in ("icom", "yaesu", "kenwood", "manual"):
            raise ValueError(f"Unknown radio type {rig}")
        ch = str(r.get("channel", "A")).upper()
        if ch not in ("A", "B", "SCAN"):
            raise ValueError("Channel must be A, B or SCAN")
        out = {"name": str(r.get("name") or "Receiver")[:40], "enabled": bool(r.get("enabled", True)),
               "rig": rig, "channel": ch, "scan_seconds": max(10, min(86400, int(r.get("scan_seconds") or 600))),
               "sample_rate": int(r.get("sample_rate") or 48000), "audio_channel": int(r.get("audio_channel") or 0)}
        if r.get("audio_device") not in (None, ""):
            ad = r["audio_device"]
            out["audio_device"] = int(ad) if str(ad).isdigit() else str(ad)
        if rig != "manual":
            if not r.get("port"):
                raise ValueError(f"{out['name']}: choose a COM port, or set the radio type to Manual")
            out["port"] = str(r["port"])
            out["baud"] = int(r.get("baud") or (19200 if rig == "icom" else 38400 if rig == "yaesu" else 9600))
            out["stopbits"] = int(r.get("stopbits") or 1)
            if rig == "icom":
                addr = str(r.get("civ_address") or "0x7C")
                int(addr, 16)  # validate
                out["civ_address"] = addr
        return out

    def apply_settings(self, new: dict) -> str:
        """Validate, save to config.json and restart the receivers with the new settings."""
        receivers = [self._clean_receiver(r) for r in new.get("receivers", [])]
        names = [r["name"] for r in receivers]
        if len(set(names)) != len(names):
            raise ValueError("Each receiver needs a different name")
        stn = new.get("station", {})
        lat = float(stn["lat"]) if stn.get("lat") not in (None, "") else None
        lon = float(stn["lon"]) if stn.get("lon") not in (None, "") else None
        if lat is not None and not -90 <= lat <= 90 or lon is not None and not -180 <= lon <= 180:
            raise ValueError("Latitude must be -90..90 and longitude -180..180")
        if not self.config_path:
            raise ValueError("No config file in use")
        p = Path(self.config_path)
        try:
            saved = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            saved = {}
        old_rx = {r.get("name"): r for r in saved.get("receivers", [])}
        merged = []
        for r in receivers:  # keep any extra keys the page doesn't show
            base = dict(old_rx.get(r["name"], {}))
            for k in self.EDITABLE:
                base.pop(k, None)
            base.update(r)
            merged.append(base)
        share = []
        for s in new.get("share", []):
            host, port = str(s.get("host") or "").strip(), s.get("port")
            if not host and not port:
                continue
            if not host or not str(port).isdigit() or not 0 < int(port) < 65536:
                raise ValueError(f"Sharing '{s.get('name') or host}': enter the host name and port the network gave you")
            proto = str(s.get("protocol") or "udp").lower()
            share.append({"name": str(s.get("name") or host)[:40], "enabled": bool(s.get("enabled", True)),
                          "host": host[:200], "port": int(port), "protocol": "tcp" if proto == "tcp" else "udp"})
        saved.setdefault("station", {}).update({"name": str(stn.get("name") or "AIS station")[:60],
                                                "callsign": str(stn.get("callsign") or "")[:12].upper(), "lat": lat, "lon": lon})
        saved["receivers"] = merged
        saved["share"] = share
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        tmp.replace(p)
        if self.cfg.get("_demo"):
            return "Saved. You're in demo mode: close it and use Start AIS Station to run with these settings."
        # apply live
        for rx in self.receivers:
            rx.close()
        self.receivers = []
        self.cfg["receivers"] = merged
        self.cfg["station"] = saved["station"]
        self.cfg["share"] = share
        self._start_feeds()
        self.callsign = saved["station"]["callsign"]
        self.name, self.lat, self.lon = saved["station"]["name"], lat, lon
        self.tracker.lat, self.tracker.lon = lat, lon
        for r in merged:
            if r.get("enabled", True):
                self.add_audio_receiver(r)
        return "Saved and applied."

    def test_cat(self, r: dict) -> dict:
        r = self._clean_receiver(r)
        if r["rig"] == "manual":
            return {"ok": True, "status": "Manual radio: no CAT to test. Tune it by hand."}
        live = next((x for x in self.receivers if x.cfg.get("port") == r["port"]), None)
        if live:  # port already open by a running receiver: test through it
            live.tune()
            return {"ok": live.rig.ok, "status": live.rig.status}
        rig = make_rig(r)
        try:
            ok = rig.tune(CHANNELS["A" if r["channel"] == "SCAN" else r["channel"]])
            return {"ok": ok, "status": rig.status}
        finally:
            rig.close()

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
        self._share(sentences)
        self._handle(bits, source, channel, sentences)

    def on_nmea(self, line: str, source: str) -> None:
        res = self.asm.feed(line)
        if not res:
            return
        bits, chan, own = res
        self.out.send([line.strip()])
        if not own:
            self._share([line.strip()])
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
            "station": {"name": self.name, "callsign": self.callsign, "lat": self.lat, "lon": self.lon},
            "receivers": [r.status() for r in self.receivers],
            "feeds": [f.status() for f in self.feeds],
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
            # Demo ships start out at sea west of the station (Bridgetown is on the west coast)
            # and sail roughly north or south, parallel to the coast, so they stay off the island.
            ang = math.radians(random.uniform(215, 325))
            r = random.uniform(4, 18) / 60
            cog = random.choice((random.uniform(170, 190), random.uniform(350, 370) % 360))
            self.ships.append({"mmsi": (377 if st != 37 and st != 30 else 314) * 1000000 + 100 + i,
                               "lat": lat + r * math.cos(ang), "lon": lon + r * math.sin(ang),
                               "sog": random.uniform(4, 16) if st != 30 else 3.0, "cog": cog,
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
                s["cog"] = (s["cog"] + random.uniform(-0.5, 0.5)) % 360
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
