"""Hardware access for Monster TULPAR T5 V21.4 on Linux.

This unit is a Tongfang GM5MG0Y barebone, not a Clevo one: the system serial
number starts with GM5MG0Y, the BIOS is AMI Aptio N.1.08MON06 and the EC is
an ITE part sitting behind the memory window the DSDT declares as
OperationRegion (ECMG, SystemMemory, 0xFE410000, 0x00010000).

Everything hardware specific lives here: sysfs paths, the EC layout of this
board, nvidia-smi calls and the graphics mode switch.

The EC layout below was mapped on this exact machine, from the DSDT field
list plus live probing of /sys/kernel/debug/ec/ec0/io (which mirrors the
first 256 bytes of the same window):

    0x36  CPU package temperature (deg C)   DSDT CPTM, window offset 0x43E
    0x3E  PCH / board temperature
    0x4F  GPU temperature                   DSDT VGAT, window offset 0x44F
    0x60  fan 1 level, 12 bit (bits 11..0 of the two bytes, big endian)
    0x68  fan 2 level, 12 bit

The DSDT also declares manual fan duty in the extended part of the window:
0xE8C holds F1DC (7 bit duty) and F1CM (manual enable), 0xE9D holds the same
for fan 2. Writing 0x80 | duty there is accepted and reads back, but the EC
keeps both fans on its own curve: in a 20 second test at 100 % duty the fan
level registers did not move and the fan speed fields at 0xE1C stayed at
zero. So fan levels stay read-only. What is controllable is the power budget
(RAPL for the CPU, nvidia-smi for the GPU) plus the usual cpufreq knobs, and
that is what the cooling profiles use.
"""

from __future__ import annotations

import glob
import json
import os
import re
import errno
import subprocess
import time

from . import led

EC_IO = "/sys/kernel/debug/ec/ec0/io"
EC_BOARD_TEMP = 0x3E        # tracks the chipset / board sensor
EC_GPU_TEMP = 0x4F
EC_FAN1 = 0x60
EC_FAN2 = 0x68
FAN_LEVEL_MAX = 0x0FFF

RAPL = "/sys/class/powercap/intel-rapl/intel-rapl:0"
INTEL_PSTATE = "/sys/devices/system/cpu/intel_pstate"
CPUFREQ = "/sys/devices/system/cpu/cpufreq"

PROFILES = {
    "silent": {
        "label": "Silent",
        "pl1": 20, "pl2": 25,
        "epp": "power", "turbo": False,
        "max_mhz": 2300,
        "gpu_w": 60, "led_brightness": 12,
        "description": "Quiet, cool, low clocks",
    },
    "balanced": {
        "label": "Balanced",
        "pl1": 35, "pl2": 45,
        "epp": "balance_power", "turbo": True,
        "gpu_w": 80, "led_brightness": 28,
        "description": "Everyday use, stock GPU budget",
    },
    "performance": {
        "label": "Performance",
        "pl1": 55, "pl2": 70,
        "epp": "performance", "turbo": True,
        "gpu_w": 100, "led_brightness": 38,
        "description": "High sustained clocks",
    },
    "gaming": {
        "label": "Gaming",
        "pl1": 70, "pl2": 90,
        "epp": "performance", "turbo": True,
        "gpu_w": 115, "led_brightness": 40,
        "description": "Maximum power budget",
    },
    "battery": {
        "label": "Battery saver",
        "pl1": 15, "pl2": 20,
        "epp": "power", "turbo": False,
        "max_mhz": 2300,
        "gpu_w": 40, "led_brightness": 8,
        "description": "Longest runtime, lowest power",
    },
}


# ---------------------------------------------------------------- helpers

def _read(path, default=None, cast=None):
    try:
        with open(path) as f:
            value = f.read().strip()
        return cast(value) if cast else value
    except (OSError, ValueError):
        return default


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def _run(cmd, timeout=10):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def machine_identity():
    """Who made this board, so the app never has to guess again.

    Tongfang and Clevo put the barebone code at the front of the DMI system
    serial number: GM5MG0Y2210300570 is a Tongfang GM5MG0Y. Only the code is
    returned, never the rest of the serial.
    """
    base = os.environ.get("MONSTER_CENTER_DMI", "/sys/class/dmi/id")
    def field(name):
        return _read(os.path.join(base, name), "")

    serial = field("product_serial")
    code = "".join(ch for ch in serial[:7] if ch.isalnum())
    return {
        "model": " ".join(p for p in (field("sys_vendor"), field("product_name")) if p),
        "barebone": code,
        "bios": " ".join(p for p in (field("bios_vendor"), field("bios_version")) if p),
        "ec_firmware": field("ec_firmware_release"),
    }


