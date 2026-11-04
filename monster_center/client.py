"""Tiny JSON-over-unix-socket client used by the GUI and the CLI."""

from __future__ import annotations

import json
import socket

SOCKET_PATH = "/run/monster-center/sock"


class DaemonError(RuntimeError):
    pass


def request(payload, timeout=30):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(SOCKET_PATH)
            sock.sendall(json.dumps(payload).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout) as exc:
        raise DaemonError(f"monster-center daemon not reachable: {exc}") from exc
    if not buf.strip():
        raise DaemonError("empty reply from the daemon")
    return json.loads(buf.decode())


def telemetry():
    return request({"op": "telemetry"})


def call(op, **kwargs):
    payload = {"op": op}
    payload.update(kwargs)
    return request(payload)
