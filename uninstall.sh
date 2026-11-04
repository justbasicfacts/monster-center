#!/usr/bin/env bash
# Remove Monster Center.
set -uo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo ./uninstall.sh" >&2; exit 1; }

systemctl disable --now monster-center.service 2>/dev/null
systemctl disable --now monster-center-powermode.service 2>/dev/null
rm -f /etc/systemd/system/monster-center.service
rm -f /etc/systemd/system/monster-center-powermode.service

# user session popup, for the desktop user recorded at install time
POPUP_USER="$(sed -n 's/^user=//p' /etc/monster-center.conf 2>/dev/null)"
if [ -n "${POPUP_USER}" ]; then
    POPUP_HOME="$(getent passwd "${POPUP_USER}" | cut -d: -f6)"
    POPUP_UID="$(id -u "${POPUP_USER}" 2>/dev/null || true)"
    if [ -n "${POPUP_HOME}" ] && [ -n "${POPUP_UID}" ]; then
        runuser -u "${POPUP_USER}" -- env XDG_RUNTIME_DIR="/run/user/${POPUP_UID}" \
            systemctl --user disable --now monster-center-powermode-popup.service 2>/dev/null
        rm -f "${POPUP_HOME}/.config/systemd/user/monster-center-powermode-popup.service"
        runuser -u "${POPUP_USER}" -- env XDG_RUNTIME_DIR="/run/user/${POPUP_UID}" \
            systemctl --user daemon-reload 2>/dev/null
    fi
fi

rm -f /usr/local/bin/monster-center
rm -f /usr/share/applications/monster-center.desktop
rm -f /etc/monster-center.conf
rm -f /etc/modprobe.d/monster-center-ec.conf
rm -f /etc/modules-load.d/monster-center-ec.conf
rm -rf /opt/monster-center /var/lib/monster-center /run/monster-center
systemctl daemon-reload
command -v update-desktop-database >/dev/null && update-desktop-database -q || true
echo "Monster Center removed. The ec_sys module stays available for monitoring tools."
