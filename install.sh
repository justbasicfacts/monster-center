#!/usr/bin/env bash
# Install Monster Center (GUI + privileged daemon) on Debian/Ubuntu/Pop!_OS.
set -euo pipefail

APP_NAME="monster-center"
INSTALL_DIR="/opt/${APP_NAME}"
BIN="/usr/local/bin/${APP_NAME}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-${USER}}"
if [ -n "${PKEXEC_UID:-}" ] && [ "${PKEXEC_UID}" != "0" ]; then
    TARGET_USER="$(getent passwd "${PKEXEC_UID}" | cut -d: -f1)"
fi

info() { printf '\033[0;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run this with sudo: sudo ./install.sh"
[ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ] || fail "cannot determine the desktop user"

info "checking runtime packages"
export DEBIAN_FRONTEND=noninteractive
need=()
apt-get update -qq >/dev/null 2>&1 || true
for pkg in python3 python3-gi gir1.2-gtk-3.0 python3-usb lm-sensors; do
    dpkg -s "$pkg" >/dev/null 2>&1 || need+=("$pkg")
done
if [ ${#need[@]} -gt 0 ]; then
    info "installing: ${need[*]}"
    apt-get install -y -qq "${need[@]}" >/dev/null 2>&1 || \
        info "package install reported an error, continuing (check apt yourself)"
fi
/usr/bin/python3 - <<'PY' || fail "python3-gi with GTK 3 is required"
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
PY

info "copying application files to ${INSTALL_DIR}"
rm -rf "${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
cp -r "${SRC_DIR}/monster_center" "${INSTALL_DIR}/"
find "${INSTALL_DIR}" -name '__pycache__' -type d -prune -exec rm -rf {} +
chown -R root:root "${INSTALL_DIR}"

info "writing ${BIN}"
cat > "${BIN}" <<EOF
#!/bin/sh
export PYTHONPATH="${INSTALL_DIR}"
exec /usr/bin/python3 -m monster_center.cli "\$@"
EOF
chmod 755 "${BIN}"

info "installing desktop entry"
install -m 644 "${SRC_DIR}/monster-center.desktop" /usr/share/applications/monster-center.desktop
command -v update-desktop-database >/dev/null && update-desktop-database -q || true

info "configuring the embedded controller interface (read only)"
cat > /etc/modprobe.d/monster-center-ec.conf <<'EOF'
# Monster Center only reads the EC (fan levels and temperatures).
# write_support stays off so no process can poke EC registers by accident.
options ec_sys write_support=0
EOF
echo ec_sys > /etc/modules-load.d/monster-center-ec.conf
modprobe -r ec_sys 2>/dev/null || true
modprobe ec_sys write_support=0 || true

info "recording the desktop user (${TARGET_USER})"
cat > /etc/monster-center.conf <<EOF
# user allowed to talk to the monster-center daemon socket
user=${TARGET_USER}
EOF

info "installing the systemd service"
install -m 644 "${SRC_DIR}/systemd/monster-center.service" /etc/systemd/system/monster-center.service
systemctl daemon-reload
systemctl enable monster-center.service >/dev/null 2>&1
systemctl restart monster-center.service

info "installing the power-mode key watcher"
install -m 644 "${SRC_DIR}/systemd/monster-center-powermode.service" /etc/systemd/system/monster-center-powermode.service
systemctl daemon-reload
systemctl enable monster-center-powermode.service >/dev/null 2>&1
systemctl restart monster-center-powermode.service

info "installing the power-mode popup for ${TARGET_USER}"
TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
TARGET_UID="$(id -u "${TARGET_USER}")"
TARGET_GROUP="$(id -gn "${TARGET_USER}")"
install -d -m 755 -o "${TARGET_USER}" -g "${TARGET_GROUP}" "${TARGET_HOME}/.config/systemd/user"
install -m 644 -o "${TARGET_USER}" -g "${TARGET_GROUP}" \
    "${SRC_DIR}/systemd/monster-center-powermode-popup.service" \
    "${TARGET_HOME}/.config/systemd/user/monster-center-powermode-popup.service"
runuser -u "${TARGET_USER}" -- env XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
    systemctl --user daemon-reload 2>/dev/null || true
if ! runuser -u "${TARGET_USER}" -- env XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
    systemctl --user enable --now monster-center-powermode-popup.service 2>/dev/null; then
    printf '\033[0;33m!!\033[0m popup service not started (no session for %s). Enable it later with:\n' "${TARGET_USER}"
    printf '   systemctl --user enable --now monster-center-powermode-popup.service\n'
fi

sleep 1
if systemctl --quiet is-active monster-center.service; then
    info "daemon is running"
else
    printf '\033[0;33m!!\033[0m daemon failed to start, see: journalctl -u monster-center -n 40\n'
fi

info "done. Launch it from the application grid ('Monster Center') or run: ${APP_NAME} status"
