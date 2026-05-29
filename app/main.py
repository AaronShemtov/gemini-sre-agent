"""
Entrypoint.

Runs two things:
- a tiny HTTP server on :8080 exposing /healthz and /readyz, so the
  Kubernetes liveness/readiness probes have something to hit;
- the Telegram long-polling bot (blocking).

The health server runs in a background thread; the bot owns the main
thread (python-telegram-bot manages its own asyncio loop).
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger("ai-sre-agent.main")


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/healthz", "/readyz", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):  # silence per-request logging
        return


def _serve_health() -> None:
    srv = HTTPServer(("0.0.0.0", 8080), _Health)
    srv.serve_forever()


def main() -> None:
    threading.Thread(target=_serve_health, daemon=True).start()
    from app.telegram.bot import main as run_bot
    run_bot()


if __name__ == "__main__":
    main()
