"""9600-baud AIS demodulator for FM-discriminator audio.

Audio → low-pass → DC block → clock recovery (PLL on zero crossings) →
bit slicer → NRZI decode → HDLC deframing (flags, bit unstuffing) →
CRC-16 check → AIS message bits.

Works at any sound-card rate (48 kHz recommended; 44.1 kHz also works).
Polarity doesn't matter (NRZI), so inverted discriminator outputs are fine.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np
from scipy.signal import butter, firwin, lfilter, lfilter_zi

BAUD = 9600


def crc16_x25(data: bytes) -> int:
    """Running CRC-16/X.25 without the final XOR (residue over data+FCS is 0xF0B8)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


class AisDemod:
    def __init__(self, fs: int, on_frame: Callable[[list[int]], None]) -> None:
        self.fs = fs
        self.on_frame = on_frame
        self.taps = firwin(31, 7200, fs=fs)
        self.zi = np.zeros(len(self.taps) - 1)
        self.hb, self.ha = butter(1, 30, btype="highpass", fs=fs)
        self.hz = lfilter_zi(self.hb, self.ha) * 0
        self.inc = BAUD / fs
        self.gain = 0.25
        self.phase = 0.0
        self.prev = 0.0
        self.last_level = False
        # HDLC state
        self.shreg = 0
        self.ones = 0
        self.in_frame = False
        self.bits: list[int] = []
        # stats
        self.frames = 0
        self.crc_errors = 0
        self.level_db = -120.0

    def process(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float64).ravel()
        if not len(x):
            return
        rms = float(np.sqrt(np.mean(x * x)))
        self.level_db = round(20 * math.log10(rms + 1e-12), 1)
        y, self.zi = lfilter(self.taps, 1.0, x, zi=self.zi)
        y, self.hz = lfilter(self.hb, self.ha, y, zi=self.hz)

        phase, prev, inc, gain = self.phase, self.prev, self.inc, self.gain
        last_level = self.last_level
        bit = self._bit
        for s in y.tolist():
            if (s >= 0.0) != (prev >= 0.0) and prev != s:
                # zero crossing: should sit halfway between bit centres
                cp = (phase + inc * (prev / (prev - s))) % 1.0
                phase -= gain * (cp - 0.5)
            nxt = phase + inc
            if nxt >= 1.0:
                t = (1.0 - phase) / inc
                v = prev + (s - prev) * min(max(t, 0.0), 1.0)
                level = v >= 0.0
                bit(1 if level == last_level else 0)  # NRZI: no change = 1
                last_level = level
                nxt -= 1.0
            phase = nxt
            prev = s
        self.phase, self.prev, self.last_level = phase, prev, last_level

    def _bit(self, b: int) -> None:
        self.shreg = ((self.shreg >> 1) | (b << 7)) & 0xFF
        if self.shreg == 0x7E:  # flag 01111110
            if self.in_frame and len(self.bits) > 7:
                self._frame(self.bits[:-7])
            self.bits = []
            self.ones = 0
            self.in_frame = True
            return
        if not self.in_frame:
            return
        if b:
            self.ones += 1
            if self.ones >= 7:  # abort / idle
                self.in_frame = False
                self.bits = []
                return
            self.bits.append(1)
        else:
            if self.ones == 5:  # stuffed zero
                self.ones = 0
                return
            self.ones = 0
            self.bits.append(0)
        if len(self.bits) > 1200:
            self.in_frame = False
            self.bits = []

    def _frame(self, bits: list[int]) -> None:
        n = len(bits)
        if n < 112 or n % 8:
            return
        data = bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, n, 8))
        if crc16_x25(data) != 0xF0B8:
            if n <= 1100:
                self.crc_errors += 1
            return
        self.frames += 1
        msg_bits: list[int] = []
        for i in range(0, n - 16, 8):
            msg_bits.extend(reversed(bits[i:i + 8]))  # bytes go out LSB first
        self.on_frame(msg_bits)
