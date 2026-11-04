#!/usr/bin/env python3
"""Command line front end for Monster Center."""

from __future__ import annotations

import argparse
import json
import sys

from . import client, hardware


def fmt(value, suffix="", none="n/a"):
    if value is None:
        return none
    if isinstance(value, float):
        return f"{value:.1f}{suffix}"
    return f"{value}{suffix}"


def print_status(data):
    ec = data.get("ec", {})
    cpu = data.get("cpu", {})
    gpu = data.get("gpu", {})
    power = data.get("power", {})
    bat = data.get("battery", {})
    ram = data.get("ram", {})
    temps = data.get("temps", {})
    cores = temps.get("cpu_cores", {})
    package = next((v for k, v in cores.items() if "Package" in k), None)
    hottest = max(cores.values()) if cores else None

    print("Monster Center - TULPAR T5 V21.4")
    print("=" * 46)
    print(f"CPU      {fmt(package, ' C')} package / {fmt(hottest, ' C')} hottest core")
    print(f"         {cpu.get('cur_freq_mhz', '?')} MHz now, "
          f"{cpu.get('min_freq_mhz', '?')}-{cpu.get('max_freq_mhz', '?')} MHz range")
    print(f"         governor {cpu.get('governor')}, epp {cpu.get('epp')}, "
          f"turbo {'on' if cpu.get('turbo') else 'off'}")
    print(f"         package power {fmt(power.get('package_w'), ' W')} "
          f"(PL1 {fmt(power.get('pl1_w'), ' W')}, PL2 {fmt(power.get('pl2_w'), ' W')})")
    if gpu.get("available"):
        print(f"GPU      {fmt(gpu.get('temp'), ' C')}, {fmt(gpu.get('power_draw'), ' W')} draw, "
              f"{gpu.get('util')}% load, {gpu.get('clock_graphics')} MHz, "
              f"vram {gpu.get('mem_used')}/{gpu.get('mem_total')} MiB")
        print(f"         {gpu.get('name')}, power limit {fmt(gpu.get('power_limit'), ' W')}")
    else:
        print("GPU      not reachable")
    if ec.get("available"):
        print(f"Fans     fan 1 {ec.get('fan1_pct')}% level, fan 2 {ec.get('fan2_pct')}% level "
              f"(EC controlled)")
        print(f"EC       board {ec.get('board_temp')} C, gpu {ec.get('gpu_temp')} C")
    else:
        print("Fans     EC not readable (ec_sys not loaded?)")
    other = []
    for label, values in (("pch", temps.get("pch", {})), ("nvme", temps.get("nvme", {})),
                          ("wifi", temps.get("wifi", {}))):
        for key, value in values.items():
            other.append(f"{label} {value} C")
    if other:
        print("Sensors  " + ", ".join(other))
    if bat.get("present"):
        print(f"Battery  {bat.get('capacity')}% ({bat.get('status')}), "
              f"AC {'plugged' if bat.get('ac_online') else 'unplugged'}")
    if ram.get("available"):
        used, total = ram.get("used_mb", 0), ram.get("total_mb", 0)
        pct = round(used * 100 / total) if total else 0
        swap = (f", swap {ram.get('swap_used_mb')}/{ram.get('swap_total_mb')} MiB"
                if ram.get("swap_total_mb") else ", no swap")
        print(f"RAM      {used} / {total} MiB used ({pct}%){swap}")
    kbd = data.get("keyboard") or {}
    if kbd.get("available"):
        print(f"Keyboard {kbd.get('zones', 4)} addressable zones on {kbd.get('device')}")
    else:
        print("Keyboard backlight controller (048d:ce00) not found")
    print(f"Graphics {data.get('graphics_mode') or 'unknown'}")
    state = data.get("state", {})
    profile = state.get("profile")
    label = (hardware.PROFILES.get(profile, {}).get("label") if profile else "custom")
    mode = data.get("mode") or hardware.power_state()
    guard = state.get("guard", {})
    note = ""
    if guard.get("limited") and guard.get("pl1"):
        note = f", thermal guard holding PL1 at {guard['pl1']:.0f} W"
    print(f"Mode     {label} - {hardware.describe_state(mode)}{note}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="monster-center",
                                     description="Control centre for the Monster TULPAR T5")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show a live hardware summary")
    sub.add_parser("gui", help="open the graphical control centre")

    p = sub.add_parser("profile", help="apply a power profile")
    p.add_argument("name", nargs="?",
                   choices=list(hardware.PROFILES) + ["list"])
    p.add_argument("--list", action="store_true")

    p = sub.add_parser("cpu", help="tune the CPU")
    p.add_argument("--governor")
    p.add_argument("--epp")
    p.add_argument("--turbo", choices=["on", "off"])
    p.add_argument("--max-freq", type=int, dest="max_freq")

    p = sub.add_parser("power", help="set the CPU power budget")
    p.add_argument("--pl1", type=float)
    p.add_argument("--pl2", type=float)

    p = sub.add_parser("gpu", help="set the GPU power limit")
    p.add_argument("--power-limit", type=float, dest="power_limit")

    p = sub.add_parser("graphics", help="show or change the graphics mode")
    p.add_argument("mode", nargs="?", choices=["hybrid", "nvidia", "integrated", "compute"])

    p = sub.add_parser("led", help="keyboard backlight (4 zones)")
    p.add_argument("--rgb", help="colour for every zone, as R,G,B")
    p.add_argument("--zones", help="per zone colours: R,G,B;R,G,B;R,G,B;R,G,B")
    p.add_argument("--brightness", type=int, default=30,
                   help="brightness 0-50, or brightness only when no colour is given")
    p.add_argument("--off", action="store_true")
    p.add_argument("--effect", choices=None, default=None)

    args = parser.parse_args(argv)

    if args.command in (None, "status"):
        try:
            print_status(client.telemetry()["data"])
        except client.DaemonError as exc:
            print(f"{exc}\nStart it with: sudo systemctl start monster-center", file=sys.stderr)
            return 1
        return 0

    if args.command == "gui":
        from .gui import main as gui_main
        return gui_main()

    try:
        if args.command == "profile":
            if args.list or args.name in (None, "list"):
                for name, profile in hardware.PROFILES.items():
                    print(f"{name:<12} {profile['label']:<14} {profile['description']}")
                return 0
            reply = client.call("profile", name=args.name)
        elif args.command == "cpu":
            payload = {}
            if args.governor:
                payload["governor"] = args.governor
            if args.epp:
                payload["epp"] = args.epp
            if args.turbo:
                payload["turbo"] = args.turbo == "on"
            if args.max_freq:
                payload["max_freq_mhz"] = args.max_freq
            if not payload:
                print("nothing to change", file=sys.stderr)
                return 1
            reply = client.call("cpu", **payload)
        elif args.command == "power":
            reply = client.call("power", pl1=args.pl1, pl2=args.pl2)
        elif args.command == "gpu":
            reply = client.call("gpu", power_limit=args.power_limit)
        elif args.command == "graphics":
            if args.mode is None:
                print(hardware.graphics_mode())
                return 0
            reply = client.call("graphics", mode=args.mode)
        elif args.command == "led":
            if args.off:
                reply = client.call("led", off=True)
            elif args.effect:
                reply = client.call("led", effect=args.effect, brightness=args.brightness)
            elif args.zones:
                zones = []
                for part in args.zones.split(";"):
                    part = part.strip()
                    if part:
                        zones.append([int(c) for c in part.split(",")])
                reply = client.call("led", zones=zones, brightness=args.brightness)
            elif args.rgb:
                rgb = [int(part) for part in args.rgb.split(",")]
                reply = client.call("led", rgb=rgb, brightness=args.brightness)
            else:
                reply = client.call("led", brightness_only=True,
                                    brightness=args.brightness)
        else:
            parser.print_help()
            return 1
    except client.DaemonError as exc:
        print(f"{exc}\nStart it with: sudo systemctl start monster-center", file=sys.stderr)
        return 1

    for message in reply.get("messages", []):
        print(("ok: " if reply.get("ok") else "failed: ") + message)
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
