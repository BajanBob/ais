"""Audio input from the radio, NMEA inputs (UDP / serial) and NMEA outputs (UDP / TCP / log file)."""

from __future__ import annotations

import queue
import socket
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np


# ------------------------------------------------------------------ audio
def list_audio_devices() -> str:
    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:
        return f"Audio unavailable ({e}). Install it with: pip install sounddevice"
    lines = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            lines.append(f"  [{i}] {d['name']}  ({d['max_input_channels']} ch, {int(d['default_samplerate'])} Hz)")
    return "Audio inputs:\n" + "\n".join(lines)


def list_audio_inputs() -> list[dict]:
    """Sound inputs for the settings page: [{index, name, channels, rate}]."""
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        apis = sd.query_hostapis()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0:
            api = apis[d["hostapi"]]["name"] if d.get("hostapi") is not None else ""
            out.append({"index": i, "name": d["name"], "channels": d["max_input_channels"],
                        "rate": int(d["default_samplerate"]), "api": api})
    return out


def list_serial_ports() -> list[dict]:
    """COM ports for the settings page: [{device, description}]."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [{"device": p.device, "description": p.description or ""}
            for p in sorted(list_ports.comports(), key=lambda p: p.device)]


class AudioInput:
    """Sound card / USB audio from the radio's DATA (9600) or discriminator output."""

    def __init__(self, device, channel: int, fs: int, sink: Callable[[np.ndarray], None]) -> None:
        import sounddevice as sd
        self.q: queue.Queue = queue.Queue(maxsize=200)
        self.channel = channel
        self.sink = sink
        self.running = True
        dev = int(device) if isinstance(device, (str, int)) and str(device).isdigit() else device
        info = sd.query_devices(dev, "input")
        chans = max(1, min(int(info["max_input_channels"]), channel + 1))
        self.stream = sd.InputStream(device=dev, channels=chans, samplerate=fs, blocksize=fs // 10,
                                     dtype="float32", callback=self._cb)
        threading.Thread(target=self._run, daemon=True).start()
        self.stream.start()

    def _cb(self, data, frames, t, status) -> None:
        try:
            self.q.put_nowait(data[:, min(self.channel, data.shape[1] - 1)].copy())
        except queue.Full:
            pass

    def _run(self) -> None:
        while self.running:
            try:
                self.sink(self.q.get(timeout=0.5))
            except queue.Empty:
                continue

    def close(self) -> None:
        self.running = False
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass


class WavInput:
    """Plays a recorded .wav into the decoder (for testing a recording)."""

    def __init__(self, path: str, sink: Callable[[np.ndarray], None], loop: bool = False) -> None:
        from scipy.io import wavfile
        fs, data = wavfile.read(path)
        if data.ndim > 1:
            data = data[:, 0]
        if data.dtype.kind == "i":
            data = data.astype(np.float32) / np.iinfo(data.dtype).max
        self.fs, self.data, self.sink, self.loop = fs, data.astype(np.float32), sink, loop
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        step = self.fs // 10
        while True:
            for i in range(0, len(self.data), step):
                self.sink(self.data[i:i + step])
                time.sleep(0.1)
            if not self.loop:
                return


# ------------------------------------------------------------------ NMEA inputs
class NmeaUdpInput:
    """Receives !AIVDM lines from another decoder (e.g. an RTL-SDR with AIS-catcher)."""

    def __init__(self, port: int, sink: Callable[[str, str], None], name: str = "") -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        self.name = name or f"UDP {port}"
        self.sink = sink
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            data, _ = self.sock.recvfrom(4096)
            for line in data.decode(errors="ignore").splitlines():
                self.sink(line, self.name)


class NmeaSerialInput:
    """AIS sentences from a marine AIS receiver/transponder's NMEA 0183 output (usually 38400 baud)."""

    def __init__(self, port: str, baud: int, sink: Callable[[str, str], None], name: str = "") -> None:
        import serial
        self.ser = serial.Serial(port, baud, timeout=1)
        self.name = name or f"NMEA {port}"
        self.sink = sink
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            line = self.ser.readline().decode(errors="ignore")
            if line:
                self.sink(line, self.name)


