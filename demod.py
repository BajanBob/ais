"""9600-baud AIS demodulator for FM-discriminator audio, with weak-signal enhancements.

Signal chain:
  1. High-pass (150 Hz)        removes DC, mains hum and its low harmonics
  2. Impulse limiter           clips radar pulses, ignition and switching clicks
                               (a symmetric limiter keeps every zero crossing and
                               bit sign intact, so real data is never damaged)
  3. Adaptive notch filters    finds steady whistles/carriers (heterodynes, CTCSS,
                               switch-mode supply tones) and notches up to 3 of them
  4. Low-pass (5.6 kHz)        matched to the 9600-baud GMSK spectrum
  5. Clock recovery            PLL locked to zero crossings
  6. Three bit slicers         sample at slightly different moments in each bit, so
                               a burst with timing jitter still decodes on one of them
  7. HDLC + CRC                with soft-decision error correction: when the CRC fails
                               on a frame of the right length, the least-confident bits
                               are flipped (singly and in NRZI pairs) until it passes.

Polarity doesn't matter (NRZI). Works at 48 kHz (recommended) or 44.1 kHz.
"""

from __future__ import annotations

import math
import time
from itertools import combinations
from typing import Callable

import numpy as np
from scipy.signal import butter, firwin, iirnotch, lfilter, lfilter_zi, welch

BAUD = 9600
# Message length in bits (without CRC) for the types where error correction is safe.
EXPECTED_BITS = {1: 168, 2: 168, 3: 168, 4: 168, 11: 168, 9: 168, 18: 168, 5: 424, 19: 312, 24: (160, 168), 27: 96}


