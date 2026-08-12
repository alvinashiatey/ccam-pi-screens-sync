#!/usr/bin/env python3
"""Unified master/client synchronizer for an existing mpv IPC instance."""

import configparser
import json
import os
import signal
import socket
import sys
import time
import uuid
from pathlib import Path


class Mpv:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.reader = self.sock.makefile("r")
        self.request_id = 0

    def command(self, command):
        self.request_id += 1
        rid = self.request_id
        payload = json.dumps({"command": command, "request_id": rid}) + "\n"
        self.sock.sendall(payload.encode())
        while True:
            line = self.reader.readline()
            if not line:
                raise RuntimeError("mpv IPC connection closed")
            response = json.loads(line)
            if response.get("request_id") != rid:
                continue
            if response.get("error") != "success":
                raise RuntimeError(f"mpv error: {response.get('error')}")
            return response.get("data")

    def get(self, name):
        return self.command(["get_property", name])

    def set(self, name, value):
        return self.command(["set_property", name, value])

    def close(self):
        self.reader.close()
        self.sock.close()


def csv(value):
    return [item.strip() for item in value.replace("\n", "").split(",") if item.strip()]


def wait_for_duration(mpv):
    for _ in range(100):
        time.sleep(0.05)
        try:
            duration = mpv.get("duration")
            if duration and float(duration) > 0:
                return float(duration)
        except Exception:
            pass
    raise RuntimeError("could not determine video duration")


def prepare(mpv, video):
    mpv.set("pause", True)
    mpv.set("speed", 1.0)
    mpv.command(["loadfile", video, "replace"])
    duration = wait_for_duration(mpv)
    mpv.set("pause", True)
    mpv.set("time-pos", 0)
    mpv.set("speed", 1.0)
    return duration


def circular_error(current, target, duration):
    error = current - target
    half = duration / 2.0
    if error > half:
        error -= duration
    elif error < -half:
        error += duration
    return error


def correct(mpv, expected, duration, soft, hard, slow, fast):
    current = mpv.get("time-pos")
    if current is None:
        return None
    error = circular_error(float(current), expected, duration)
    if abs(error) >= hard:
        mpv.set("time-pos", expected)
        mpv.set("speed", 1.0)
    elif error > soft:
        mpv.set("speed", slow)
    elif error < -soft:
        mpv.set("speed", fast)
    else:
        mpv.set("speed", 1.0)
    return error


def master(mpv, cfg, duration):
    port = cfg.getint("sync", "port")
    interval = cfg.getfloat("sync", "interval")
    epoch = time.time() + cfg.getfloat("sync", "start_delay")
    targets = csv(cfg.get("sync", "targets"))
    if not targets:
        raise RuntimeError("no client targets are configured")
    destinations = []
    for target in targets:
        try:
            destinations.append((socket.gethostbyname(target), port))
        except socket.gaierror as exc:
            print(f"WARNING: cannot resolve target {target}: {exc}", flush=True)
    if not destinations:
        raise RuntimeError("none of the configured client targets resolved")

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    session = str(uuid.uuid4())
    started = False
    sequence = 0
    last_status = 0.0
    print(f"MASTER session={session} clients={len(destinations)} start={epoch:.3f}", flush=True)
    try:
        while True:
            now = time.time()
            packet = json.dumps({"version": 1, "session": session, "epoch": epoch,
                                 "duration": duration, "sequence": sequence}).encode()
            for destination in destinations:
                udp.sendto(packet, destination)
            sequence += 1
            expected = (now - epoch) % duration if now >= epoch else 0.0
            if not started and now >= epoch:
                mpv.set("time-pos", expected)
                mpv.set("pause", False)
                started = True
            elif started:
                correct(mpv, expected, duration,
                        cfg.getfloat("sync", "soft_threshold"),
                        cfg.getfloat("sync", "hard_threshold"),
                        cfg.getfloat("sync", "slow_speed"),
                        cfg.getfloat("sync", "fast_speed"))
            if time.monotonic() - last_status >= 5:
                state = f"position={expected:.3f}" if started else f"starts_in={max(epoch-now, 0):.2f}"
                print(f"MASTER {state} packets={sequence}", flush=True)
                last_status = time.monotonic()
            time.sleep(interval)
    finally:
        udp.close()


def client(mpv, cfg, duration):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind(("", cfg.getint("sync", "port")))
    udp.settimeout(cfg.getfloat("sync", "packet_timeout"))
    session = None
    playing = False
    last_status = 0.0
    print(f"CLIENT listening port={cfg.getint('sync', 'port')}", flush=True)
    try:
        while True:
            try:
                packet, address = udp.recvfrom(4096)
            except socket.timeout:
                print("WARNING: waiting for master packets", flush=True)
                continue
            try:
                message = json.loads(packet.decode())
                new_session = str(message["session"])
                epoch = float(message["epoch"])
            except (ValueError, KeyError, TypeError):
                continue
            if session != new_session:
                session = new_session
                playing = False
                mpv.set("pause", True)
                mpv.set("speed", 1.0)
                print(f"CLIENT session={session} master={address[0]}", flush=True)
            now = time.time()
            if now < epoch:
                continue
            expected = (now - epoch) % duration
            if not playing:
                mpv.set("time-pos", expected)
                mpv.set("pause", False)
                playing = True
                continue
            error = correct(mpv, expected, duration,
                            cfg.getfloat("sync", "soft_threshold"),
                            cfg.getfloat("sync", "hard_threshold"),
                            cfg.getfloat("sync", "slow_speed"),
                            cfg.getfloat("sync", "fast_speed"))
            if error is not None and time.monotonic() - last_status >= 5:
                print(f"CLIENT position={expected:.3f} drift_ms={error*1000:+.1f}", flush=True)
                last_status = time.monotonic()
    finally:
        udp.close()


def main():
    config_path = Path(os.environ.get("VIDEO_SYNC_CONFIG", "/etc/video-sync/config.ini"))
    active_path = Path(os.environ.get("VIDEO_SYNC_ACTIVE", "/var/lib/video-sync/active-video"))
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(config_path):
        raise RuntimeError(f"cannot read configuration: {config_path}")
    hostname = socket.gethostname().split(".")[0].lower()
    master_hostname = cfg.get("sync", "master_hostname").split(".")[0].lower()
    role = "master" if hostname == master_hostname else "client"
    home = os.path.expanduser("~")
    configured = cfg.get("mpv", "default_video").replace("{home}", home).replace("{hostname}", hostname)
    video = active_path.read_text().strip() if active_path.exists() else configured
    video = str(Path(video).expanduser().resolve())
    if not Path(video).is_file():
        raise RuntimeError(f"video does not exist: {video}")
    socket_path = cfg.get("mpv", "socket")
    if not Path(socket_path).is_socket():
        raise RuntimeError(f"mpv socket does not exist: {socket_path}")

    mpv = Mpv(socket_path)
    stopping = False
    def stop(_signum, _frame):
        nonlocal stopping
        if stopping:
            os._exit(1)
        stopping = True
        try:
            mpv.set("speed", 1.0)
            mpv.set("pause", True)
        finally:
            raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        duration = prepare(mpv, video)
        print(f"START role={role} hostname={hostname} video={video} duration={duration:.3f}", flush=True)
        (master if role == "master" else client)(mpv, cfg, duration)
    finally:
        try:
            mpv.set("speed", 1.0)
        except Exception:
            pass
        mpv.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
