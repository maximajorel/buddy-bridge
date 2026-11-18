#!/usr/bin/env python3
"""Mock del bridge para probar el firmware SIN Hermes ni la placa.

Sirve /state con un ciclo de estados predefinido (o random) y responde /health.
Ideal para desarrollar los gestos, el HUD y el LED en el escritorio o en la placa
sin depender de que Sofía esté trabajando en ese momento.

Uso:
    python3 mock_bridge.py                    # ciclo automático cada 6 s
    python3 mock_bridge.py --state tool       # fijo, para probar un gesto
    python3 mock_bridge.py --port 8788 --cycle

Ciclo: idle -> thinking -> tool -> thinking -> done -> idle -> bored
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LED = {
    "busy": [255, 190, 0],
    "ok": [0, 255, 80],
    "ask": [255, 40, 40],
    "idle": [0, 180, 255],
}
PHASE = {
    "thinking": "busy", "tool": "busy", "waiting": "ask", "error": "ask",
    "done": "ok", "idle": "idle", "bored": "idle",
}
LABEL = {
    "thinking": "pensando", "tool": "usando la terminal", "waiting": "te necesita",
    "error": "algo falló", "done": "listo", "idle": "acá estoy", "bored": "aburrida",
}
CYCLE = [("idle", 6), ("thinking", 5), ("tool", 8), ("thinking", 3), ("done", 8), ("idle", 6), ("bored", 10)]


class Mock:
    def __init__(self, fixed=None, cycle=True):
        self.fixed = fixed
        self.cycle = cycle
        self.state = fixed or "idle"
        self.since = time.time()
        self.i = 0

    def tick(self):
        if self.fixed or not self.cycle:
            return
        chunk = CYCLE[self.i % len(CYCLE)]
        if time.time() - self.since >= chunk[1]:
            self.i += 1
            chunk = CYCLE[self.i % len(CYCLE)]
            self.state, self.since = chunk[0], time.time()

    def snap(self):
        self.tick()
        phase = PHASE.get(self.state, "idle")
        return {
            "state": self.state,
            "label": LABEL.get(self.state, "trabajando"),
            "phase": phase,
            "led": LED[phase],
            "elapsed": round(time.time() - self.since, 1),
            "active": 0 if self.state in ("idle", "bored") else 1,
            "run_id": None,
            "ts": time.time(),
            "since": self.since,
        }


def make_handler(mock):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"status": "ok", "service": "mock-bridge"})
            if self.path == "/state":
                return self._send(200, mock.snap())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            self._send(404, {"error": "not found (mock)"})

    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=None, help="estado fijo (idle|thinking|tool|done|bored)")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--cycle", action="store_true", help="recorre el ciclo automático")
    a = ap.parse_args()
    mock = Mock(fixed=a.state, cycle=a.cycle)
    print(f"[mock] http://127.0.0.1:{a.port}/state  (state={a.state or 'ciclo'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(mock)).serve_forever()
