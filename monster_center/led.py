"""Keyboard backlight control for the ITE 8291 zone firmware on this board.

The keyboard on this Tongfang GM5MG0Y chassis is USB 048d:ce00 with
bcdDevice 0x0002. That firmware split matters: the per-key 8291r3 protocol
(the one ite8291r3-ctl speaks to 048d:6005 rev 0.03) is accepted and then
ignored here. What works is the four-zone protocol that tuxedo-drivers ships
as ite8291_zones and that Rainexn0b/keyRGB carries as ite8291_zones_clevo.

Protocol, eight byte HID feature reports on the keyboard's hidraw node:

    zone enable   1a 00 01 04 00 00 00 01
    zone colour   14 00 <zone+1> R G B 00 00        (zones 0..3)
    commit        08 02 01 03 <brightness> 08 00 00 (brightness 0..50)
    off           09 02 00 00 00 00 00 00
                  12 00 03 00 00 00 00 00
                  08 05 00 00 00 00 00 00
                  08 01 00 00 00 00 00 00
                  1a 00 00 00 00 00 00 01

The daemon runs as root, so the GUI and CLI never touch /dev/hidraw directly.
"""

from __future__ import annotations

import fcntl
import glob
import os

VENDOR_ID = 0x048D
PRODUCT_ID = 0xCE00
REQUIRED_BCD = 0x0002
NUM_ZONES = 4
BRIGHTNESS_MAX = 50
MAX_BRIGHTNESS = BRIGHTNESS_MAX

ZONE_ENABLE = bytes((0x1A, 0x00, 0x01, 0x04, 0x00, 0x00, 0x00, 0x01))
ZONE_DISABLE = bytes((0x1A, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01))
TURN_OFF = (
    bytes((0x09, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)),
    bytes((0x12, 0x00, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00)),
    bytes((0x08, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)),
    bytes((0x08, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)),
    ZONE_DISABLE,
)

NOTE = ("Four addressable zones, not per-key lighting. The keyboard answers on "
        "048d:ce00 with the ITE 8291 zones protocol.")
UNSUPPORTED = ("This keyboard has four zones, so per-key and effect modes are not "
               "available. Use the colour and brightness controls instead.")

_IOC_WRITE = 1
_IOC_READ = 2
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = 8
_IOC_SIZESHIFT = 16
_IOC_DIRSHIFT = 30


def _ioc(dir_bits, type_bits, nr, size):
    return ((dir_bits << _IOC_DIRSHIFT) | (type_bits << _IOC_TYPESHIFT)
            | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _hidiocsfeature(length):
    return _ioc(_IOC_WRITE | _IOC_READ, ord("H"), 0x06, int(length))


def _read_hex(path):
    try:
        with open(path) as f:
            return int(f.read().strip(), 16)
    except (OSError, ValueError):
        return None


def _node_info(node):
    """(vendor, product, bcdDevice) for a hidraw node, or None."""
    base = os.path.join("/sys/class/hidraw", os.path.basename(node), "device")
    try:
        with open(os.path.join(base, "uevent")) as f:
            uevent = dict(
                line.split("=", 1) for line in f.read().splitlines() if "=" in line
            )
    except OSError:
        return None
    parts = uevent.get("HID_ID", "").split(":")
    if len(parts) != 3:
        return None
    try:
        vendor, product = int(parts[1], 16), int(parts[2], 16)
    except ValueError:
        return None
    bcd = None
    path = os.path.realpath(base)
    for _ in range(4):
        bcd = _read_hex(os.path.join(path, "bcdDevice"))
        if bcd is not None:
            break
        path = os.path.dirname(path)
    return vendor, product, bcd


def find_device():
    """Path of the keyboard's hidraw node, or None."""
    forced = os.environ.get("MONSTER_CENTER_HIDRAW")
    if forced:
        return forced if os.path.exists(forced) else None
    fallback = None
    for node in sorted(glob.glob("/dev/hidraw*")):
        info = _node_info(node)
        if not info:
            continue
        vendor, product, bcd = info
        if vendor != VENDOR_ID or product != PRODUCT_ID:
            continue
        if bcd == REQUIRED_BCD:
            return node
        fallback = fallback or node
    return fallback


def available():
    return find_device() is not None


def _send(fd, report):
    buf = bytearray(report)
    fcntl.ioctl(fd, _hidiocsfeature(len(buf)), buf, True)


def _clamp_channel(value):
    return max(0, min(255, int(value)))


def _clamp_brightness(value):
    return max(0, min(BRIGHTNESS_MAX, int(value)))


def _zone_colour(zone, rgb):
    red, green, blue = (_clamp_channel(c) for c in rgb)
    return bytes((0x14, 0x00, int(zone) + 1, red, green, blue, 0x00, 0x00))


def _commit(brightness):
    return bytes((0x08, 0x02, 0x01, 0x03, _clamp_brightness(brightness), 0x08, 0x00, 0x00))


def _write(node, reports):
    fd = os.open(node, os.O_RDWR)
    try:
        for report in reports:
            _send(fd, report)
    finally:
        os.close(fd)


def apply(colors, brightness=30):
    """Colour every zone. colors is a list of up to four (r, g, b) tuples."""
    node = find_device()
    if node is None:
        return False, "keyboard controller not found (048d:ce00)"
    brightness = _clamp_brightness(brightness)
    if brightness == 0:
        return turn_off()
    zones = [tuple(_clamp_channel(c) for c in rgb) for rgb in list(colors)[:NUM_ZONES]]
    while len(zones) < NUM_ZONES:
        zones.append(zones[-1] if zones else (0, 0, 0))
    reports = [ZONE_ENABLE]
    reports += [_zone_colour(zone, rgb) for zone, rgb in enumerate(zones)]
    reports.append(_commit(brightness))
    try:
        _write(node, reports)
    except OSError as exc:
        return False, f"keyboard write failed: {exc}"
    return True, f"keyboard set at brightness {brightness}/{BRIGHTNESS_MAX}"


def set_monocolor(rgb, brightness=30):
    return apply([tuple(rgb)] * NUM_ZONES, brightness)


def set_zones(colors, brightness=30):
    return apply(colors, brightness)


def set_brightness(level):
    node = find_device()
    if node is None:
        return False, "keyboard controller not found (048d:ce00)"
    try:
        _write(node, [_commit(level)])
    except OSError as exc:
        return False, f"keyboard write failed: {exc}"
    return True, f"keyboard brightness {level}/{BRIGHTNESS_MAX}"


def turn_off():
    node = find_device()
    if node is None:
        return False, "keyboard controller not found (048d:ce00)"
    try:
        _write(node, TURN_OFF)
    except OSError as exc:
        return False, f"keyboard write failed: {exc}"
    return True, "keyboard backlight off"


def set_effect(name, speed=5, brightness=30, color=None, direction="right"):
    return False, UNSUPPORTED