def crc16_x25(data: bytes) -> int:
    """Running CRC-16/X.25 without the final XOR (residue over data+FCS is 0xF0B8)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def _bytes(bits) -> bytes:
    return bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits), 8))


class _Slicer:
    """One bit-decision path with its own HDLC deframer."""
    __slots__ = ("offset", "last_level", "last_v", "shreg", "ones", "in_frame", "bits", "conf")

    def __init__(self, offset: float) -> None:
        self.offset = offset
        self.last_level = False
        self.last_v = 0.0
        self.shreg = 0
        self.ones = 0
        self.in_frame = False
        self.bits: list[int] = []
        self.conf: list[float] = []


class AisDemod:
    def __init__(self, fs: int, on_frame: Callable[[list[int]], None], enhanced: bool = True) -> None:
        self.fs = fs
        self.on_frame = on_frame
        self.enhanced = enhanced
        self.hb, self.ha = butter(2, 150, btype="highpass", fs=fs)
        self.hz = lfilter_zi(self.hb, self.ha) * 0
        self.taps = firwin(63, 5600, fs=fs)
        self.zi = np.zeros(len(self.taps) - 1)
        self.notches: list[tuple[float, np.ndarray, np.ndarray, np.ndarray]] = []
        self.spec_buf: list[np.ndarray] = []
        self.spec_len = 0
        self.inc = BAUD / fs
        self.gain = 0.25
        self.phase = 0.0
        self.prev = 0.0
        self.slicers = [_Slicer(o) for o in ((-0.17, 0.0, 0.17) if enhanced else (0.0,))]
        self.recent: dict[bytes, float] = {}
        self.t = 0.0  # signal time in seconds (by samples, not the wall clock)
        # stats
        self.frames = 0
        self.corrected = 0
        self.crc_errors = 0
        self.level_db = -120.0
        self.clipped_pct = 0.0

    # ------------------------------------------------------------------ filters
    def _update_notches(self, y: np.ndarray) -> None:
        """Every ~1 s: find narrow tones standing well above the noise and notch them."""
        self.spec_buf.append(y)
        self.spec_len += len(y)
        if self.spec_len < self.fs:
            return
        seg = np.concatenate(self.spec_buf)[-self.fs:]
        self.spec_buf, self.spec_len = [], 0
        f, p = welch(seg, fs=self.fs, nperseg=4096)
        pdb = 10 * np.log10(p + 1e-20)
        band = (f > 250) & (f < 8000)
        floor = np.median(pdb[band])
        found = []
        idx = np.where(band)[0]
        for i in idx[3:-3]:
            if pdb[i] < floor + 15 or pdb[i] != pdb[i - 3:i + 4].max():
                continue
            sides = np.r_[pdb[i - 12:i - 4], pdb[i + 5:i + 13]]
            if len(sides) and pdb[i] - np.median(sides) > 12:  # narrow, not broadband data
                found.append((pdb[i], float(f[i])))
        found = [fr for _, fr in sorted(found, reverse=True)[:3]]
        keep = []
        for fr in found:
            old = next((n for n in self.notches if abs(n[0] - fr) < 30), None)
            if old:
                keep.append(old)
            else:
                b, a = iirnotch(fr, 30, fs=self.fs)
                keep.append((fr, b, a, lfilter_zi(b, a) * 0))
        self.notches = keep

    # ------------------------------------------------------------------ main
    def process(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float64).ravel()
        if not len(x):
            return
        self.level_db = round(20 * math.log10(float(np.sqrt(np.mean(x * x))) + 1e-12), 1)
        y, self.hz = lfilter(self.hb, self.ha, x, zi=self.hz)
        if self.enhanced:
            med = np.median(y)
            sigma = 1.4826 * float(np.median(np.abs(y - med))) + 1e-9
            lim = 4.5 * sigma
            self.clipped_pct = round(100 * float(np.mean(np.abs(y) > lim)), 2)
            y = np.clip(y, -lim, lim)
            self._update_notches(y)
            for k, (fr, b, a, z) in enumerate(self.notches):
                y, z2 = lfilter(b, a, y, zi=z)
                self.notches[k] = (fr, b, a, z2)
        y, self.zi = lfilter(self.taps, 1.0, y, zi=self.zi)
        scale = float(np.sqrt(np.mean(y * y))) + 1e-12
        self.t += len(y) / self.fs

        phase, prev, inc, gain = self.phase, self.prev, self.inc, self.gain
        slicers = self.slicers
        for s in y.tolist():
            if (s >= 0.0) != (prev >= 0.0) and prev != s:
                cp = (phase + inc * (prev / (prev - s))) % 1.0
                phase -= gain * (cp - 0.5)
            nxt = phase + inc
            for sl in slicers:
                a0 = phase + sl.offset
                k0 = math.floor(a0)
                if math.floor(nxt + sl.offset) > k0:  # this slicer's sampling moment falls inside this step
                    t = (k0 + 1 - a0) / inc
                    v = prev + (s - prev) * min(max(t, 0.0), 1.0)
                    level = v >= 0.0
                    c = min(abs(v), abs(sl.last_v)) / scale
                    self._bit(sl, 1 if level == sl.last_level else 0, c)
                    sl.last_level, sl.last_v = level, v
            if nxt >= 1.0:
                nxt -= 1.0
            phase, prev = nxt, s
        self.phase, self.prev = phase, prev

    def _bit(self, sl: _Slicer, b: int, c: float) -> None:
        sl.shreg = ((sl.shreg >> 1) | (b << 7)) & 0xFF
        if sl.shreg == 0x7E:
            if sl.in_frame and len(sl.bits) > 7:
                self._frame(sl.bits[:-7], sl.conf[:-7])
            sl.bits, sl.conf, sl.ones, sl.in_frame = [], [], 0, True
            return
        if not sl.in_frame:
            return
        if b:
            sl.ones += 1
            if sl.ones >= 7:
                sl.in_frame, sl.bits, sl.conf = False, [], []
                return
            sl.bits.append(1)
            sl.conf.append(c)
        else:
            if sl.ones == 5:  # stuffed zero
                sl.ones = 0
                return
            sl.ones = 0
            sl.bits.append(0)
            sl.conf.append(c)
        if len(sl.bits) > 1200:
            sl.in_frame, sl.bits, sl.conf = False, [], []

    # ------------------------------------------------------------------ frames
    def _frame(self, bits: list[int], conf: list[float]) -> None:
        n = len(bits)
        if n < 112 or n % 8:
            return
        data = _bytes(bits)
        if crc16_x25(data) == 0xF0B8:
            return self._emit(bits, data)
        if self.enhanced and self._fixable(bits):
            fixed = self._correct(bits, conf)
            if fixed:
                self.corrected += 1
                return self._emit(fixed, _bytes(fixed))
        if n <= 1100:
            self.crc_errors += 1

    @staticmethod
    def _msg_type(bits) -> int:
        first = bits[0:8][::-1]  # bytes go out LSB first
        return int("".join(map(str, first[:6])), 2)

    def _fixable(self, bits) -> bool:
        exp = EXPECTED_BITS.get(self._msg_type(bits))
        if exp is None:
            return False
        n = len(bits) - 16
        return n in exp if isinstance(exp, tuple) else n == exp

    def _correct(self, bits, conf):
        """Try flipping the least-confident bits (single, NRZI pair, two singles)."""
        order = sorted(range(len(bits)), key=conf.__getitem__)[:10]
        cands: list[tuple[int, ...]] = []
        for i in order:
            cands.append((i,))
            if i + 1 < len(bits):
                cands.append((i, i + 1))  # one wrong level flips two decoded bits
            if i > 0:
                cands.append((i - 1, i))
        cands += list(combinations(order[:6], 2))
        tried = set()
        for c in cands:
            if c in tried:
                continue
            tried.add(c)
            b2 = bits[:]
            for i in c:
                b2[i] ^= 1
            if crc16_x25(_bytes(b2)) == 0xF0B8 and self._msg_type(b2) == self._msg_type(bits):
                return b2
        return None

    def _emit(self, bits, data: bytes) -> None:
        now = self.t
        if data in self.recent and self.recent[data] > now - 0.15:  # same burst seen by another slicer
            return
        self.recent[data] = now
        if len(self.recent) > 200:
            self.recent = {k: t for k, t in self.recent.items() if t > now - 1}
        self.frames += 1
        msg_bits: list[int] = []
        for i in range(0, len(bits) - 16, 8):
            msg_bits.extend(reversed(bits[i:i + 8]))
        self.on_frame(msg_bits)

    def status(self) -> dict:
        return {"corrected": self.corrected, "clipped_pct": self.clipped_pct,
                "notches": [round(n[0]) for n in self.notches], "enhanced": self.enhanced}
