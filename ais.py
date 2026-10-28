"""AIS message decoding, NMEA 0183 (!AIVDM) encoding/parsing and the vessel tracker."""

from __future__ import annotations

import math
import threading
import time

# ------------------------------------------------------------------ bit helpers
def uint(b: list[int], start: int, n: int) -> int | None:
    if start + n > len(b):
        return None
    v = 0
    for i in range(start, start + n):
        v = (v << 1) | b[i]
    return v


def sint(b: list[int], start: int, n: int) -> int | None:
    v = uint(b, start, n)
    if v is None:
        return None
    return v - (1 << n) if v & (1 << (n - 1)) else v


def text(b: list[int], start: int, n: int) -> str | None:
    if start + n > len(b):
        return None
    out = []
    for i in range(start, start + n - 5, 6):
        c = uint(b, i, 6)
        out.append(chr(c + 64) if c < 32 else chr(c))
    return "".join(out).split("@")[0].strip() or None


# ------------------------------------------------------------------ NMEA armoring
def bits_to_payload(bits: list[int]) -> tuple[str, int]:
    fill = (6 - len(bits) % 6) % 6
    padded = bits + [0] * fill
    chars = []
    for i in range(0, len(padded), 6):
        v = uint(padded, i, 6)
        chars.append(chr(v + 48 if v < 40 else v + 56))
    return "".join(chars), fill


def payload_to_bits(payload: str, fill: int) -> list[int] | None:
    bits: list[int] = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        if not 0 <= v < 64:
            return None
        bits.extend((v >> k) & 1 for k in range(5, -1, -1))
    return bits[: len(bits) - fill] if fill else bits


def checksum(body: str) -> str:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"{c:02X}"


_seq = 0


def to_nmea(bits: list[int], channel: str, talker: str = "AIVDM") -> list[str]:
    """Encode decoded message bits as one or more !AIVDM sentences."""
    global _seq
    payload, fill = bits_to_payload(bits)
    parts = [payload[i:i + 60] for i in range(0, len(payload), 60)] or [""]
    seq = ""
    if len(parts) > 1:
        _seq = (_seq + 1) % 10
        seq = str(_seq)
    out = []
    for i, p in enumerate(parts, 1):
        f = fill if i == len(parts) else 0
        body = f"{talker},{len(parts)},{i},{seq},{channel},{p},{f}"
        out.append(f"!{body}*{checksum(body)}")
    return out


class NmeaAssembler:
    """Joins multi-sentence !AIVDM/!AIVDO messages from external sources."""

    def __init__(self) -> None:
        self.partial: dict[tuple, tuple[float, list[str]]] = {}

    def feed(self, line: str) -> tuple[list[int], str, bool] | None:
        line = line.strip()
        if not line.startswith(("!AIVDM", "!AIVDO", "!BSVDM", "!ABVDM")) or "*" not in line:
            return None
        body, _, cs = line[1:].partition("*")
        if checksum(body) != cs[:2].upper():
            return None
        f = body.split(",")
        if len(f) < 7:
            return None
        try:
            total, num, fill = int(f[1]), int(f[2]), int(f[6] or 0)
        except ValueError:
            return None
        own = f[0].endswith("VDO")
        chan = f[4] or "?"
        if total == 1:
            bits = payload_to_bits(f[5], fill)
            return (bits, chan, own) if bits else None
        key = (f[3], chan, total)
        now = time.time()
        self.partial = {k: v for k, v in self.partial.items() if now - v[0] < 10}
        if num == 1:
            self.partial[key] = (now, [f[5]])
            return None
        if key not in self.partial or len(self.partial[key][1]) != num - 1:
            return None
        self.partial[key][1].append(f[5])
        if num < total:
            return None
        payload = "".join(self.partial.pop(key)[1])
        bits = payload_to_bits(payload, fill)
        return (bits, chan, own) if bits else None