def _hwmon_path(name, index=0):
    """Find a hwmon device by exact name, else by name prefix (pch_cometlake_2)."""
    exact, prefixed = [], []
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        hw_name = _read(os.path.join(hw, "name")) or ""
        if hw_name == name:
            exact.append(hw)
        elif hw_name.startswith(name):
            prefixed.append(hw)
    found = exact + prefixed
    if len(found) > index:
        return found[index]
    return None


def _hwmon_temps(name, index=0):
    """Return {label: celsius} for a hwmon device."""
    hw = _hwmon_path(name, index)
    if not hw:
        return {}
    out = {}
    for inp in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
        base = os.path.basename(inp).replace("_input", "")
        raw = _read(inp, cast=int)
        if raw is None:
            continue
        label = _read(os.path.join(hw, f"{base}_label")) or base
        out[label] = round(raw / 1000, 1)
    return out


# ------------------------------------------------------------------- EC

class Ec:
    """Read-only access to the embedded controller via ec_sys."""

    def __init__(self):
        self.available = os.path.exists(EC_IO)

    def _bytes(self):
        try:
            with open(EC_IO, "rb") as f:
                return f.read()
        except OSError:
            self.available = False
            return None

    @staticmethod
    def _fan_level(data, offset):
        raw = ((data[offset] & 0x0F) << 8) | data[offset + 1]
        return raw

    def status(self):
        data = self._bytes()
        if not data or len(data) < 0x70:
            return {"available": False}
        fan1 = self._fan_level(data, EC_FAN1)
        fan2 = self._fan_level(data, EC_FAN2)
        return {
            "available": True,
            "board_temp": data[EC_BOARD_TEMP],
            "gpu_temp": data[EC_GPU_TEMP],
            "fan1_raw": fan1,
            "fan2_raw": fan2,
            "fan1_pct": round(fan1 * 100 / FAN_LEVEL_MAX),
            "fan2_pct": round(fan2 * 100 / FAN_LEVEL_MAX),
        }


# ------------------------------------------------------------------ CPU

def cpu_info():
    freqs = []
    for policy in sorted(glob.glob(f"{CPUFREQ}/policy*")):
        cur = _read(os.path.join(policy, "scaling_cur_freq"), cast=int)
        if cur:
            freqs.append(cur)
    gov = _read(f"{CPUFREQ}/policy0/scaling_governor", "unknown")
    epp = _read(f"{CPUFREQ}/policy0/energy_performance_preference", "unknown")
    return {
        "governor": gov,
        "epp": epp,
        "turbo": _read(f"{INTEL_PSTATE}/no_turbo", cast=int) == 0,
        "min_freq_mhz": (_read(f"{CPUFREQ}/policy0/scaling_min_freq", cast=int) or 0) // 1000,
        "max_freq_mhz": (_read(f"{CPUFREQ}/policy0/scaling_max_freq", cast=int) or 0) // 1000,
        "cur_freq_mhz": round(sum(freqs) / len(freqs) / 1000) if freqs else 0,
        "min_perf_pct": _read(f"{INTEL_PSTATE}/min_perf_pct", cast=int),
        "max_perf_pct": _read(f"{INTEL_PSTATE}/max_perf_pct", cast=int),
        "num_pstates": _read(f"{INTEL_PSTATE}/num_pstates", cast=int),
        "governors": sorted(os.listdir(f"{CPUFREQ}/policy0")) and _read(
            f"{CPUFREQ}/policy0/scaling_available_governors", "").split(),
        "epps": _read(
            f"{CPUFREQ}/policy0/energy_performance_available_preferences", "").split(),
    }


def set_governor(name):
    if not os.path.isdir(f"{CPUFREQ}/policy0"):
        return False, "cpufreq not available"
    for policy in glob.glob(f"{CPUFREQ}/policy*"):
        try:
            _write(os.path.join(policy, "scaling_governor"), name)
        except OSError as exc:
            return False, str(exc)
    return True, f"governor set to {name}"


