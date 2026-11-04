#!/usr/bin/env python3
"""GTK front end for Monster Center."""

from __future__ import annotations

import os
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import client, hardware  # noqa: E402

MODEL = "Monster TULPAR T5 V21.4"

CSS = """
.mc-card { border-radius: 10px; padding: 14px; }
.mc-big { font-size: 30px; font-weight: 300; }
.mc-caption { font-size: 11px; opacity: 0.65; }
.mc-value { font-size: 15px; }
.mc-warn { color: #e5a50a; }
.mc-ok { color: #2ec27e; }
.mc-profile-btn { padding: 9px 6px; }
.mc-profile-btn.mc-profile-active { background-image: none;
  background-color: #3584e4; color: #ffffff; font-weight: 700; }
.mc-profile-btn.mc-profile-active:hover { background-color: #1c71d8; }
progressbar.mc-fan trough { min-height: 10px; }
"""


def card(title):
    frame = Gtk.Frame()
    frame.get_style_context().add_class("mc-card")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_border_width(14)
    label = Gtk.Label(label=title)
    label.set_xalign(0)
    label.get_style_context().add_class("mc-caption")
    box.pack_start(label, False, False, 0)
    frame.add(box)
    return frame, box


def big_value(text="--"):
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.get_style_context().add_class("mc-big")
    return label


def row(key, value="--"):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    k = Gtk.Label(label=key)
    k.set_xalign(0)
    k.get_style_context().add_class("mc-caption")
    v = Gtk.Label(label=value)
    v.set_xalign(1)
    v.set_hexpand(True)
    v.get_style_context().add_class("mc-value")
    box.pack_start(k, False, False, 0)
    box.pack_end(v, False, False, 0)
    return box, v