# ------------------------------------------------------------------ message decoding
NAV_STATUS = ["Under way (engine)", "At anchor", "Not under command", "Restricted manoeuvrability",
              "Constrained by draught", "Moored", "Aground", "Fishing", "Under way (sailing)",
              "Reserved (HSC)", "Reserved (WIG)", "Towing astern", "Pushing ahead / towing alongside",
              "Reserved", "AIS-SART / MOB / EPIRB", "Not defined"]


def ship_type_name(t: int | None) -> str | None:
    if t is None or t == 0:
        return None
    named = {30: "Fishing", 31: "Towing", 32: "Towing (large)", 33: "Dredging", 34: "Diving ops",
             35: "Military", 36: "Sailing", 37: "Pleasure craft", 50: "Pilot", 51: "Search & rescue",
             52: "Tug", 53: "Port tender", 54: "Anti-pollution", 55: "Law enforcement", 58: "Medical",
             59: "Noncombatant"}
    if t in named:
        return named[t]
    groups = {2: "Wing in ground", 4: "High-speed craft", 6: "Passenger", 7: "Cargo", 8: "Tanker", 9: "Other"}
    return groups.get(t // 10, f"Type {t}")


AID_TYPES = {1: "Reference point", 2: "RACON", 3: "Fixed structure", 5: "Light", 6: "Light with sectors",
             9: "Beacon, cardinal N", 10: "Beacon, cardinal E", 11: "Beacon, cardinal S", 12: "Beacon, cardinal W",
             13: "Beacon, port", 14: "Beacon, starboard", 19: "Beacon, special mark", 20: "Light vessel / LANBY",
             21: "Cardinal N", 22: "Cardinal E", 23: "Cardinal S", 24: "Cardinal W", 25: "Port hand mark",
             26: "Starboard hand mark", 30: "Special mark", 31: "Light vessel / rig"}


def _pos(lon, lat, scale=600000.0):
    if lon is None or lat is None:
        return None, None
    lon, lat = lon / scale, lat / scale
    if abs(lon) > 180 or abs(lat) > 90:
        return None, None
    return round(lat, 6), round(lon, 6)


def _sog(v):
    return None if v is None or v == 1023 else v / 10


def _cog(v):
    return None if v is None or v >= 3600 else v / 10


def _hdg(v):
    return None if v is None or v == 511 else v


def decode(b: list[int]) -> dict | None:
    t = uint(b, 0, 6)
    mmsi = uint(b, 8, 30)
    if t is None or mmsi is None:
        return None
    m: dict = {"type": t, "mmsi": mmsi}
    if t in (1, 2, 3) and len(b) >= 168:
        st = uint(b, 38, 4)
        lat, lon = _pos(sint(b, 61, 28), sint(b, 89, 27))
        m.update(cls="A", status=NAV_STATUS[st], lat=lat, lon=lon, sog=_sog(uint(b, 50, 10)),
                 cog=_cog(uint(b, 116, 12)), heading=_hdg(uint(b, 128, 9)))
        rot = sint(b, 42, 8)
        if rot is not None and rot != -128:
            m["rot"] = round(math.copysign((rot / 4.733) ** 2, rot), 1)
    elif t in (4, 11) and len(b) >= 168:
        lat, lon = _pos(sint(b, 79, 28), sint(b, 107, 27))
        m.update(cls="Base station", lat=lat, lon=lon)
    elif t == 5 and len(b) >= 420:
        st = uint(b, 232, 8)
        bow, stern, port, stbd = uint(b, 240, 9), uint(b, 249, 9), uint(b, 258, 6), uint(b, 264, 6)
        mon, day, hr, mn = uint(b, 274, 4), uint(b, 278, 5), uint(b, 283, 5), uint(b, 288, 6)
        m.update(cls="A", imo=uint(b, 40, 30) or None, callsign=text(b, 70, 42), name=text(b, 112, 120),
                 shiptype=st, shiptype_name=ship_type_name(st), length=(bow or 0) + (stern or 0) or None,
                 beam=(port or 0) + (stbd or 0) or None, draught=(uint(b, 294, 8) or 0) / 10 or None,
                 destination=text(b, 302, 120))
        if mon and day and hr is not None and hr < 24 and mn is not None and mn < 60:
            m["eta"] = f"{day:02d}/{mon:02d} {hr:02d}:{mn:02d} UTC"
    elif t == 18 and len(b) >= 168:
        lat, lon = _pos(sint(b, 57, 28), sint(b, 85, 27))
        m.update(cls="B", lat=lat, lon=lon, sog=_sog(uint(b, 46, 10)), cog=_cog(uint(b, 112, 12)),
                 heading=_hdg(uint(b, 124, 9)))
    elif t == 19 and len(b) >= 312:
        lat, lon = _pos(sint(b, 57, 28), sint(b, 85, 27))
        st = uint(b, 263, 8)
        m.update(cls="B", lat=lat, lon=lon, sog=_sog(uint(b, 46, 10)), cog=_cog(uint(b, 112, 12)),
                 heading=_hdg(uint(b, 124, 9)), name=text(b, 143, 120), shiptype=st,
                 shiptype_name=ship_type_name(st))
    elif t == 21 and len(b) >= 272:
        lat, lon = _pos(sint(b, 164, 28), sint(b, 192, 27))
        m.update(cls="AtoN", aid_type=AID_TYPES.get(uint(b, 38, 5), "Aid to navigation"),
                 name=text(b, 43, 120), lat=lat, lon=lon)
    elif t == 24 and len(b) >= 160:
        part = uint(b, 38, 2)
        m["cls"] = "B"
        if part == 0:
            m["name"] = text(b, 40, 120)
        elif part == 1 and len(b) >= 168:
            st = uint(b, 40, 8)
            m.update(shiptype=st, shiptype_name=ship_type_name(st), callsign=text(b, 90, 42))
    elif t == 27 and len(b) >= 96:
        lat, lon = _pos(sint(b, 44, 18), sint(b, 62, 17), 600.0)
        st = uint(b, 40, 4)
        sog = uint(b, 79, 6)
        cog = uint(b, 85, 9)
        m.update(cls="A", status=NAV_STATUS[st], lat=lat, lon=lon,
                 sog=None if sog == 63 else sog, cog=None if cog == 511 else cog)
    return m


# ------------------------------------------------------------------ vessel tracker
def distance_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class Tracker:
    def __init__(self, station_lat: float | None, station_lon: float | None, max_age_s: int = 3600) -> None:
        self.lat, self.lon, self.max_age = station_lat, station_lon, max_age_s
        self.vessels: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.total = 0
        self.by_type: dict[int, int] = {}
        self.max_range = 0.0

    def update(self, m: dict, source: str, channel: str) -> None:
        now = time.time()
        with self.lock:
            self.total += 1
            self.by_type[m["type"]] = self.by_type.get(m["type"], 0) + 1
            v = self.vessels.setdefault(m["mmsi"], {"mmsi": m["mmsi"], "first": now, "count": 0, "sources": []})
            for k, val in m.items():
                if val is not None and k != "type":
                    v[k] = val
            v["count"] += 1
            v["last"] = now
            v["channel"] = channel
            if source not in v["sources"]:
                v["sources"].append(source)
            if m.get("lat") is not None:
                v["pos_time"] = now
                track = v.setdefault("track", [])
                if not track or (track[-1][0], track[-1][1]) != (m["lat"], m["lon"]):
                    track.append((m["lat"], m["lon"]))
                    del track[:-40]
                if self.lat is not None:
                    d = distance_nm(self.lat, self.lon, m["lat"], m["lon"])
                    v["range_nm"] = round(d, 1)
                    v["bearing"] = round(bearing(self.lat, self.lon, m["lat"], m["lon"]))
                    if d < 300:  # ignore obviously bad positions for the range record
                        self.max_range = max(self.max_range, d)

    def snapshot(self) -> list[dict]:
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.vessels.items() if now - v["last"] > self.max_age]:
                del self.vessels[k]
            return sorted((dict(v) for v in self.vessels.values()), key=lambda v: -v["last"])
