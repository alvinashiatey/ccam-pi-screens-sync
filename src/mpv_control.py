#!/usr/bin/env python3
"""Small command-line controller for the kiosk's existing mpv process."""

import argparse
import json
import socket
import sys
from pathlib import Path


def command(socket_path, value):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(socket_path)
        client.sendall((json.dumps({"command": value, "request_id": 1}) + "\n").encode())
        reader = client.makefile("r")
        for line in reader:
            response = json.loads(line)
            if response.get("request_id") != 1:
                continue
            if response.get("error") != "success":
                raise RuntimeError(f"mpv error: {response.get('error')}")
            return
        raise RuntimeError("mpv IPC connection closed")
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--socket", default="/tmp/mpv-sync.sock")
    args = parser.parse_args()
    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        raise RuntimeError(f"video does not exist: {video}")
    command(args.socket, ["loadfile", str(video), "replace"])
    command(args.socket, ["set_property", "speed", 1.0])
    command(args.socket, ["set_property", "pause", False])
    print(f"Local video: {video}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
