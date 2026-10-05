"""Small real HTTP workload; each replica processes CPU work sequentially."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        self.respond({"status": "ok", "pid": os.getpid()})

    def do_POST(self):
        if self.path != "/work":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                self.send_error(413)
                return
            payload = json.loads(self.rfile.read(length))
            duration = max(1, min(500, int(payload.get("work_ms", 30)))) / 1000
        except (ValueError, TypeError):
            self.send_error(400)
            return
        start = time.perf_counter()
        value = b"helio-measured-workload"
        rounds = 0
        while time.perf_counter() - start < duration:
            value = hashlib.pbkdf2_hmac("sha256", value, b"workload", 1000)
            rounds += 1
        self.respond(
            {
                "worker_pid": os.getpid(),
                "work_ms": round((time.perf_counter() - start) * 1000, 2),
                "rounds": rounds,
                "checksum": value.hex(),
            }
        )

    def respond(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--ready-file")
    parser.add_argument("--parent-pipe", action="store_true")
    parser.add_argument("--health-port", type=int)
    args = parser.parse_args()
    HTTPServer.request_queue_size = 128
    server = HTTPServer((args.host, args.port), Handler)
    health_server = HTTPServer((args.host, args.health_port), Handler) if args.health_port else None
    if health_server:
        threading.Thread(target=health_server.serve_forever, daemon=True).start()
    if args.ready_file:
        Path(args.ready_file).write_text(json.dumps({"port": server.server_port}), encoding="utf-8")
    if args.parent_pipe:

        def stop_with_parent():
            sys.stdin.buffer.read()
            server.shutdown()

        threading.Thread(target=stop_with_parent, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if health_server:
            health_server.shutdown()
            health_server.server_close()
