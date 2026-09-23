#!/bin/bash
# Install the repository's standard-speed Coral runtime on Debian 13.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi
coral_tmp=$(mktemp -d)
trap 'rm -rf "$coral_tmp"' EXIT
curl --fail --show-error --location https://packages.cloud.google.com/apt/doc/apt-key.gpg --output "$coral_tmp/coral.asc"
install -m 0644 "$coral_tmp/coral.asc" /usr/share/keyrings/coral-edgetpu.asc
cat > /etc/apt/sources.list.d/coral-edgetpu.list <<'SOURCE'
deb [signed-by=/usr/share/keyrings/coral-edgetpu.asc] https://packages.cloud.google.com/apt coral-edgetpu-stable main
SOURCE
apt-get update
apt-get install -y libedgetpu1-std
# Cover both the initial and firmware-loaded USB identities, including SSH use.
cat > /etc/udev/rules.d/71-edgetpu.rules <<'RULES'
SUBSYSTEM=="usb", ATTR{idVendor}=="1a6e", ATTR{idProduct}=="089a", MODE="0660", GROUP="plugdev", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", ATTR{idProduct}=="9302", MODE="0660", GROUP="plugdev", TAG+="uaccess"
RULES
udevadm control --reload-rules
udevadm trigger --action=change --subsystem-match=usb --attr-match=idVendor=1a6e
udevadm trigger --action=change --subsystem-match=usb --attr-match=idVendor=18d1
udevadm settle
ldconfig
echo "Coral runtime and USB permissions installed."
