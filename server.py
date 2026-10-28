"""Local web server for the station display. Listens on 127.0.0.1 by default."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "web"


class Handler(BaseHTTPRequestHandler):
    station = None
    allow_lan = False

    def log_message(self, *_a) -> None:
        pass

    def _host_ok(self) -> bool:
        if self.allow_lan:
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(403, b"{}")
        if self.path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, json.dumps(self.station.snapshot()).encode())
        if self.path == "/api/settings":
            return self._send(200, json.dumps(self.station.settings()).encode())
        self._send(404, b"{}")

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode())

    def do_POST(self) -> None:
        if not self._host_ok() or self.headers.get("X-AIS") != "1":
            return self._send(403, b"{}")
        n = min(int(self.headers.get("Content-Length") or 0), 65536)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, b"{}")
        if self.path in ("/api/settings", "/api/test_cat"):
            try:
                if self.path == "/api/settings":
                    return self._json({"ok": True, "message": self.station.apply_settings(data)})
                return self._json(self.station.test_cat(data))
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 400)
        if self.path == "/api/channel":
            mode = str(data.get("mode", "")).upper()
            if mode not in ("A", "B", "SCAN"):
                return self._send(400, b"{}")
            for rx in self.station.receivers:
                if rx.name == data.get("rx"):
                    rx.set_mode(mode)
            return self._send(200, b'{"ok":true}')
        self._send(404, b"{}")


def serve(station, port: int, lan: bool = False) -> ThreadingHTTPServer:
    Handler.station = station
    Handler.allow_lan = lan
    httpd = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd
