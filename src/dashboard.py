#!/usr/bin/env python3
"""Minimal kiosk9 control dashboard and fleet proxy."""

import configparser
import concurrent.futures
import hashlib
import http.client
import json
import mimetypes
import os
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = Path(os.environ.get("VIDEO_SYNC_CONFIG", "/etc/video-sync/config.ini"))
STATIC = Path(os.environ.get("VIDEO_SYNC_STATIC", "/opt/video-sync/web"))
cfg = configparser.ConfigParser(interpolation=None)
if not cfg.read(CONFIG):
    raise SystemExit(f"Cannot read {CONFIG}")
TOKEN = os.environ.get("PI_SYNC_API_TOKEN", "")
if not TOKEN:
    raise SystemExit("PI_SYNC_API_TOKEN is not configured")
PORT = cfg.getint("web", "dashboard_port", fallback=8080)
AGENT_PORT = cfg.getint("web", "agent_port", fallback=5010)
LIMIT = cfg.getint("web", "upload_limit_gb", fallback=20) * 1024**3
MASTER = cfg.get("sync", "master_device")
DEVICES = dict(cfg.items("devices"))
VIDEO_HOME = Path(os.environ.get("VIDEO_SYNC_VIDEO_DIR", f"/home/{MASTER}/mov"))
ALLOWED = {".mp4", ".mov", ".mkv", ".webm"}


def agent_request(device, method, path, body=None, headers=None, timeout=8):
    if device not in DEVICES:
        raise ValueError(f"unknown device: {device}")
    connection = http.client.HTTPConnection(DEVICES[device], AGENT_PORT, timeout=timeout)
    request_headers = {"X-Pi-Sync-Token": TOKEN}
    request_headers.update(headers or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    parsed = json.loads(data or b"{}")
    if response.status >= 300:
        raise RuntimeError(parsed.get("error", f"HTTP {response.status}"))
    return parsed


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_to(device, path, digest):
    size = path.stat().st_size
    with path.open("rb") as stream:
        return agent_request(device, "POST", "/api/upload", stream, {
            "Content-Length": str(size),
            "X-Filename": urllib.parse.quote(path.name),
            "X-SHA256": digest,
            "Content-Type": "application/octet-stream",
        }, timeout=max(60, int(size / 2_000_000)))


class Handler(BaseHTTPRequestHandler):
    server_version = "PiSyncDashboard/1"

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}", flush=True)

    def json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        return self.headers.get("X-Pi-Sync-Token", "") == TOKEN

    def require_auth(self):
        if self.authorized():
            return True
        self.json(401, {"error": "Enter the dashboard token"})
        return False

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            if not self.require_auth():
                return
            if path == "/api/status":
                self.status_all()
            elif path == "/api/videos":
                try:
                    self.json(200, agent_request(MASTER, "GET", "/api/videos"))
                except Exception as exc:
                    self.json(502, {"error": str(exc)})
            elif path == "/api/config":
                self.json(200, {"master": MASTER, "devices": DEVICES})
            else:
                self.json(404, {"error": "not found"})
            return
        self.static(path)

    def do_POST(self):
        if not self.require_auth():
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/upload":
                self.upload()
            elif path == "/api/distribute":
                self.distribute(self.read_json())
            elif path == "/api/action":
                self.action(self.read_json())
            else:
                self.json(404, {"error": "not found"})
        except Exception as exc:
            self.json(400, {"error": str(exc)})

    def status_all(self):
        def inspect(item):
            name, address = item
            try:
                value = agent_request(name, "GET", "/api/health", timeout=2)
                value.update({"online": True, "address": address})
            except Exception as exc:
                value = {"device": name, "address": address, "online": False, "error": str(exc)}
            return name, value
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(DEVICES)) as pool:
            result = dict(pool.map(inspect, DEVICES.items()))
        self.json(200, {"devices": result})

    def upload(self):
        name = Path(urllib.parse.unquote(self.headers.get("X-Filename", ""))).name
        length = int(self.headers.get("Content-Length", "0"))
        if not name or Path(name).suffix.lower() not in ALLOWED:
            raise ValueError("choose an MP4, MOV, MKV, or WebM video")
        if length <= 0 or length > LIMIT:
            raise ValueError("invalid upload size")
        VIDEO_HOME.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".pi-sync-", dir=VIDEO_HOME)
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as output:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("upload ended early")
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, VIDEO_HOME / name)
            os.chmod(VIDEO_HOME / name, 0o644)
            self.json(201, {"ok": True, "name": name, "size": length, "sha256": digest.hexdigest()})
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def distribute(self, data):
        video = Path(str(data.get("video", ""))).name
        path = VIDEO_HOME / video
        if not path.is_file():
            raise ValueError("video is not on kiosk9")
        selected = [name for name in data.get("devices", []) if name != MASTER]
        if not selected:
            raise ValueError("select at least one client")
        digest = file_digest(path)
        def send(name):
            try:
                upload_to(name, path, digest)
                return name, {"ok": True}
            except Exception as exc:
                return name, {"ok": False, "error": str(exc)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = dict(pool.map(send, selected))
        self.json(200, {"results": results, "sha256": digest})

    def action(self, data):
        action = data.get("action")
        if action not in {"sync", "local", "stop"}:
            raise ValueError("unsupported action")
        selected = data.get("devices", [])
        video = Path(str(data.get("video", ""))).name if data.get("video") else None
        if not selected:
            raise ValueError("select at least one device")
        def send(name):
            try:
                value = agent_request(name, "POST", f"/api/action/{action}", {"video": video})
                return name, value
            except Exception as exc:
                return name, {"ok": False, "error": str(exc)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = dict(pool.map(send, selected))
        self.json(200, {"results": results})

    def static(self, path):
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"Pi Sync dashboard: http://0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
