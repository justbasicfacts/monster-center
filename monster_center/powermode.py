#!/usr/bin/env python3
"""Chassis power-mode key bridge for Tongfang GM5MG0Y laptops.

The embedded controller keeps the firmware power mode in extended RAM at
address 0x0751, which the DSDT exposes through a 64 KiB window at 0xFE410000.
Masking the byte with 0xB0 gives:

    0xA0  Silent
    0x00  Balanced
    0x10  Performance
    0x80  Auto (mode LED off)

Two roles, matching the two services in systemd/:

    watch   runs as root, reads the byte, debounces it and publishes the mode
            name to /run/monster-center/powermode
    popup   runs in the desktop session, follows that file, applies the
            matching monster-center profile through the daemon and shows a
            desktop notification

Usage:
    python3 -m monster_center.powermode watch
    python3 -m monster_center.powermode popup
    python3 -m monster_center.powermode status
"""

from __future__ import annotations

import mmap
import os
import subprocess
import sys
import time

from . import client

BASE = 0xFE410000
OFFSET = 0x751
MASK = 0xA0 | 0x10 | 0x80
MODE_FILE = "/run/monster-center/powermode"
STABLE_SECONDS = 1.0

NAMES = {0xA0: "silent", 0x00: "balanced", 0x10: "performance", 0x80: "auto"}
LABELS = {
    "silent": "Silent",
    "balanced": "Balanced",
    "performance": "Performance",
    "auto": "Auto",
}
DETAIL = {
    "silent": "quiet fans, low power",
    "balanced": "everyday profile",
    "performance": "full fan curve and power",
    "auto": "EC default, mode LED off",
}
# Auto is the EC's "no profile" state with the LED off; balanced is the closest
# OS equivalent, and it keeps every press of the button doing something.
APPLY = {
    "silent": "silent",
    "balanced": "balanced",
    "performance": "performance",
    "auto": "balanced",
}


def read_ec_mode():
    """Read and decode the firmware power mode from the EC memory window."""
    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    try:
        with mmap.mmap(fd, 0x10000, mmap.MAP_SHARED, mmap.PROT_READ, offset=BASE) as mem:
            raw = mem[OFFSET]
    finally:
        os.close(fd)
    value = raw & MASK
    return NAMES.get(value, f"unknown 0x{value:02x}")


def publish(mode):
    os.makedirs(os.path.dirname(MODE_FILE), exist_ok=True)
    tmp = MODE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(mode + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, MODE_FILE)


def read_published():
    try:
        with open(MODE_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def watch():
    published = None
    candidate = None
    since = 0.0
    while True:
        try:
            mode = read_ec_mode()
        except OSError as exc:
            print(f"cannot read the EC memory window: {exc}", flush=True)
            time.sleep(5)
            continue
        now = time.monotonic()
        if mode != candidate:
            candidate = mode
            since = now
        elif now - since >= STABLE_SECONDS and mode != published:
            publish(mode)
            published = mode
            print(f"power mode: {mode}", flush=True)
        time.sleep(0.25)


def apply_profile(mode):
    """Apply the matching profile through the daemon, return the last message."""
    name = APPLY.get(mode)
    if not name:
        return ""
    try:
        reply = client.call("profile", name=name)
    except client.DaemonError as exc:
        return f"daemon unreachable: {exc}"
    messages = reply.get("messages") or []
    return messages[-1] if messages else ""


def notify(mode, applied):
    label = LABELS.get(mode, mode.title())
    body = applied
    for prefix in ("ok: ", "verified: "):
        body = body.removeprefix(prefix)
    if not body:
        body = DETAIL.get(mode, "")
    try:
        subprocess.run(
            ["notify-send", "-a", "Monster Center", "-t", "3000",
             f"Power mode: {label}", body],
            check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def log_path():
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "monster-center-powermode.log")


def log(line):
    try:
        with open(log_path(), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def popup():
    last = None
    while True:
        mode = read_published()
        if not mode:
            time.sleep(0.5)
            continue
        if last is None:
            applied = apply_profile(mode)
            log(f"{time.strftime('%F %T')} startup {mode} {applied}")
            last = mode
        elif mode != last:
            applied = apply_profile(mode)
            notify(mode, applied)
            log(f"{time.strftime('%F %T')} {mode} {applied}")
            last = mode
        time.sleep(0.4)


def status():
    live = "unavailable"
    try:
        live = read_ec_mode()
    except OSError as exc:
        live = f"error: {exc}"
    print(f"live EC mode: {live}")
    print(f"published:    {read_published() or '-'}")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "status"
    if action == "watch":
        watch()
    elif action == "popup":
        popup()
    elif action == "status":
        status()
        return 0
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
