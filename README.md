# Monster Center

A Linux control centre for the **Monster TULPAR T5 V21.4**. This unit is a
Tongfang GM5MG0Y barebone (the system serial number starts with GM5MG0Y), with
an AMI Aptio BIOS (N.1.08MON06) and an ITE embedded controller behind the
memory window the DSDT declares as `ECMG` at 0xFE410000. It replaces the
Windows-only Monster Control Center with the parts that can actually be
controlled on this board, and it shows the readings the vendor tool shows.

Tested on this machine: Pop!_OS 24.04, kernel 7.1, i7-10875H, RTX 3060 Laptop
GPU, EC firmware 1.17.

## What it does

Monitoring (1-2 s refresh)

- CPU package and per-core temperatures, current and allowed frequency,
  governor, energy preference, turbo state, package power from RAPL
- RTX 3060 temperature, load, clocks, VRAM use and power draw
- Fan levels, board and GPU temperatures from the embedded controller
- Chipset, two NVMe drives, Wi-Fi temperatures
- Battery level, charge state, AC adapter

Controls (verified on the hardware)

- Five power profiles: Silent, Balanced, Performance, Gaming, Battery saver.
  Each one sets the CPU package power limits (RAPL PL1/PL2), the intel_pstate
  energy preference and turbo boost. After applying one, the daemon reads the
  values back from sysfs and reports what is really in place, so a write the
  firmware ignores shows up as a failure instead of a silent success
- The Performance page shows the mode that is in effect: the profile name, its
  live PL1/PL2, energy preference and turbo state, the time it was applied, and
  the active profile button is highlighted. A hand tuned limit or energy
  preference falls back to "Custom", and a mismatch between the profile and the
  live values is called out in the same card
- Manual CPU power limits, governor, energy preference and turbo boost
- Graphics mode switch through system76-power (hybrid, integrated, nvidia),
  needs a reboot
- Keyboard backlight: four addressable zones, colour and brightness (0 to 50),
  set for all zones at once or one colour per zone
- Automatic thermal guard: caps CPU power in 5 W steps when the package passes
  90 C and restores the profile once it drops to 82 C

Power-mode key

- The chassis key next to the power button switches the embedded controller's
  power mode. The firmware owns the state and its LED; this project reads that
  state from the EC, applies the matching profile (power limits, energy
  preference, turbo, clock ceiling, GPU limit) and shows a desktop notification
  naming the mode, so the LED, the popup and the CPU settings move together

Known limits on this model

The CPU mode is the intel_pstate energy preference, not the governor. The
governor stays on `powersave` because HWP does the scaling; the profile's energy
preference (`power`, `balance_power`, `balance_performance`, `performance`) sets
how hard the CPU pushes. The governor also has to stay there: while a policy runs
the intel_pstate `performance` governor the kernel rejects energy preference
writes with `EBUSY`, so a profile click would set its power limits and then fail
on the preference. `set_epp` falls back to `powersave` when it meets that state,
and the Performance page explains the split under Processor policy.

The fans stay under embedded controller control. The DSDT does declare manual
duty for both fans in the extended part of the EC window (0xE8C holds F1DC and
F1CM, 0xE9D holds F2DC and F2CM). Writing `0x80 | duty` there is accepted and
reads back, but in a 20 second test at 100 % the fan level registers did not
move and the fan speed fields at 0xE1C stayed at zero. The EC keeps its own
curve, which you can see in the live fan level, so the app offers no manual fan
slider. CPU power limits are the effective lever for cooling this machine.

The keyboard backlight works. This is the ITE 8291 zone firmware: USB
048d:ce00 with bcdDevice 0.02, which speaks the four-zone protocol tuxedo-drivers
ships as `ite8291_zones` and Rainexn0b/keyRGB calls `ite8291_zones_clevo`. The
per-key 8291r3 protocol that `ite8291r3-ctl` sends to 048d:6005 rev 0.03 is
accepted by this firmware and then ignored, which is why earlier attempts looked
dead. Colours, per-zone colours and brightness (0 to 50) are verified on the
hardware; there is no per-key or effect mode on this firmware.

The NVIDIA driver on this unit does not report a settable GPU power limit
(`nvidia-smi -pl` returns no value), so that control appears disabled.

## Install

Requirements: a Debian based distro with systemd (tested on Pop!_OS 24.04),
Python 3 with GTK 3 (`python3-gi`, `gir1.2-gtk-3.0`), and the hardware listed
above. Root is needed for the install only.

```bash
git clone https://github.com/justbasicfacts/monster-center.git
cd monster-center
sudo ./install.sh
```

The installer copies the app to `/opt/monster-center`, installs the root
services (`monster-center` and `monster-center-powermode`), the user-session
popup service for the account that ran the installer, a launcher in
`/usr/local/bin`, a desktop entry, and sets `ec_sys` to read-only mode for the
fan and temperature reads.

Start the GUI from the application grid ("Monster Center") or with
`monster-center gui`.

Check the power-mode bridge:

```bash
systemctl status monster-center-powermode          # root side, reads the EC
systemctl --user status monster-center-powermode-popup
python3 -m monster_center.powermode status         # needs PYTHONPATH=/opt/monster-center
```

## Power-mode key

