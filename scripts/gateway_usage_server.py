#!/usr/bin/env python3
"""Read-only dashboard for the gateway usage log (src/gateway_usage.py). Usage: gateway_usage_server.py [port] [db]
Serves / (HTML, auto-refresh) and /json. Stdlib only."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src import gateway_usage as gu  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8989
DB = sys.argv[2] if len(sys.argv) > 2 else None


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        try:
            days = max(1, min(365, int((parse_qs(u.query).get("days") or ["14"])[0])))
        except ValueError:
            days = 14
        if u.path == "/json":
            body, ctype = json.dumps(gu.summary(days, DB)).encode(), "application/json"
        elif u.path in ("/", "/index.html"):
            body, ctype = gu.render_html(gu.summary(days, DB)).encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
