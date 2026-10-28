"""Builds AIS messages and turns them into 9600-baud audio, like a receiver's
discriminator output. Used for the self-test and for --demo mode (simulated
ships), so the whole decoding chain can be checked without a radio. It only
produces audio samples for the decoder; it never drives a transmitter."""

from __future__ import annotations

import math

import numpy as np

from demod import BAUD, crc16_x25


class Bits:
    def __init__(self) -> None:
        self.b: list[int] = []

    def put(self, value: int, n: int) -> "Bits":
        value &= (1 << n) - 1  # two's complement for negatives
        self.b.extend((value >> k) & 1 for k in range(n - 1, -1, -1))
        return self

    def text(self, s: str, nchars: int) -> "Bits":
        s = (s.upper() + "@" * nchars)[:nchars]
        for ch in s:
            c = ord(ch)
            self.put(c - 64 if c >= 64 else c, 6)
        return self


def type1(mmsi, lat, lon, sog, cog, hdg, status=0) -> list[int]:
    w = Bits().put(1, 6).put(0, 2).put(mmsi, 30).put(status, 4).put(0, 8).put(round(sog * 10), 10)
    w.put(1, 1).put(round(lon * 600000), 28).put(round(lat * 600000), 27).put(round(cog * 10), 12)
    w.put(hdg, 9).put(30, 6).put(0, 2).put(0, 3).put(0, 1).put(0, 19)
    return w.b


def type18(mmsi, lat, lon, sog, cog, hdg) -> list[int]:
    w = Bits().put(18, 6).put(0, 2).put(mmsi, 30).put(0, 8).put(round(sog * 10), 10).put(1, 1)
    w.put(round(lon * 600000), 28).put(round(lat * 600000), 27).put(round(cog * 10), 12).put(hdg, 9)
    w.put(30, 6).put(0, 2).put(1, 1).put(0, 1).put(1, 1).put(1, 1).put(1, 1).put(0, 1).put(0, 1).put(1, 1).put(0, 20)
    return w.b


def type5(mmsi, name, callsign, shiptype, dest, length=100, beam=20, draught=6.5, imo=0) -> list[int]:
    w = Bits().put(5, 6).put(0, 2).put(mmsi, 30).put(0, 2).put(imo, 30).text(callsign, 7).text(name, 20)
    w.put(shiptype, 8).put(length // 2, 9).put(length - length // 2, 9).put(beam // 2, 6).put(beam - beam // 2, 6)
    w.put(1, 4).put(11, 4).put(15, 5).put(14, 5).put(30, 6).put(round(draught * 10), 8).text(dest, 20)
    w.put(0, 1).put(0, 1)
    return w.b


def type24a(mmsi, name) -> list[int]:
    return Bits().put(24, 6).put(0, 2).put(mmsi, 30).put(0, 2).text(name, 20).b


def type24b(mmsi, shiptype, callsign) -> list[int]:
    w = Bits().put(24, 6).put(0, 2).put(mmsi, 30).put(1, 2).put(shiptype, 8).text("OIS", 3).put(0, 4)
    w.put(0, 20).text(callsign, 7).put(0, 30).put(0, 6)
    return w.b


def type4(mmsi, lat, lon) -> list[int]:
    w = Bits().put(4, 6).put(0, 2).put(mmsi, 30).put(2026, 14).put(10, 4).put(28, 5).put(12, 5).put(0, 6).put(0, 6)
    w.put(1, 1).put(round(lon * 600000), 28).put(round(lat * 600000), 27).put(7, 4).put(0, 10).put(0, 1).put(0, 19)
    return w.b


def frame_levels(msg_bits: list[int]) -> list[int]:
    """Message bits → HDLC frame → NRZI line levels (±1)."""
    pad = (-len(msg_bits)) % 8
    mb = msg_bits + [0] * pad
    data = bytes(int("".join(map(str, mb[i:i + 8])), 2) for i in range(0, len(mb), 8))
    fcs = crc16_x25(data) ^ 0xFFFF
    payload = data + bytes([fcs & 0xFF, fcs >> 8])
    onair = [(byte >> k) & 1 for byte in payload for k in range(8)]
    stuffed, ones = [], 0
    for b in onair:
        stuffed.append(b)
        ones = ones + 1 if b else 0
        if ones == 5:
            stuffed.append(0)
            ones = 0
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    bits = [0, 1] * 12 + flag + stuffed + flag + [0] * 4
    levels, lvl = [], 1
    for b in bits:
        if b == 0:
            lvl = -lvl
        levels.append(lvl)
    return levels


def audio(levels: list[int], fs: int = 48000, bt: float = 0.4, amp: float = 0.4,
          noise: float = 0.03, invert: bool = False, gap_s: float = 0.02, rng=None) -> np.ndarray:
    """Gaussian-filtered NRZ at 9600 baud, as an FM receiver's discriminator would output."""
    rng = rng or np.random.default_rng()
    n = int(len(levels) * fs / BAUD)
    t = np.arange(n) * BAUD / fs
    nrz = np.asarray(levels, dtype=float)[np.minimum(t.astype(int), len(levels) - 1)]
    sigma = math.sqrt(math.log(2)) / (2 * math.pi * bt) * fs / BAUD
    k = np.arange(-int(4 * sigma), int(4 * sigma) + 1)
    g = np.exp(-0.5 * (k / sigma) ** 2)
    sig = np.convolve(nrz, g / g.sum(), mode="same") * amp * (-1 if invert else 1)
    gap = np.zeros(int(gap_s * fs))
    out = np.concatenate([gap, sig, gap])
    return out + rng.normal(0, noise, len(out))
