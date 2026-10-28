"""End-to-end decoder test: synthesized AIS audio → demodulator → decoded fields.
Run:  python -m pytest tests   (or: python tests/test_pipeline.py)"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import synth  # noqa: E402
from ais import NmeaAssembler, decode, to_nmea  # noqa: E402
from demod import AisDemod  # noqa: E402

MSGS = [
    synth.type1(377123456, 13.0975, -59.6145, 12.3, 271.5, 270),
    synth.type5(377123456, "OCEAN SPIRIT", "J8AB2", 70, "BRIDGETOWN", 180, 28, 9.1, 9312345),
    synth.type18(338765432, 13.1520, -59.6402, 6.2, 45.0, 44),
    synth.type24a(338765432, "SEA BREEZE"),
    synth.type24b(338765432, 37, "8PXY1"),
    synth.type4(3140001, 13.0723, -59.6081),
]


def run(fs, noise, invert, seed):
    rng = np.random.default_rng(seed)
    got = []
    d = AisDemod(fs, got.append)
    sig = np.concatenate([synth.audio(synth.frame_levels(m), fs=fs, noise=noise, invert=invert, rng=rng)
                          for m in MSGS])
    for i in range(0, len(sig), fs // 10):  # feed in 100 ms blocks like a sound card
        d.process(sig[i:i + fs // 10])
    return got, d


def check(got):
    dec = [decode(b) for b in got]
    assert len(dec) == len(MSGS), f"decoded {len(dec)}/{len(MSGS)}"
    a = dec[0]
    assert a["mmsi"] == 377123456 and abs(a["lat"] - 13.0975) < 1e-5 and abs(a["lon"] + 59.6145) < 1e-5
    assert a["sog"] == 12.3 and a["cog"] == 271.5 and a["heading"] == 270
    s = dec[1]
    assert s["name"] == "OCEAN SPIRIT" and s["callsign"] == "J8AB2" and s["destination"] == "BRIDGETOWN"
    assert s["length"] == 180 and s["shiptype_name"] == "Cargo" and s["imo"] == 9312345
    b = dec[2]
    assert b["cls"] == "B" and b["sog"] == 6.2 and abs(b["lat"] - 13.152) < 1e-5
    assert dec[3]["name"] == "SEA BREEZE" and dec[4]["callsign"] == "8PXY1"
    assert dec[5]["cls"] == "Base station"


def test_48k_clean():
    got, d = run(48000, 0.02, False, 1)
    check(got)
    assert d.frames == len(MSGS)


def test_44k_inverted_noisy():
    got, _ = run(44100, 0.08, True, 2)
    check(got)


def test_nmea_roundtrip():
    asm = NmeaAssembler()
    for m in MSGS:
        sentences = to_nmea(m, "A")
        res = None
        for s in sentences:
            res = asm.feed(s) or res
        assert res and decode(res[0])["mmsi"] == decode(m)["mmsi"]
    assert len(to_nmea(MSGS[1], "B")) == 2  # type 5 needs two sentences


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