# ------------------------------------------------------------------ sharing with AIS networks
class Feed:
    """Sends decoded AIS sentences to an AIS network (VesselTracker, AISHub, MarineTraffic, VesselFinder…).

    UDP: each batch is sent as one datagram. TCP: keeps a connection open and
    reconnects automatically (with back-off) if the network drops it.
    """

    def __init__(self, name: str, host: str, port: int, protocol: str = "udp") -> None:
        self.name, self.host, self.port = name, host, int(port)
        self.protocol = protocol.lower()
        self.sent = 0
        self.last_sent: float | None = None
        self.error: str | None = None
        self.sock: socket.socket | None = None
        self.next_try = 0.0
        self.lock = threading.Lock()
        self.q: queue.Queue = queue.Queue(maxsize=2000)
        self.running = True
        threading.Thread(target=self._run, daemon=True, name=f"feed-{name}").start()

    def send(self, sentences: list[str]) -> None:
        try:
            self.q.put_nowait("".join(s + "\r\n" for s in sentences).encode())
        except queue.Full:
            self.error = "queue full: network not keeping up"

    def _connect(self) -> None:
        if self.protocol == "tcp":
            s = socket.create_connection((self.host, self.port), timeout=10)
            s.settimeout(10)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((self.host, self.port))  # resolves the name once; errors show early
        self.sock = s

    def _run(self) -> None:
        backoff = 5
        while self.running:
            try:
                data = self.q.get(timeout=1)
            except queue.Empty:
                continue
            if self.sock is None:
                if time.time() < self.next_try:
                    continue  # drop while offline rather than flood on reconnect
                try:
                    self._connect()
                    self.error, backoff = None, 5
                except OSError as e:
                    self.error = f"cannot reach {self.host}:{self.port} ({e.strerror or e})"
                    self.next_try, backoff = time.time() + backoff, min(backoff * 2, 300)
                    continue
            try:
                self.sock.send(data) if self.protocol == "udp" else self.sock.sendall(data)
                self.sent += data.count(b"\n")
                self.last_sent = time.time()
                self.error = None
            except OSError as e:
                self.error = f"send failed ({e.strerror or e}); reconnecting"
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock, self.next_try = None, time.time() + backoff

    def close(self) -> None:
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def status(self) -> dict:
        ok = self.error is None and self.last_sent is not None and time.time() - self.last_sent < 300
        return {"name": self.name, "target": f"{self.protocol.upper()} {self.host}:{self.port}", "sent": self.sent,
                "last_sent": self.last_sent, "error": self.error, "ok": ok}


# ------------------------------------------------------------------ NMEA outputs
class NmeaOutput:
    """Sends decoded sentences to chart plotters: UDP targets, a TCP server, and a log file."""

    def __init__(self, udp_targets: list[str], tcp_port: int | None, log_file: str | None) -> None:
        self.udp = []
        for t in udp_targets:
            host, _, port = t.rpartition(":")
            self.udp.append((host or "127.0.0.1", int(port)))
        self.usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.clients: list[socket.socket] = []
        self.lock = threading.Lock()
        self.log = Path(log_file) if log_file else None
        self.tcp_port = tcp_port
        if tcp_port:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", tcp_port))
            srv.listen(8)
            threading.Thread(target=self._accept, args=(srv,), daemon=True).start()

    def _accept(self, srv) -> None:
        while True:
            c, _ = srv.accept()
            with self.lock:
                self.clients.append(c)

    def send(self, sentences: list[str]) -> None:
        data = "".join(s + "\r\n" for s in sentences).encode()
        for target in self.udp:
            try:
                self.usock.sendto(data, target)
            except OSError:
                pass
        with self.lock:
            for c in list(self.clients):
                try:
                    c.sendall(data)
                except OSError:
                    self.clients.remove(c)
        if self.log:
            try:
                with self.log.open("a", encoding="ascii") as f:
                    f.write(data.decode())
            except OSError:
                pass
