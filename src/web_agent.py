#!/usr/bin/env python3
"""Authenticated local control API used by every display Pi."""

import configparser
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = Path(os.environ.get("VIDEO_SYNC_CONFIG", "/etc/video-sync/config.ini"))
DEVICE = os.environ.get("VIDEO_SYNC_DEVICE", "unknown")
VIDEO_HOME = Path(os.environ.get("VIDEO_SYNC_VIDEO_DIR", Path(os.environ.get("VIDEO_SYNC_HOME", f"/home/{DEVICE}")) / "mov"))
cfg = configparser.ConfigParser(interpolation=None)
if not cfg.read(CONFIG):
    raise SystemExit(f"Cannot read {CONFIG}")
TOKEN = os.environ.get("PI_SYNC_API_TOKEN", "")
if not TOKEN:
    raise SystemExit("PI_SYNC_API_TOKEN is not configured")
PORT = cfg.getint("web", "agent_port", fallback=5010)
LIMIT = cfg.getint("web", "upload_limit_gb", fallback=20) * 1024**3
ALLOWED = {".mp4", ".mov", ".mkv", ".webm"}


def run(*args):
    return subprocess.run(args, text=True, capture_output=True, timeout=8)


def service_state():
    result = run("systemctl", "is-active", "video-sync.service")
    return result.stdout.strip() or "unknown"


def clock_offset():
    result = run("chronyc", "tracking")
    for line in result.stdout.splitlines():
        if line.strip().startswith("Last offset"):
            return line.split(":", 1)[1].strip()
    return None


def videos():
    VIDEO_HOME.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(VIDEO_HOME.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file() and path.suffix.lower() in ALLOWED:
            stat = path.stat()
            items.append({"name": path.name, "size": stat.st_size, "modified": stat.st_mtime})
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "PiSyncAgent/1"

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}", flush=True)

    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        return self.headers.get("X-Pi-Sync-Token", "") == TOKEN

    def require_auth(self):
        if self.authorized():
            return True
        self.send_json(401, {"error": "unauthorized"})
        return False

    def body_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if not self.require_auth():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            usage = shutil.disk_usage(VIDEO_HOME if VIDEO_HOME.exists() else VIDEO_HOME.parent)
            active = Path("/var/lib/video-sync/active-video")
            sync_status = Path("/run/video-sync/status.json")
            detail = {}
            if sync_status.exists() and service_state() == "active":
                try:
                    detail = json.loads(sync_status.read_text())
                except (OSError, ValueError):
                    pass
            self.send_json(200, {
                "device": DEVICE,
                "service": service_state(),
                "mode": "sync" if service_state() == "active" else "local",
                "active_video": active.read_text().strip() if active.exists() else None,
                "clock_offset": clock_offset(),
                "disk_free": usage.free,
                "time": time.time(),
                "sync": detail,
            })
        elif path == "/api/videos":
            self.send_json(200, {"videos": videos()})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.require_auth():
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/upload":
                self.upload()
                return
            data = self.body_json()
            if path == "/api/action/sync":
                video = safe_video(data.get("video"))
                Path("/var/lib/video-sync").mkdir(parents=True, exist_ok=True)
                Path("/var/lib/video-sync/active-video").write_text(str(video) + "\n")
                run_checked("systemctl", "restart", "video-sync.service")
            elif path == "/api/action/local":
                video = safe_video(data.get("video"))
                run_checked("systemctl", "stop", "video-sync.service")
                run_checked("/opt/video-sync/mpv_control.py", str(video))
            elif path == "/api/action/stop":
                run_checked("systemctl", "stop", "video-sync.service")
                mpv_command(["set_property", "pause", True])
            else:
                self.send_json(404, {"error": "not found"})
                return
            self.send_json(200, {"ok": True, "device": DEVICE})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def upload(self):
        name = Path(urllib.parse.unquote(self.headers.get("X-Filename", ""))).name
        expected = self.headers.get("X-SHA256", "").lower()
        length = int(self.headers.get("Content-Length", "0"))
        if not name or Path(name).suffix.lower() not in ALLOWED:
            raise ValueError("unsupported video filename")
        if length <= 0 or length > LIMIT:
            raise ValueError("invalid upload size")
        VIDEO_HOME.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        fd, temporary = tempfile.mkstemp(prefix=".pi-sync-", dir=VIDEO_HOME)
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
            actual = digest.hexdigest()
            if expected and actual != expected:
                raise ValueError("checksum mismatch")
            os.replace(temporary, VIDEO_HOME / name)
            os.chmod(VIDEO_HOME / name, 0o644)
            self.send_json(201, {"ok": True, "name": name, "size": length, "sha256": actual})
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def safe_video(name):
    clean = Path(str(name or "")).name
    path = (VIDEO_HOME / clean).resolve()
    if path.parent != VIDEO_HOME.resolve() or not path.is_file() or path.suffix.lower() not in ALLOWED:
        raise ValueError("video not found")
    return path


def run_checked(*args):
    result = run(*args)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "command failed")


def mpv_command(command):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect("/tmp/mpv-sync.sock")
        client.sendall((json.dumps({"command": command, "request_id": 1}) + "\n").encode())
        response = json.loads(client.makefile("r").readline())
        if response.get("error") != "success":
            raise RuntimeError(f"mpv error: {response.get('error')}")
    finally:
        client.close()


if __name__ == "__main__":
    VIDEO_HOME.mkdir(parents=True, exist_ok=True)
    print(f"Pi Sync agent: {DEVICE} on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