class MonsterCenter(Gtk.Window):
    def __init__(self):
        super().__init__(title="Monster Center")
        self.set_default_size(1120, 640)
        self.set_icon_name("preferences-system")

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._busy = False
        self._last = None
        self._suppress = False
        self._fan_history = {"fan1": [], "fan2": []}

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Monster Center")
        header.set_subtitle(MODEL)
        self.header = header
        self.set_titlebar(header)

        self.profile_combo = Gtk.ComboBoxText()
        for key, profile in hardware.PROFILES.items():
            self.profile_combo.append(key, profile["label"])
        self.profile_combo.append("custom", "Custom")
        self.profile_combo.set_tooltip_text("Apply a power profile")
        self.profile_combo.connect("changed", self.on_profile_changed)
        header.pack_end(self.profile_combo)

        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic",
                                                            Gtk.IconSize.BUTTON)
        self.refresh_button.set_tooltip_text("Refresh now")
        self.refresh_button.connect("clicked", lambda *_: self.schedule_poll())
        header.pack_end(self.refresh_button)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(stack)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top.set_border_width(8)
        top.pack_start(switcher, True, False, 0)
        outer.pack_start(top, False, False, 0)
        outer.pack_start(stack, True, True, 0)

        self.banner = Gtk.InfoBar()
        self.banner.set_no_show_all(True)
        self.banner_label = Gtk.Label()
        self.banner_label.set_line_wrap(True)
        self.banner.get_content_area().add(self.banner_label)
        self.banner.set_show_close_button(False)
        outer.pack_start(self.banner, False, False, 0)

        self.status = Gtk.Label(label="")
        self.status.set_xalign(0)
        self.status.set_margin_start(12)
        self.status.set_margin_end(12)
        self.status.set_margin_top(4)
        self.status.set_margin_bottom(6)
        self.status.get_style_context().add_class("mc-caption")
        outer.pack_end(self.status, False, False, 0)
        outer.pack_end(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        stack.add_titled(self.scrolled(self.build_overview()), "overview", "Overview")
        stack.add_titled(self.scrolled(self.build_performance()), "performance", "Performance")
        stack.add_titled(self.scrolled(self.build_cooling()), "cooling", "Cooling")
        stack.add_titled(self.scrolled(self.build_keyboard()), "keyboard", "Keyboard")
        stack.add_titled(self.scrolled(self.build_diagnostics()), "about", "Diagnostics")
        start_page = os.environ.get("MONSTER_CENTER_PAGE")
        if start_page:
            GLib.idle_add(lambda: (stack.set_visible_child_name(start_page), False)[1])

        self.add(outer)
        self.connect("destroy", Gtk.main_quit)

        self.poll_thread = threading.Thread(target=self.poll_loop, daemon=True)
        self.poll_thread.start()
        GLib.timeout_add(250, self.apply_pending)

    # ------------------------------------------------------------ overview

    @staticmethod
    def scrolled(widget):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(widget)
        return scroller

    def build_overview(self):
        grid = Gtk.Grid(column_spacing=14, row_spacing=14)
        grid.set_border_width(16)
        grid.set_column_homogeneous(True)

        cpu_card, cpu_box = card("Processor")
        self.cpu_temp = big_value()
        cpu_box.pack_start(self.cpu_temp, False, False, 0)
        self.cpu_caption, self.cpu_caption_v = row("hottest core", "--")
        cpu_box.pack_start(self.cpu_caption, False, False, 0)
        self.cpu_freq, self.cpu_freq_v = row("frequency", "--")
        self.cpu_power, self.cpu_power_v = row("power", "--")
        self.cpu_mode, self.cpu_mode_v = row("mode", "--")
        for widget in (self.cpu_freq, self.cpu_power, self.cpu_mode):
            cpu_box.pack_start(widget, False, False, 0)
        grid.attach(cpu_card, 0, 0, 1, 1)

        gpu_card, gpu_box = card("Graphics")
        self.gpu_temp = big_value()
        gpu_box.pack_start(self.gpu_temp, False, False, 0)
        self.gpu_load, self.gpu_load_v = row("load", "--")
        self.gpu_clock, self.gpu_clock_v = row("core clock", "--")
        self.gpu_power, self.gpu_power_v = row("power", "--")
        self.gpu_mem, self.gpu_mem_v = row("vram", "--")
        for widget in (self.gpu_load, self.gpu_clock, self.gpu_power, self.gpu_mem):
            gpu_box.pack_start(widget, False, False, 0)
        grid.attach(gpu_card, 1, 0, 1, 1)

        fan_card, fan_box = card("Fans")
        self.fan1_caption = Gtk.Label(label="Fan 1")
        self.fan1_caption.set_xalign(0)
        self.fan1_caption.get_style_context().add_class("mc-caption")
        self.fan1_bar = Gtk.ProgressBar()
        self.fan1_bar.get_style_context().add_class("mc-fan")
        self.fan1_label = Gtk.Label(label="--")
        self.fan1_label.set_xalign(0)
        self.fan1_label.get_style_context().add_class("mc-value")
        self.fan2_caption = Gtk.Label(label="Fan 2")
        self.fan2_caption.set_xalign(0)
        self.fan2_caption.get_style_context().add_class("mc-caption")
        self.fan2_bar = Gtk.ProgressBar()
        self.fan2_bar.get_style_context().add_class("mc-fan")
        self.fan2_label = Gtk.Label(label="--")
        self.fan2_label.set_xalign(0)
        self.fan2_label.get_style_context().add_class("mc-value")
        fan_note = Gtk.Label(label="Fan curve is owned by the embedded controller.")
        fan_note.set_xalign(0)
        fan_note.set_line_wrap(True)
        fan_note.get_style_context().add_class("mc-caption")
        for widget in (self.fan1_caption, self.fan1_bar, self.fan1_label,
                       self.fan2_caption, self.fan2_bar, self.fan2_label, fan_note):
            fan_box.pack_start(widget, False, False, 0)
        grid.attach(fan_card, 2, 0, 1, 1)

        bat_card, bat_box = card("Power supply")
        self.bat_level = big_value()
        bat_box.pack_start(self.bat_level, False, False, 0)
        self.bat_status, self.bat_status_v = row("state", "--")
        self.bat_ac, self.bat_ac_v = row("adapter", "--")
        bat_box.pack_start(self.bat_status, False, False, 0)
        bat_box.pack_start(self.bat_ac, False, False, 0)
        grid.attach(bat_card, 0, 1, 1, 1)

        other_card, other_box = card("Other sensors")
        self.other_rows = {}
        for key in ("PCH", "NVMe 1", "NVMe 2", "Wi-Fi"):
            widget, value = row(key, "--")
            other_box.pack_start(widget, False, False, 0)
            self.other_rows[key] = value
        grid.attach(other_card, 1, 1, 1, 1)

        cores_card, cores_box = card("CPU cores")
        self.cores_grid = Gtk.Grid(column_spacing=18, row_spacing=4)
        cores_box.pack_start(self.cores_grid, False, False, 0)
        grid.attach(cores_card, 2, 1, 1, 1)

        mem_card, mem_box = card("Memory")
        self.ram_row, self.ram_v = row("system ram", "--")
        self.swap_row, self.swap_v = row("swap", "--")
        for widget in (self.ram_row, self.swap_row):
            mem_box.pack_start(widget, False, False, 0)
        grid.attach(mem_card, 0, 2, 3, 1)

        return grid

    # -------------------------------------------------------- performance

    def build_performance(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.set_border_width(16)

        mode_card, mode_box = card("Current mode")
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.mode_label = big_value("--")
        self.mode_detail = Gtk.Label(label="", xalign=0)
        self.mode_detail.set_line_wrap(True)
        self.mode_detail.get_style_context().add_class("mc-value")
        left.pack_start(self.mode_label, False, False, 0)
        left.pack_start(self.mode_detail, False, False, 0)
        self.mode_badge = Gtk.Label(label="", xalign=1)
        self.mode_badge.set_line_wrap(True)
        self.mode_badge.get_style_context().add_class("mc-caption")
        mode_row.pack_start(left, True, True, 0)
        mode_row.pack_end(self.mode_badge, False, False, 0)
        mode_box.pack_start(mode_row, False, False, 0)
        outer.pack_start(mode_card, False, False, 0)

        prof_card, prof_box = card("Power profiles")
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.profile_buttons = {}
        for key, profile in hardware.PROFILES.items():
            button = Gtk.Button(label=profile["label"])
            button.get_style_context().add_class("mc-profile-btn")
            button.set_tooltip_text(profile["description"])
            button.connect("clicked", lambda _b, k=key: self.apply_profile(k))
            row_box.pack_start(button, True, True, 0)
            self.profile_buttons[key] = button
        prof_box.pack_start(row_box, False, False, 0)
        outer.pack_start(prof_card, False, False, 0)

        cpu_card, cpu_box = card("Processor policy")
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.attach(Gtk.Label(label="Governor", xalign=0), 0, 0, 1, 1)
        self.governor_combo = Gtk.ComboBoxText()
        for name in ("powersave", "performance"):
            self.governor_combo.append_text(name)
        self.governor_combo.connect("changed", self.on_governor_changed)
        grid.attach(self.governor_combo, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Energy preference", xalign=0), 0, 1, 1, 1)
        self.epp_combo = Gtk.ComboBoxText()
        for name in ("power", "balance_power", "balance_performance", "performance"):
            self.epp_combo.append_text(name)
        self.epp_combo.connect("changed", self.on_epp_changed)
        grid.attach(self.epp_combo, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Turbo boost", xalign=0), 0, 2, 1, 1)
        self.turbo_switch = Gtk.Switch()
        self.turbo_switch.connect("notify::active", self.on_turbo_changed)
        grid.attach(self.turbo_switch, 1, 2, 1, 1)
        cpu_box.pack_start(grid, False, False, 0)
        policy_note = Gtk.Label(
            label="intel_pstate keeps the governor on powersave; the energy preference "
                  "is what decides how hard the CPU pushes and it is what the profiles set.")
        policy_note.set_xalign(0)
        policy_note.set_line_wrap(True)
        policy_note.get_style_context().add_class("mc-caption")
        cpu_box.pack_start(policy_note, False, False, 0)
        outer.pack_start(cpu_card, False, False, 0)

        power_card, power_box = card("Power budget")
        pgrid = Gtk.Grid(column_spacing=12, row_spacing=10)
        pgrid.attach(Gtk.Label(label="Long term limit (PL1)", xalign=0), 0, 0, 1, 1)
        self.pl1_spin = Gtk.SpinButton.new_with_range(10, 120, 1)
        pgrid.attach(self.pl1_spin, 1, 0, 1, 1)
        pgrid.attach(Gtk.Label(label="Short term limit (PL2)", xalign=0), 0, 1, 1, 1)
        self.pl2_spin = Gtk.SpinButton.new_with_range(10, 150, 1)
        pgrid.attach(self.pl2_spin, 1, 1, 1, 1)
        apply_power = Gtk.Button(label="Apply power limits")
        apply_power.connect("clicked", self.on_apply_power)
        pgrid.attach(apply_power, 0, 2, 2, 1)
        power_box.pack_start(pgrid, False, False, 0)
        self.power_hint = Gtk.Label(
            label="Lower limits mean a cooler, quieter machine at the cost of clock speed.")
        self.power_hint.set_xalign(0)
        self.power_hint.set_line_wrap(True)
        self.power_hint.get_style_context().add_class("mc-caption")
        power_box.pack_start(self.power_hint, False, False, 0)
        outer.pack_start(power_card, False, False, 0)

        gpu_card, gpu_box = card("Graphics card")
        ggrid = Gtk.Grid(column_spacing=12, row_spacing=10)
        ggrid.attach(Gtk.Label(label="GPU power limit (W)", xalign=0), 0, 0, 1, 1)
        self.gpu_spin = Gtk.SpinButton.new_with_range(40, 130, 5)
        ggrid.attach(self.gpu_spin, 1, 0, 1, 1)
        self.apply_gpu_button = Gtk.Button(label="Apply GPU limit")
        self.apply_gpu_button.connect("clicked", self.on_apply_gpu)
        ggrid.attach(self.apply_gpu_button, 0, 1, 2, 1)
        ggrid.attach(Gtk.Label(label="Graphics mode", xalign=0), 0, 2, 1, 1)
        self.graphics_combo = Gtk.ComboBoxText()
        for mode in ("nvidia", "hybrid", "integrated"):
            self.graphics_combo.append_text(mode)
        ggrid.attach(self.graphics_combo, 1, 2, 1, 1)
        apply_graphics = Gtk.Button(label="Apply graphics mode (reboot required)")
        apply_graphics.connect("clicked", self.on_apply_graphics)
        ggrid.attach(apply_graphics, 0, 3, 2, 1)
        gpu_box.pack_start(ggrid, False, False, 0)
        self.gpu_hint = Gtk.Label(label="")
        self.gpu_hint.set_xalign(0)
        self.gpu_hint.set_line_wrap(True)
        self.gpu_hint.get_style_context().add_class("mc-caption")
        gpu_box.pack_start(self.gpu_hint, False, False, 0)
        outer.pack_start(gpu_card, False, False, 0)

        return outer

    # ------------------------------------------------------------ cooling

    def build_cooling(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.set_border_width(16)

        card1, box1 = card("Thermal load")
        self.cool_cpu, self.cool_cpu_v = row("CPU package", "--")
        self.cool_gpu, self.cool_gpu_v = row("GPU core", "--")
        self.cool_board, self.cool_board_v = row("Board (EC)", "--")
        self.cool_pch, self.cool_pch_v = row("Chipset", "--")
        for widget in (self.cool_cpu, self.cool_gpu, self.cool_board, self.cool_pch):
            box1.pack_start(widget, False, False, 0)
        outer.pack_start(card1, False, False, 0)

        card2, box2 = card("Fan levels")
        self.cool_fan1, self.cool_fan1_v = row("Fan 1", "--")
        self.cool_fan2, self.cool_fan2_v = row("Fan 2", "--")
        box2.pack_start(self.cool_fan1, False, False, 0)
        box2.pack_start(self.cool_fan2, False, False, 0)
        outer.pack_start(card2, False, False, 0)

        card3, box3 = card("Automatic thermal guard")
        guard_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(label="Cap CPU power when the package passes 90 C")
        label.set_xalign(0)
        label.set_halign(Gtk.Align.START)
        self.guard_switch = Gtk.Switch()
        self.guard_switch.connect("notify::active", self.on_guard_changed)
        guard_box.pack_start(label, False, False, 0)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        guard_box.pack_start(spacer, True, True, 0)
        guard_box.pack_end(self.guard_switch, False, False, 0)
        box3.pack_start(guard_box, False, False, 0)
        note = Gtk.Label()
        note.set_markup(
            "On this board the embedded controller keeps the fans on its own curve. "
            "Tongfang exposes manual fan duty at EC offsets 0xE8C and 0xE9D, and writing "
            "100 % there is accepted but changes nothing, so fan speed is read only here. "
            "Power limits are the effective way to keep this machine cool.")
        note.set_xalign(0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("mc-caption")
        box3.pack_start(note, False, False, 0)
        outer.pack_start(card3, False, False, 0)

        return outer

    # ----------------------------------------------------------- keyboard

    def build_keyboard(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.set_border_width(16)

        notice, notice_box = card("Keyboard backlight")
        self.keyboard_label = Gtk.Label()
        self.keyboard_label.set_xalign(0)
        self.keyboard_label.set_line_wrap(True)
        self.keyboard_label.set_markup(
            "<b>Four zones, not per-key.</b>\n\n"
            + hardware.KEYBOARD_NOTE.replace("&", "&amp;"))
        notice_box.pack_start(self.keyboard_label, False, False, 0)
        outer.pack_start(notice, False, False, 0)

        card1, box1 = card("All zones")
        colors = [
            ("Red", [255, 45, 45]), ("Orange", [255, 140, 20]),
            ("Yellow", [255, 220, 40]), ("Green", [40, 220, 90]),
            ("Cyan", [40, 210, 220]), ("Blue", [60, 110, 255]),
            ("Purple", [150, 80, 255]), ("Pink", [255, 90, 190]),
            ("White", [255, 245, 235]),
        ]
        color_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.colour_buttons = []
        for name, rgb in colors:
            button = Gtk.Button(label=name)
            button.connect("clicked", lambda _b, c=rgb: self.apply_led_color(c))
            color_box.pack_start(button, True, True, 0)
            self.colour_buttons.append(button)
        box1.pack_start(color_box, False, False, 0)

        bright_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bright_box.pack_start(Gtk.Label(label="Brightness", xalign=0), False, False, 0)
        self.brightness_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 50, 1)
        self.brightness_scale.set_value(30)
        self.brightness_scale.set_draw_value(True)
        bright_box.pack_start(self.brightness_scale, True, True, 0)
        apply_brightness = Gtk.Button(label="Apply brightness")
        apply_brightness.connect("clicked", self.on_apply_brightness)
        bright_box.pack_start(apply_brightness, False, False, 0)
        off_button = Gtk.Button(label="Backlight off")
        off_button.connect("clicked", lambda *_: self.apply_led_off())
        bright_box.pack_start(off_button, False, False, 0)
        box1.pack_start(bright_box, False, False, 0)
        outer.pack_start(card1, False, False, 0)

        card2, box2 = card("Per zone")
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.zone_buttons = []
        defaults = [(255, 45, 45), (40, 220, 90), (60, 110, 255), (255, 90, 190)]
        for index, rgb in enumerate(defaults):
            grid.attach(Gtk.Label(label=f"Zone {index + 1}", xalign=0), 0, index, 1, 1)
            button = Gtk.ColorButton()
            button.set_rgba(Gdk.RGBA(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1.0))
            grid.attach(button, 1, index, 1, 1)
            self.zone_buttons.append(button)
        apply_zones = Gtk.Button(label="Apply zone colours")
        apply_zones.connect("clicked", self.on_apply_zones)
        grid.attach(apply_zones, 0, len(defaults), 2, 1)
        box2.pack_start(grid, False, False, 0)
        note = Gtk.Label(label="Zone 1 sits on the left of the keyboard, zone 4 on the right.")
        note.set_xalign(0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("mc-caption")
        box2.pack_start(note, False, False, 0)
        outer.pack_start(card2, False, False, 0)

        return outer

    # -------------------------------------------------------- diagnostics

    def build_diagnostics(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_border_width(16)
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(Gtk.WrapMode.WORD)
        self.diag_buffer = view.get_buffer()
        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.add(view)
        outer.pack_start(scroller, True, True, 0)
        return outer

    # ------------------------------------------------------------ actions

    def show_status(self, text, warn=False):
        self.banner_label.set_text(text)
        self.banner.set_message_type(Gtk.MessageType.WARNING if warn
                                     else Gtk.MessageType.INFO)
        # The bar was built with no_show_all, so show_all() skips it entirely
        # and a plain show() leaves its label hidden: show the content here.
        self.banner.get_content_area().show_all()
        self.banner_label.show()
        self.banner.show()

    def hide_status(self):
        self.banner.hide()

    def call(self, op, **kwargs):
        try:
            reply = client.call(op, **kwargs)
        except client.DaemonError as exc:
            self.show_status(str(exc), warn=True)
            return None
        message = "; ".join(reply.get("messages", [])) or "done"
        self.show_status(message, warn=not reply.get("ok"))
        GLib.timeout_add_seconds(6, lambda: (self.hide_status(), False)[1])
        self.schedule_poll()
        return reply

    def _mark_profile(self, name):
        """Highlight the active profile button and keep the header in step."""
        for key, button in self.profile_buttons.items():
            active = key == name
            context = button.get_style_context()
            label = hardware.PROFILES[key]["label"]
            if active:
                context.add_class("mc-profile-active")
                label = f"{label}  \u2713"
            else:
                context.remove_class("mc-profile-active")
            if button.get_label() != label:
                button.set_label(label)
        combo_id = name or "custom"
        if self.profile_combo.get_active_id() != combo_id:
            self._suppress = True
            try:
                self.profile_combo.set_active_id(combo_id)
            finally:
                self._suppress = False
        self.header.set_subtitle(f"{MODEL} - {self.mode_label.get_text()}")

    def show_mode(self, live, state=None, applied=None):
        """Display the mode that is really in effect, read back from sysfs."""
        state = state or {}
        live = live or {}
        name = state.get("profile")
        profile = hardware.PROFILES.get(name) if name else None
        details = hardware.describe_state(live)
        if profile:
            self.mode_label.set_text(profile["label"])
            self.mode_detail.set_text(f"{profile['description']}. {details}")
        else:
            self.mode_label.set_text("Custom")
            self.mode_detail.set_text(f"No profile, set by hand. {details}")

        badge = ""
        if profile:
            wanted = {"pl1_w": profile["pl1"], "epp": profile["epp"],
                      "turbo": profile["turbo"]}
            drift = []
            for key, label in (("pl1_w", "PL1"), ("epp", "energy preference"),
                               ("turbo", "turbo")):
                have = round(live[key]) if key == "pl1_w" else live.get(key)
                if have != wanted[key]:
                    drift.append(f"{label} is {have}, profile wants {wanted[key]}")
            stamp = (applied or state.get("applied") or {}).get("at")
            if drift:
                badge = "; ".join(drift)
            elif stamp:
                badge = "applied " + time.strftime("%H:%M", time.localtime(stamp))
        guard = state.get("guard") or {}
        if guard.get("limited") and guard.get("pl1"):
            badge = f"thermal guard holding PL1 at {guard['pl1']:.0f} W"
        elif guard.get("enabled") is False:
            badge = (badge + " - guard off").strip(" -")
        if self.mode_badge.get_text() != badge:
            self.mode_badge.set_text(badge)
        context = self.mode_badge.get_style_context()
        if badge.startswith(("PL1 is", "energy preference is", "turbo is")):
            context.add_class("mc-warn")
        else:
            context.remove_class("mc-warn")
        line = f"Mode: {self.mode_label.get_text()} - {details}"
        if badge:
            line += f" - {badge}"
        if self.status.get_text() != line:
            self.status.set_text(line)
        self._mark_profile(name)

    def apply_profile(self, name):
        reply = self.call("profile", name=name)
        if not reply or not reply.get("ok"):
            return
        # Show the result at once instead of waiting for the next poll.
        applied = reply.get("applied") or {}
        if applied:
            profile = hardware.PROFILES.get(name, {})
            state = dict((self._last or {}).get("state") or {})
            state["profile"] = name
            state["applied"] = applied
            self.show_mode(applied, state, applied)
            self.show_status(
                f"{profile.get('label', name.title())} mode on - "
                f"{hardware.describe_state(applied)}")
        self._mark_profile(name)

    def on_profile_changed(self, combo):
        if self._suppress:
            return
        key = combo.get_active_id()
        if key and key != "custom":
            self.apply_profile(key)

    def on_governor_changed(self, combo):
        if self._suppress or combo.get_active() < 0:
            return
        self.call("cpu", governor=combo.get_active_text())

    def on_epp_changed(self, combo):
        if self._suppress or combo.get_active() < 0:
            return
        self.call("cpu", epp=combo.get_active_text())

    def on_turbo_changed(self, switch, _param):
        if self._suppress:
            return
        self.call("cpu", turbo=switch.get_active())

    def on_apply_power(self, _button):
        self.call("power", pl1=self.pl1_spin.get_value(), pl2=self.pl2_spin.get_value())

    def on_apply_gpu(self, _button):
        self.call("gpu", power_limit=self.gpu_spin.get_value())

    def on_apply_graphics(self, _button):
        mode = self.graphics_combo.get_active_text()
        if mode:
            self.call("graphics", mode=mode)

    def on_guard_changed(self, switch, _param):
        if self._suppress:
            return
        self.call("guard", enabled=switch.get_active())

    def apply_led_color(self, rgb):
        self.call("led", rgb=list(rgb), brightness=int(self.brightness_scale.get_value()))

    def apply_led_off(self):
        self.call("led", off=True)

    def on_apply_brightness(self, _button):
        self.call("led", brightness_only=True,
                  brightness=int(self.brightness_scale.get_value()))

    def on_apply_zones(self, _button):
        zones = []
        for button in self.zone_buttons:
            rgba = button.get_rgba()
            zones.append([int(round(rgba.red * 255)), int(round(rgba.green * 255)),
                          int(round(rgba.blue * 255))])
        self.call("led", zones=zones, brightness=int(self.brightness_scale.get_value()))

    # ------------------------------------------------------------ polling

    def schedule_poll(self):
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._poll_once, daemon=True).start()

    def poll_loop(self):
        while True:
            self._poll_once()
            time.sleep(2.0)

    def _poll_once(self):
        try:
            data = client.telemetry()["data"]
            GLib.idle_add(self.update_ui, data)
        except client.DaemonError as exc:
            GLib.idle_add(self.show_status,
                          f"{exc} - run: sudo systemctl start monster-center", True)
        finally:
            self._busy = False

    def apply_pending(self):
        return True

    # --------------------------------------------------------------- ui

    def update_ui(self, data):
        if not self.is_visible():
            return False
        self._last = data
        ec = data.get("ec", {})
        cpu = data.get("cpu", {})
        gpu = data.get("gpu", {})
        power = data.get("power", {})
        bat = data.get("battery", {})
        temps = data.get("temps", {})
        cores = temps.get("cpu_cores", {})
        package = next((v for k, v in cores.items() if "Package" in k), None)
        state = data.get("state", {})

        self.cpu_temp.set_text(f"{package:.0f} C" if package is not None else "--")
        hottest = max(cores.values()) if cores else None
        self.cpu_caption_v.set_text(f"{hottest:.0f} C" if hottest else "--")
        self.cpu_freq_v.set_text(
            f"{cpu.get('cur_freq_mhz', 0)} MHz  ({cpu.get('min_freq_mhz')}-"
            f"{cpu.get('max_freq_mhz')} MHz)")
        pkg_w = power.get("package_w")
        self.cpu_power_v.set_text(
            (f"{pkg_w:.1f} W" if pkg_w else "--") +
            f"   limit {power.get('pl1_w', 0):.0f} W")
        mode_label = (hardware.PROFILES.get(state.get("profile"), {}).get("label")
                      or "Custom")
        self.cpu_mode_v.set_text(
            f"{mode_label} - energy preference {cpu.get('epp')}, "
            f"turbo {'on' if cpu.get('turbo') else 'off'}")

        if gpu.get("available"):
            self.gpu_temp.set_text(f"{gpu.get('temp')} C")
            self.gpu_load_v.set_text(f"{gpu.get('util')}%")
            self.gpu_clock_v.set_text(f"{gpu.get('clock_graphics')} MHz")
            self.gpu_power_v.set_text(f"{gpu.get('power_draw'):.0f} W"
                                      if gpu.get("power_draw") else "--")
            self.gpu_mem_v.set_text(f"{gpu.get('mem_used')} / {gpu.get('mem_total')} MiB")
        else:
            self.gpu_temp.set_text("--")
            self.gpu_load_v.set_text("unavailable")

        if ec.get("available"):
            f1, f2 = ec.get("fan1_pct", 0), ec.get("fan2_pct", 0)
            self.fan1_bar.set_fraction(f1 / 100)
            self.fan2_bar.set_fraction(f2 / 100)
            self.fan1_label.set_text(f"{f1}% of range")
            self.fan2_label.set_text(f"{f2}% of range")
            self.cool_fan1_v.set_text(f"{f1}%")
            self.cool_fan2_v.set_text(f"{f2}%")
            self.cool_board_v.set_text(f"{ec.get('board_temp')} C")
        self.cool_cpu_v.set_text(f"{package:.0f} C" if package is not None else "--")
        self.cool_gpu_v.set_text(f"{gpu.get('temp')} C" if gpu.get("available") else "--")
        self.cool_pch_v.set_text(
            f"{list(temps.get('pch', {}).values())[0]:.0f} C"
            if temps.get("pch") else "--")

        gpu_limit_supported = bool(gpu.get("power_limit"))
        self.gpu_spin.set_sensitive(gpu_limit_supported)
        self.apply_gpu_button.set_sensitive(gpu_limit_supported)
        self.gpu_hint.set_text(
            f"Driver reports a limit of {gpu['power_limit']:.0f} W"
            if gpu_limit_supported else
            "This driver does not expose a settable GPU power limit, so the "
            "control is disabled. CPU power limits still control the heat.")

        if bat.get("present"):
            self.bat_level.set_text(f"{bat.get('capacity')}%")
            self.bat_status_v.set_text(bat.get("status") or "--")
            self.bat_ac_v.set_text("plugged in" if bat.get("ac_online") else "on battery")

        ram = data.get("ram") or {}
        if ram.get("available"):
            pct = round(ram["used_mb"] * 100 / ram["total_mb"]) if ram["total_mb"] else 0
            self.ram_v.set_text(
                f"{ram['used_mb']} / {ram['total_mb']} MiB  ({pct}%)")
            if ram.get("swap_total_mb"):
                self.swap_v.set_text(
                    f"{ram['swap_used_mb']} / {ram['swap_total_mb']} MiB")
            else:
                self.swap_v.set_text("none")
        else:
            self.ram_v.set_text("unavailable")
            self.swap_v.set_text("unavailable")

        pch = temps.get("pch", {})
        nvme = temps.get("nvme", {})
        nvme2 = temps.get("nvme2", {})
        wifi = temps.get("wifi", {})
        self.other_rows["PCH"].set_text(
            f"{list(pch.values())[0]:.0f} C" if pch else "--")
        self.other_rows["NVMe 1"].set_text(
            f"{list(nvme.values())[0]:.0f} C" if nvme else "--")
        self.other_rows["NVMe 2"].set_text(
            f"{list(nvme2.values())[0]:.0f} C" if nvme2 else "--")
        self.other_rows["Wi-Fi"].set_text(
            f"{list(wifi.values())[0]:.0f} C" if wifi else "--")

        for child in self.cores_grid.get_children():
            self.cores_grid.remove(child)
        core_items = [(k, v) for k, v in cores.items() if "Package" not in k]
        for index, (label, value) in enumerate(core_items[:16]):
            name = Gtk.Label(label=label.replace("Core ", "Core "), xalign=0)
            name.get_style_context().add_class("mc-caption")
            number = Gtk.Label(label=f"{value:.0f} C", xalign=1)
            self.cores_grid.attach(name, 0, index, 1, 1)
            self.cores_grid.attach(number, 1, index, 1, 1)
        self.cores_grid.show_all()

        live = data.get("mode") or {
            "pl1_w": round(power.get("pl1_w") or 0),
            "pl2_w": round(power.get("pl2_w") or 0),
            "governor": cpu.get("governor", "unknown"),
            "epp": cpu.get("epp", "unknown"),
            "turbo": cpu.get("turbo"),
        }
        self.show_mode(live, state)

        self._suppress = True
        try:
            gov = cpu.get("governor")
            if gov and self.governor_combo.get_active_text() != gov:
                model = self.governor_combo.get_model()
                for i, item in enumerate(model):
                    if item[0] == gov:
                        self.governor_combo.set_active(i)
                        break
            epp = cpu.get("epp")
            if epp:
                model = self.epp_combo.get_model()
                for i, item in enumerate(model):
                    if item[0] == epp:
                        self.epp_combo.set_active(i)
                        break
            self.turbo_switch.set_active(bool(cpu.get("turbo")))
            if self.pl1_spin.get_value() != power.get("pl1_w"):
                self.pl1_spin.set_value(power.get("pl1_w") or 35)
            if self.pl2_spin.get_value() != power.get("pl2_w"):
                self.pl2_spin.set_value(power.get("pl2_w") or 45)
            if gpu.get("available") and gpu.get("power_limit"):
                self.gpu_spin.set_value(gpu["power_limit"])
            mode = data.get("graphics_mode")
            if mode:
                model = self.graphics_combo.get_model()
                for i, item in enumerate(model):
                    if item[0] == mode:
                        self.graphics_combo.set_active(i)
                        break
            guard = state.get("guard", {})
            self.guard_switch.set_active(bool(guard.get("enabled")))
        finally:
            self._suppress = False

        self.diag_buffer.set_text(self.diagnostics_text(data))
        return False

    def diagnostics_text(self, data):
        gpu = data.get("gpu", {})
        power = data.get("power", {})
        ec = data.get("ec", {})
        kbd = data.get("keyboard") or {}
        ident = data.get("identity") or hardware.machine_identity()
        mode = data.get("mode") or hardware.power_state()
        cpu = data.get("cpu") or {}
        state = data.get("state") or {}
        profile_label = hardware.PROFILES.get(state.get("profile"), {}).get("label") or "custom"
        keyboard_line = (
            f"  Keyboard backlight: {kbd.get('zones', 4)} zones on {kbd.get('device')}"
            if kbd.get("available")
            else "  Keyboard backlight: controller 048d:ce00 not found"
        )
        lines = [
            f"Monster Center for the {ident['model'] or 'TULPAR T5 V21.4'}",
            f"  barebone {ident['barebone'] or 'unknown'}, BIOS {ident['bios'] or 'unknown'}, "
            f"EC firmware {ident['ec_firmware'] or 'unknown'}",
            "",
            "Working controls",
            "  CPU power budget (RAPL PL1/PL2), energy preference, turbo, max frequency",
            "  GPU power limit through nvidia-smi (driver decides if it is settable)",
            "  Graphics mode through system76-power (needs a reboot)",
            keyboard_line,
            "    ITE 8291 zone protocol, brightness 0-50, per zone colours",
            "",
            "Read only",
            "  Fan levels and EC temperatures. The EC keeps its own fan curve; the",
            "  manual duty fields at 0xE8C and 0xE9D accept writes and ignore them,",
            "  so manual fan control is not offered.",
            "",
            "Live values",
            f"  EC access: {'yes' if ec.get('available') else 'no'}",
            f"  Mode: {profile_label} ({hardware.describe_state(mode)})",
            f"  Governor {cpu.get('governor', 'unknown')}, turbo "
            f"{'on' if cpu.get('turbo') else 'off'}",
            f"  Power limit: PL1 {power.get('pl1_w', 0):.0f} W, PL2 {power.get('pl2_w', 0):.0f} W",
            f"  GPU: {gpu.get('name') or 'n/a'}",
            f"  GPU power limit: {gpu.get('power_limit') or 'not reported by driver'}",
            f"  Graphics mode: {data.get('graphics_mode') or 'unknown'}",
            f"  Kernel: {self._uname()}",
            "",
            "Daemon socket: " + client.SOCKET_PATH,
        ]
        return "\n".join(lines)

    @staticmethod
    def _uname():
        try:
            with open("/proc/sys/kernel/osrelease") as f:
                return f.read().strip()
        except OSError:
            return "unknown"


def main():
    window = MonsterCenter()
    window.show_all()
    window.hide_status()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