def set_epp(value):
    """Set the HWP energy preference on every policy.

    A policy running the intel_pstate 'performance' governor refuses EPP
    writes with EBUSY, because that governor pins the preference. Fall back
    to the 'powersave' governor, which is where EPP actually does its work,
    so switching to a lower preference cannot fail for that reason.
    """
    governor_reset = False
    try:
        for policy in glob.glob(f"{CPUFREQ}/policy*"):
            try:
                _write(os.path.join(policy, "energy_performance_preference"), value)
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise
                _write(os.path.join(policy, "scaling_governor"), "powersave")
                _write(os.path.join(policy, "energy_performance_preference"), value)
                governor_reset = True
    except OSError as exc:
        return False, str(exc)
    message = f"energy preference set to {value}"
    if governor_reset:
        message += " (governor reset to powersave, which is what makes EPP stick)"
    return True, message


def set_turbo(enabled):
    try:
        _write(f"{INTEL_PSTATE}/no_turbo", 0 if enabled else 1)
        return True, f"turbo boost {'on' if enabled else 'off'}"
    except OSError as exc:
        return False, str(exc)


def set_max_freq_mhz(mhz):
    try:
        for policy in glob.glob(f"{CPUFREQ}/policy*"):
            _write(os.path.join(policy, "scaling_max_freq"), int(mhz) * 1000)
        return True, f"max frequency set to {mhz} MHz"
    except OSError as exc:
        return False, str(exc)


def cpu_max_mhz():
    """Hardware ceiling, in MHz, from the first policy."""
    value = _read(f"{CPUFREQ}/policy0/cpuinfo_max_freq", cast=int) or 0
    return value // 1000


# --------------------------------------------------------------- power

def memory_info():
    """System RAM and swap in MiB, from /proc/meminfo."""
    wanted = ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree")
    values = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in wanted:
                    values[key] = int(rest.split()[0]) // 1024
    except (OSError, ValueError):
        return {"available": False}
    total = values.get("MemTotal", 0)
    if not total:
        return {"available": False}
    available = values.get("MemAvailable", values.get("MemFree", 0))
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "available": True,
        "total_mb": total,
        "used_mb": max(total - available, 0),
        "available_mb": available,
        "swap_total_mb": swap_total,
        "swap_used_mb": max(swap_total - swap_free, 0),
    }


def power_limits():
    return {
        "pl1_w": (_read(f"{RAPL}/constraint_0_power_limit_uw", cast=int) or 0) / 1e6,
        "pl2_w": (_read(f"{RAPL}/constraint_1_power_limit_uw", cast=int) or 0) / 1e6,
        "pl1_max_w": (_read(f"{RAPL}/constraint_0_max_power_uw", cast=int) or 0) / 1e6,
        "name": _read(f"{RAPL}/name", "package-0"),
        "writable": os.access(f"{RAPL}/constraint_0_power_limit_uw", os.W_OK),
    }


def set_power_limits(pl1_w, pl2_w=None):
    result = []
    try:
        _write(f"{RAPL}/constraint_0_power_limit_uw", int(pl1_w * 1e6))
        result.append(f"PL1 {int(pl1_w)}W")
    except OSError as exc:
        return False, f"PL1 write failed: {exc}"
    if pl2_w:
        try:
            _write(f"{RAPL}/constraint_1_power_limit_uw", int(pl2_w * 1e6))
            result.append(f"PL2 {int(pl2_w)}W")
        except OSError as exc:
            result.append(f"PL2 write failed: {exc}")
    return True, ", ".join(result) + " applied"


class PowerMeter:
    """CPU package power from the RAPL energy counter."""

    def __init__(self):
        self._last = None
        self._last_t = None

    def watts(self):
        energy = _read(f"{RAPL}/energy_uj", cast=int)
        now = time.monotonic()
        if energy is None:
            return None
        if self._last is None or self._last_t is None:
            self._last, self._last_t = energy, now
            return None
        delta = energy - self._last
        elapsed = now - self._last_t
        self._last, self._last_t = energy, now
        if delta < 0 or elapsed <= 0:
            return None
        return round(delta / elapsed / 1e6, 1)


# ------------------------------------------------------------------ GPU

