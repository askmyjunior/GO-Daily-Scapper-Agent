"""goir-relay: fetch from the GOIR portal for the daily sync, from Mumbai.

Deployed on Google Cloud Run in asia-south1 (Mumbai). The daily sync runs on
GitHub Actions in the US, and goir.ap.gov.in answers only connections from
India — tested 2026-10-04 from 40 locations: Mumbai answered, all 39 others
timed out. So the sync sends its portal requests here and this makes them.

Why Python and not a Supabase Edge Function: the portal's server completes a
TLS handshake only with TLS 1.2 CBC ciphers (ECDHE-RSA-AES256-SHA384) and
resets anything offering only AEAD suites or TLS 1.3. Deno's TLS (rustls)
offers only those, so an Edge Function in Mumbai connected in 63 ms and was
reset at the handshake. Python's OpenSSL still offers CBC suites.

Deliberately not a general proxy:
  * one host, hard-coded: https://goir.ap.gov.in
  * two paths: the listing page "/" (GET, or the form's POST) and the
    document download "/dgo.ashx?gid=<digits>&fileType=E|T" (GET)
  * a shared secret in `x-relay-token` (env GOIR_RELAY_TOKEN), compared in
    constant time; without it every request is refused

The response body is the portal's, byte for byte. The portal's status and its
session cookie come back as headers, so the caller carries the ASP.NET session
exactly as it would talking to the portal directly. Standard library only.
"""
from __future__ import annotations

import gzip
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTAL = "https://goir.ap.gov.in"
PATH_OK = re.compile(r"^/(dgo\.ashx\?gid=\d{1,12}&fileType=[ET])?$")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
MAX_REQUEST = 2 << 20          # the listing form's POST carries ~100 KB of ViewState
REGION = os.environ.get("RELAY_REGION", "asia-south1")


class Relay(BaseHTTPRequestHandler):
    server_version = "goir-relay"

    def _send(self, code: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {"Content-Type": "text/plain; charset=utf-8"}).items():
            self.send_header(k, v)
        self.send_header("x-relay-region", REGION)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send(405, b"POST only")

    def do_POST(self) -> None:  # noqa: N802
        token = os.environ.get("GOIR_RELAY_TOKEN", "")
        if not token or not hmac.compare_digest(self.headers.get("x-relay-token", ""), token):
            return self._send(403, b"forbidden")
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_REQUEST:
            return self._send(413, b"request too large")
        try:
            spec = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            return self._send(400, b"body must be JSON")

        method = "POST" if spec.get("method") == "POST" else "GET"
        path = spec.get("path") or "/"
        if not isinstance(path, str) or not PATH_OK.match(path):
            return self._send(400, b"path not allowed")
        if method == "POST" and path != "/":
            return self._send(400, b"POST is only for the listing form")

        headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
        if spec.get("cookie"):
            headers["Cookie"] = str(spec["cookie"])
        data = None
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = str(spec.get("body") or "").encode()

        req = urllib.request.Request(PORTAL + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=80) as resp:
                status, rh, body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, rh, body = e.code, e.headers, e.read()
        except Exception as e:  # noqa: BLE001 — reported to the caller, which retries
            return self._send(502, f"portal fetch failed: {type(e).__name__}: {e}".encode())

        if (rh.get("Content-Encoding") or "").lower() == "gzip":
            body = gzip.decompress(body)
        out = {"Content-Type": rh.get("Content-Type", "application/octet-stream"),
               "x-goir-status": str(status)}
        cookie = rh.get("Set-Cookie")
        if cookie:
            out["x-goir-set-cookie"] = cookie
        self._send(200, body, out)

    def log_message(self, fmt: str, *args) -> None:
        # One line per request: the method, the path the CALLER asked for, the
        # status. Never a body, a cookie or a header that could carry the token.
        print(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"goir-relay listening on {port} in {REGION}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Relay).serve_forever()
