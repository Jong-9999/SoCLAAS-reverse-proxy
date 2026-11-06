#!/usr/bin/env python3
"""Tiny streaming reverse proxy for an LLM API. Stdlib only, no nginx/Apache/Caddy.

Binds ONLY to the Pi's Tailscale IP, so it is unreachable from the school wifi
interface and never conflicts with other services on the same port number.

Env vars:
  UPSTREAM  required  e.g. https://api.example.com/v1   (base URL of the real API)
  LLM_KEY   required  the real API key (stays on the Pi)
  PORT      optional  default 8787
  BIND      optional  default: output of `tailscale ip -4`
  AUTH_HDR  optional  default "Authorization"; set to e.g. "x-api-key" if the API uses that
  AUTH_FMT  optional  default "Bearer {key}"; set to "{key}" for raw-key headers
"""
import http.client
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

UPSTREAM = os.environ.get("UPSTREAM", "").rstrip("/")
KEY = os.environ.get("LLM_KEY", "")
PORT = int(os.environ.get("PORT", "8787"))
AUTH_HDR = os.environ.get("AUTH_HDR", "Authorization")
AUTH_FMT = os.environ.get("AUTH_FMT", "Bearer {key}")

if not UPSTREAM or not KEY:
    sys.exit("Set UPSTREAM and LLM_KEY in the environment.")

def resolve_bind():
    if os.environ.get("BIND"):
        return os.environ["BIND"]
    try:
        return subprocess.check_output(["tailscale", "ip", "-4"], text=True).split()[0]
    except (OSError, subprocess.CalledProcessError, IndexError):
        sys.exit("Set BIND to the Pi's Tailscale IP (run `tailscale ip -4` on the host); "
                 "the tailscale CLI isn't available here.")


BIND = resolve_bind()

up = urlparse(UPSTREAM)
UP_HOST = up.hostname
UP_PORT = up.port or (443 if up.scheme == "https" else 80)
UP_PREFIX = up.path.rstrip("/")
UP_CONN = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection

# Headers that must not be forwarded as-is (hop-by-hop, or ones we set ourselves)
DROP_REQ = {"host", "connection", "keep-alive", "proxy-authorization", "te",
            "trailer", "transfer-encoding", "upgrade", "authorization",
            "x-api-key", "content-length"}
DROP_RESP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade"}


class Proxy(BaseHTTPRequestHandler):
    # HTTP/1.0 => response is close-delimited, so streaming (SSE) just works.
    protocol_version = "HTTP/1.0"

    def _forward(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROP_REQ}
        headers["Host"] = up.netloc
        headers[AUTH_HDR] = AUTH_FMT.format(key=KEY)
        if body is not None:
            headers["Content-Length"] = str(len(body))

        try:
            conn = UP_CONN(UP_HOST, UP_PORT, timeout=300)
            conn.request(self.command, UP_PREFIX + self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")
            return

        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in DROP_RESP:
                self.send_header(k, v)
        self.end_headers()

        try:
            while True:
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _forward

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    # After a reboot the container can start before Tailscale has its IP up; wait instead of dying.
    while True:
        try:
            srv = ThreadingHTTPServer((BIND, PORT), Proxy)
            break
        except OSError as e:
            print(f"Cannot bind {BIND}:{PORT} ({e}); retrying in 5s (is Tailscale up?)", flush=True)
            time.sleep(5)
    print(f"Proxying http://{BIND}:{PORT} -> {UPSTREAM}", flush=True)
    srv.serve_forever()