GPU_QUERY_FIELDS = (
    "name,temperature.gpu,utilization.gpu,utilization.memory,memory.used,"
    "memory.total,clocks.current.graphics,clocks.current.memory,power.draw,"
    "power.limit,power.default_limit,power.max_limit"
)


def gpu_info():
    code, out, err = _run(["nvidia-smi", f"--query-gpu={GPU_QUERY_FIELDS}",
                           "--format=csv,noheader,nounits"])
    if code != 0 or not out:
        return {"available": False, "error": err or "nvidia-smi not available"}
    parts = [p.strip() for p in out.splitlines()[0].split(",")]

    def num(text, cast=float):
        try:
            return cast(text)
        except (TypeError, ValueError):
            return None

    return {
        "available": True,
        "name": parts[0],
        "temp": num(parts[1], int),
        "util": num(parts[2], int),
        "mem_util": num(parts[3], int),
        "mem_used": num(parts[4], int),
        "mem_total": num(parts[5], int),
        "clock_graphics": num(parts[6], int),
        "clock_memory": num(parts[7], int),
        "power_draw": num(parts[8]),
        "power_limit": num(parts[9]),
        "power_default": num(parts[10]),
        "power_max": num(parts[11]),
    }


def set_gpu_power_limit(watts):
    code, _, err = _run(["nvidia-smi", "-pl", str(int(watts))])
    if code != 0:
        return False, err or "nvidia-smi -pl failed"
    info = gpu_info()
    applied = info.get("power_limit")
    if applied:
        return True, f"GPU power limit {applied:.0f} W"
    return False, "the driver reports no settable GPU power limit on this device"


# ------------------------------------------------------- graphics mode

def graphics_mode():
    code, out, _ = _run(["system76-power", "graphics"])
    if code != 0:
        return None
    return out or None


def set_graphics_mode(mode):
    code, out, err = _run(["system76-power", "graphics", mode], timeout=30)
    if code != 0:
        return False, err or out
    return True, f"graphics mode set to {mode} (takes effect after a reboot)"


# ------------------------------------------------------------- battery

def battery_info():
    base = "/sys/class/power_supply/BAT0"
    if not os.path.isdir(base):
        return {"present": False}
    charge_now = _read(f"{base}/charge_now", cast=int)
    charge_full = _read(f"{base}/charge_full", cast=int)
    energy_now = _read(f"{base}/energy_now", cast=int)
    energy_full = _read(f"{base}/energy_full", cast=int)
    power_now = _read(f"{base}/power_now", cast=int)
    if power_now is None:
        power_now = _read(f"{base}/current_now", cast=int)
    return {
        "present": True,
        "capacity": _read(f"{base}/capacity", cast=int),
        "status": _read(f"{base}/status", ""),
        "ac_online": _read("/sys/class/power_supply/AC0/online", cast=int) == 1,
        "charge_now": charge_now,
        "charge_full": charge_full,
        "energy_now": energy_now,
        "energy_full": energy_full,
        "power_now": power_now,
    }


# ------------------------------------------------------------ keyboard

# This keyboard is the ITE 8291 zone firmware: USB 048d:ce00, bcdDevice 0.02.
# It speaks the tuxedo-drivers ite8291_zones protocol (four zones), not the
# per-key 8291r3 protocol that the 048d:6005 controller uses. led.py holds the
# report sequence; the daemon runs as root and does the hidraw writes.
KEYBOARD_RGB = True
KEYBOARD_NOTE = ("Four addressable zones, not per-key lighting. Colour and brightness "
                 "work; per-key effects do not exist on this firmware.")


def keyboard_status():
    return {
        "available": led.available(),
        "zones": led.NUM_ZONES,
        "max_brightness": led.BRIGHTNESS_MAX,
        "device": led.find_device(),
    }


def led_set_color(rgb, brightness=30):
    return led.set_monocolor(rgb, brightness)


def led_set_zones(colors, brightness=30):
    return led.set_zones(colors, brightness)


def led_brightness(level):
    return led.set_brightness(level)


def led_off():
    return led.turn_off()


def led_effect(name, speed=5, brightness=30, color=None, direction=None):
    return False, led.UNSUPPORTED


# ------------------------------------------------------------ telemetry