The key next to the power button is not a keyboard key: the embedded controller
handles it, changes its own fan curve and lights the small mode LED. The mode
lives in extended EC RAM at 0x0751, which the DSDT exposes through the
`0xFE410000` window, so it can be read without any vendor driver:

| `0x0751 & 0xB0` | Mode | CPU profile applied |
| --- | --- | --- |
| 0xA0 | Silent | PL1 20 W / PL2 25 W, energy preference `power`, turbo off, 2.3 GHz ceiling |
| 0x00 | Balanced | PL1 35 W / PL2 45 W, `balance_power`, turbo on, full clock ceiling |
| 0x80 | Auto (LED off) | same as Balanced |
| 0x10 | Performance | PL1 55 W / PL2 70 W, `performance`, turbo on, full clock ceiling |

`monster-center-powermode` reads the byte every 250 ms, waits until a value has
held for a second (the EC passes through 0x80 for a moment while switching) and
publishes the mode to `/run/monster-center/powermode`. The user service
`monster-center-powermode-popup` follows that file, applies the profile through
the daemon and calls `notify-send`.

Two gotchas on this board, both handled by the reader:

- The mode byte is in the extended area; the first 256 bytes of the window read
  as zero and the ordinary `ec0/io` debugfs node cannot see it.
- TUXEDO Control Center's `tuxedo_keyboard` driver writes 0x0751 to balanced at
  load and swallows the key as a Meta+Alt+F6 shortcut, which pins the LED in the
  middle and stops the firmware from switching modes. If you have it installed,
  keep that module out of the way:

```bash
printf 'blacklist tuxedo_keyboard\ninstall tuxedo_keyboard /bin/false\n' \
  | sudo tee /etc/modprobe.d/blacklist-tuxedo-keyboard.conf
sudo modprobe -r tuxedo_keyboard tuxedo_io
```

Keyboard RGB is unaffected: it comes from the separate `ite_8291` module.

## Use from the terminal

```bash
monster-center status                 # live summary
monster-center profile list           # available profiles
monster-center profile silent         # power limits, EPP, turbo
monster-center cpu --epp power --turbo off
monster-center power --pl1 30 --pl2 40
monster-center gpu --power-limit 90   # if the driver allows it
monster-center graphics hybrid        # reboot required
monster-center led --rgb 60,110,255 --brightness 35    # all four zones
monster-center led --zones "255,45,45;40,220,90;60,110,255;255,90,190"
monster-center led --brightness 15    # brightness only, keeps the colours
monster-center led --off
```

Root is never needed for the CLI or the GUI: both talk to the daemon over
`/run/monster-center/sock`, which is owned by `root` and your primary group.

## How it is built

```
monster_center/hardware.py   sysfs, EC, RAPL, nvidia-smi, system76-power
monster_center/daemon.py     root service, unix socket, profiles, thermal guard
monster_center/gui.py        GTK 3 front end
monster_center/cli.py        command line front end
monster_center/led.py        ITE 8291 four zone protocol for the keyboard backlight
monster_center/powermode.py  EC power-mode key bridge (watch as root, popup in the session)
```

The daemon runs as `root` under systemd, so the GUI stays unprivileged. The
socket carries line based JSON, one request and one reply per connection.

## Embedded controller notes for this board

Mapped from the DSDT (`OperationRegion ECMG, SystemMemory, 0xFE410000,
0x00010000`) and confirmed by live probing of `/sys/kernel/debug/ec/ec0/io`,
which mirrors the first 256 bytes of that window. The DSDT field names are in
brackets.

| Offset | Meaning |
| --- | --- |
| 0x36 | CPU package temperature, `CPTM` at window 0x43E |
| 0x3E | board / chipset temperature |
| 0x4F | GPU temperature, `VGAT` at window 0x44F |
| 0x60 | fan 1 level, 12 bit, plus `FFAN` in the upper nibble |
| 0x68 | fan 2 level, 12 bit, plus `SDAN` in the upper nibble |
| 0x751 | firmware power mode, bits 0xA0 / 0x10 / 0x80 (see Power-mode key) |

The upper nibble of the fan bytes carries a mode value, the lower 12 bits are
the level (0 to 4095). Reading them needs `ec_sys`, which the installer keeps
in read-only mode.

Extended fields the DSDT declares, readable only through the EC window:

| Window offset | Field | Meaning |
| --- | --- | --- |
| 0x783 | APL1 / APL2 / APL4 | CPU power limits the BIOS sets through the vendor ACPI method |
| 0xE0D | CPUT / PCHT | CPU and chipset temperatures |
| 0xE1C | F1SH / F1SL | fan 1 speed, 16 bit |
| 0xE8C | F1DC (7 bit) + F1CM (bit 7) | fan 1 duty and manual enable |
| 0xE9D | F2DC (7 bit) + F2CM (bit 7) | fan 2 duty and manual enable |
| 0xEA2 onward | UVER, CCI, CTL, MGI, MGO | vendor command mailbox and firmware version |

The vendor control path runs through the ACPI method `T1WR` in `_SB.INOU`,
which the Windows tool uses for power limits, brightness and the mailbox. An
earlier build of this app assumed a Clevo barebone and tried Clevo's fan
command 0x99 and its LED mailbox at 0xF8 to 0xFD, neither of which exists on a
Tongfang board, so those attempts could not have worked.

## Uninstall

```bash
sudo ./uninstall.sh
```
