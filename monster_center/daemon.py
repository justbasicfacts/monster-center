#!/usr/bin/env python3
"""Privileged backend for Monster Center.

Runs as root under systemd, owns the EC/sysfs/nvidia-smi/USB work and serves a
JSON protocol on a unix socket that only the local desktop user can connect to.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import signal
import socket
import stat
import sys
import threading
import time
import traceback

from . import hardware

SOCK_DIR = "/run/monster-center"
SOCKET_PATH = os.path.join(SOCK_DIR, "sock")
STATE_DIR = "/var/lib/monster-center"
CONFIG_PATH = "/etc/monster-center.conf"

GUARD_HIGH = 90.0
GUARD_RESUME = 82.0
GUARD_STEP_W = 5
GUARD_FLOOR_W = 15

meter = hardware.PowerMeter()
state_lock = threading.Lock()
state = {
    "profile": None,
    "applied": None,
    "guard": {"enabled": True, "limited": False, "pl1": None},
    "last_error": None,
}

log_lock = threading.Lock()


def log(msg):
    with log_lock:
        print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def save_state():
    """Persist the small amount of state the UI restores after a restart."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with state_lock:
            snapshot_state = {
                "profile": state["profile"],
                "applied": state["applied"],
                "guard": dict(state["guard"]),
            }
        tmp = os.path.join(STATE_DIR, "state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(snapshot_state, f)
        os.replace(tmp, os.path.join(STATE_DIR, "state.json"))
    except OSError as exc:
        log(f"could not save state: {exc}")


def desktop_user():
    """The account that is allowed to talk to the daemon."""
    user = os.environ.get("MONSTER_CENTER_USER")
    if user:
        return pwd.getpwnam(user)
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                if line.startswith("user"):
                    name = line.split("=", 1)[1].strip()
                    if name:
                        return pwd.getpwnam(name)
    except OSError:
        pass
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return pwd.getpwnam(sudo_user)
    return pwd.getpwuid(0)


def cpu_package_temp(reading):
    cores = reading.get("temps", {}).get("cpu_cores", {})
    if not cores:
        return None
    for label, value in cores.items():
        if "Package" in label:
            return value
    hottest = max(cores.values())
    return hottest


def guard_loop():
    """Cap CPU power when the package runs hot, restore when it cools down."""
    while True:
        try:
            with state_lock:
                guard = dict(state["guard"])
                profile = state["profile"]
            if not guard["enabled"]:
                time.sleep(4)
                continue
            temp = cpu_package_temp(hardware.telemetry())
            limits = hardware.power_limits()
            if temp is None:
                time.sleep(4)
                continue
            if temp >= GUARD_HIGH and not guard["limited"]:
                floor = GUARD_FLOOR_W
                target = max(floor, limits["pl1_w"] - GUARD_STEP_W)
                hardware.set_power_limits(target, max(target, target + 5))
                with state_lock:
                    state["guard"].update(limited=True, pl1=target)
                log(f"thermal guard: {temp:.0f}C, PL1 capped to {target:.0f}W")
            elif guard["limited"] and temp <= GUARD_RESUME:
                restore = None
                if profile and profile in hardware.PROFILES:
                    restore = hardware.PROFILES[profile]["pl1"]
                if restore:
                    pl2 = hardware.PROFILES[profile].get("pl2") or restore + 10
                    hardware.set_power_limits(restore, pl2)
                with state_lock:
                    state["guard"].update(limited=False, pl1=restore)
                log(f"thermal guard released at {temp:.0f}C")
        except Exception:
            log("guard error:\n" + traceback.format_exc())
        time.sleep(4)


def snapshot():
    with state_lock:
        current = dict(state)
        current["guard"] = dict(state["guard"])
    data = hardware.telemetry(meter)
    data["state"] = current
    data["mode"] = hardware.power_state()
    data["identity"] = hardware.machine_identity()
    data["profiles"] = {
        name: {k: v for k, v in profile.items()}
        for name, profile in hardware.PROFILES.items()
    }
    return data


def handle(payload):
    op = payload.get("op")
    if op == "ping":
        return {"ok": True, "pong": time.time()}
    if op == "telemetry":
        return {"ok": True, "data": snapshot()}
    if op == "profile":
        name = payload.get("name")
        ok, messages, applied = hardware.profile_apply(name)
        if ok:
            with state_lock:
                state["profile"] = name
                state["applied"] = applied
                state["guard"]["limited"] = False
            save_state()
        else:
            with state_lock:
                state["last_error"] = "; ".join(messages)
        return {"ok": ok, "messages": messages, "profile": name if ok else None,
                "applied": applied if ok else None}
    if op == "cpu":
        results = []
        ok = True
        if "governor" in payload:
            done, msg = hardware.set_governor(payload["governor"])
            ok &= done
            results.append(msg)
        if "epp" in payload:
            done, msg = hardware.set_epp(payload["epp"])
            ok &= done
            results.append(msg)
        if "turbo" in payload:
            done, msg = hardware.set_turbo(bool(payload["turbo"]))
            ok &= done
            results.append(msg)
        if "max_freq_mhz" in payload:
            done, msg = hardware.set_max_freq_mhz(payload["max_freq_mhz"])
            ok &= done
            results.append(msg)
        if ok:
            # A hand tuned knob means the machine is no longer on a profile.
            with state_lock:
                state["profile"] = None
                state["applied"] = None
            save_state()
        return {"ok": ok, "messages": results}
    if op == "power":
        done, msg = hardware.set_power_limits(payload.get("pl1"),
                                             payload.get("pl2"))
        if done:
            with state_lock:
                state["profile"] = None
                state["applied"] = None
            save_state()
        return {"ok": done, "messages": [msg]}
    if op == "gpu":
        if "power_limit" in payload:
            done, msg = hardware.set_gpu_power_limit(payload["power_limit"])
            return {"ok": done, "messages": [msg]}
        return {"ok": False, "messages": ["nothing to do"]}
    if op == "graphics":
        done, msg = hardware.set_graphics_mode(payload.get("mode", "hybrid"))
        return {"ok": done, "messages": [msg]}
    if op == "led":
        if payload.get("off"):
            done, msg = hardware.led_off()
        elif payload.get("zones"):
            done, msg = hardware.led_set_zones(
                payload["zones"], payload.get("brightness", 30))
        elif payload.get("brightness_only"):
            done, msg = hardware.led_brightness(payload.get("brightness", 30))
        elif payload.get("effect"):
            done, msg = hardware.led_effect(
                payload["effect"],
                speed=payload.get("speed", 5),
                brightness=payload.get("brightness", 30),
                color=payload.get("color_name"),
                direction=payload.get("direction", "right"),
            )
        else:
            done, msg = hardware.led_set_color(payload.get("rgb", [255, 255, 255]),
                                              payload.get("brightness", 30))
        return {"ok": done, "messages": [msg]}
    if op == "guard":
        with state_lock:
            if "enabled" in payload:
                state["guard"]["enabled"] = bool(payload["enabled"])
            enabled = state["guard"]["enabled"]
        save_state()
        return {"ok": True, "messages": [f"thermal guard {'on' if enabled else 'off'}"]}
    return {"ok": False, "messages": [f"unknown op: {op}"]}


def serve_connection(conn):
    conn.settimeout(60)
    try:
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        payload = json.loads(buf.decode())
        try:
            reply = handle(payload)
        except Exception as exc:
            log("handler error:\n" + traceback.format_exc())
            reply = {"ok": False, "messages": [f"{type(exc).__name__}: {exc}"]}
        conn.sendall(json.dumps(reply, default=str).encode() + b"\n")
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception:
        log("connection error:\n" + traceback.format_exc())
    finally:
        try:
            conn.close()
        except OSError:
            pass


def prepare_socket():
    os.makedirs(SOCK_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    user = desktop_user()
    try:
        gid = user.pw_gid
        os.chown(SOCKET_PATH, 0, gid)
        os.chmod(SOCKET_PATH, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
        log(f"socket owned by root:{grp.getgrgid(gid).gr_name}")
    except OSError as exc:
        log(f"could not set socket ownership: {exc}")
    server.listen(16)
    return server


def main():
    log("monster-center daemon starting")
    os.makedirs("/sys/kernel/debug", exist_ok=True)
    if not os.path.exists(hardware.EC_IO):
        os.system("modprobe ec_sys >/dev/null 2>&1")
    try:
        loaded = json.load(open(os.path.join(STATE_DIR, "state.json")))
        with state_lock:
            state.update({k: v for k, v in loaded.items() if k in state})
    except (OSError, ValueError):
        pass

    threading.Thread(target=guard_loop, daemon=True).start()
    server = prepare_socket()

    def shutdown(*_):
        log("shutting down")
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        server.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            break
        threading.Thread(target=serve_connection, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