def sensors_all():
    temps = {
        "cpu_cores": _hwmon_temps("coretemp"),
        "pch": _hwmon_temps("pch_cometlake", 0) or _hwmon_temps("pch_cannonlake", 0),
        "nvme": _hwmon_temps("nvme", 0),
        "nvme2": _hwmon_temps("nvme", 1),
        "wifi": _hwmon_temps("iwlwifi_1", 0) or _hwmon_temps("iwlwifi", 0),
    }
    return temps


def telemetry(meter=None):
    ec = Ec()
    data = {
        "time": time.time(),
        "ec": ec.status(),
        "cpu": cpu_info(),
        "gpu": gpu_info(),
        "power": power_limits(),
        "battery": battery_info(),
        "ram": memory_info(),
        "temps": sensors_all(),
        "graphics_mode": graphics_mode(),
        "keyboard": keyboard_status(),
    }
    if meter is not None:
        data["power"]["package_w"] = meter.watts()
    return data


def gpu_power_supported():
    """True when the driver reports a settable GPU power limit."""
    info = gpu_info()
    return bool(info.get("available") and info.get("power_limit"))


def power_state():
    """The live CPU mode, read back from sysfs (never from what we wrote)."""
    limits = power_limits()
    return {
        "pl1_w": round(limits["pl1_w"]),
        "pl2_w": round(limits["pl2_w"]),
        "governor": _read(f"{CPUFREQ}/policy0/scaling_governor", "unknown"),
        "epp": _read(f"{CPUFREQ}/policy0/energy_performance_preference", "unknown"),
        "turbo": _read(f"{INTEL_PSTATE}/no_turbo", cast=int) == 0,
        "max_freq_mhz": (_read(f"{CPUFREQ}/policy0/scaling_max_freq", cast=int) or 0) // 1000,
    }


def describe_state(values):
    """One line for a mode readout, used by the daemon, the CLI and the GUI."""
    return (f"PL1 {values.get('pl1_w', 0)} W, PL2 {values.get('pl2_w', 0)} W, "
            f"energy preference {values.get('epp', 'unknown')}, "
            f"turbo {'on' if values.get('turbo') else 'off'}")


def _state_value(values, key):
    value = values.get(key)
    return round(value) if key.endswith("_w") else value


def profile_apply(name):
    """Apply a profile and verify it by reading the values back.

    Returns (ok, messages, applied). ``applied`` is the state that is really
    in place afterwards, so the UI can show the mode instead of trusting the
    write: on this board the firmware accepts some writes and keeps its own
    value, and that must look like a failure, not like success.
    """
    profile = PROFILES.get(name)
    if not profile:
        return False, [f"unknown profile: {name}"], {}
    messages = []
    ok = True
    done, msg = set_power_limits(profile["pl1"], profile["pl2"])
    ok &= done
    messages.append(msg)
    done, msg = set_epp(profile["epp"])
    ok &= done
    messages.append(msg)
    done, msg = set_turbo(profile["turbo"])
    ok &= done
    messages.append(msg)
    # The clock ceiling has to be reset explicitly: whatever the last profile
    # (or system76-power) capped it to would otherwise survive a switch.
    ceiling = profile.get("max_mhz") or cpu_max_mhz()
    if ceiling:
        done, msg = set_max_freq_mhz(ceiling)
        ok &= done
        messages.append(msg)
    if profile.get("gpu_w") and gpu_power_supported():
        done, msg = set_gpu_power_limit(profile["gpu_w"])
        messages.append(msg)

    applied = power_state()
    applied.update({"name": name, "label": profile["label"],
                    "description": profile["description"], "at": time.time()})
    wanted = {"pl1_w": profile["pl1"], "pl2_w": profile["pl2"],
              "epp": profile["epp"], "turbo": profile["turbo"],
              "max_freq_mhz": ceiling}
    for key, label in (("pl1_w", "PL1"), ("pl2_w", "PL2"),
                       ("epp", "energy preference"), ("turbo", "turbo"),
                       ("max_freq_mhz", "max frequency")):
        if _state_value(applied, key) == wanted[key]:
            continue
        note = (f"{label} stayed at {_state_value(applied, key)}"
                f" instead of {wanted[key]}")
        messages.append(note)
        # PL2 is advisory on this firmware; the other three define the mode.
        if key != "pl2_w":
            ok = False
    if ok:
        messages.append(f"verified: {describe_state(applied)}")
    return ok, messages, applied
