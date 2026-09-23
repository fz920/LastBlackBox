#!/bin/bash
# Install the tutorial's Revision 2 NB3 mouth/ears driver on this Raspberry Pi.
# Build first as your normal user; run this installation step with sudo.
set -euo pipefail
nb3_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)
nb3_kernel=$(uname -r)
nb3_module="$nb3_repo/_tmp/nb3-audio-driver/nb3-audio-module.ko"
if [ "$EUID" -ne 0 ]; then
  echo 'Run this installation step with sudo.' >&2
  exit 1
fi
if [ ! -f "$nb3_module" ] || [ "$(modinfo -F vermagic "$nb3_module" | cut -d' ' -f1)" != "$nb3_kernel" ]; then
  echo 'Build the NB3 module for the currently running kernel first; see Setup-Pi.md.' >&2
  exit 1
fi
# Refuse an unexpected pre-existing overlay instead of changing its pin routing.
python3 - <<'PY'
from pathlib import Path
text = Path('/boot/firmware/config.txt').read_text()
for line in text.splitlines():
    if line.strip().startswith('dtoverlay=max98357a') and line.strip() != 'dtoverlay=max98357a,sdmode-pin=16':
        raise SystemExit('Existing MAX98357A configuration differs; review it before installing.')
PY
nb3_backup="/var/backups/nb3-audio/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$nb3_backup"
cp -a /boot/firmware/config.txt "$nb3_backup/config.txt"
for nb3_file in /etc/modprobe.d/nb3-audio.conf /etc/modules-load.d/nb3-audio.conf; do
  if [ -f "$nb3_file" ]; then cp -a "$nb3_file" "$nb3_backup/$(basename "$(dirname "$nb3_file")").conf"; fi
done
install -m 644 -D "$nb3_module" "/lib/modules/$nb3_kernel/extra/nb3-audio-module.ko"
# Both codecs match maxim,max98357a. Select the repository's duplex driver
# instead of letting the stock playback-only codec bind first at boot.
printf '%s\n' 'blacklist snd_soc_max98357a' > /etc/modprobe.d/nb3-audio.conf
printf '%s\n' 'nb3-audio-module' > /etc/modules-load.d/nb3-audio.conf
depmod -a "$nb3_kernel"
python3 - <<'PY'
from pathlib import Path
path = Path('/boot/firmware/config.txt')
text = path.read_text()
if '# NB3 tutorial audio (setup-pi.sh)' not in text:
    # Existing exact overlay, if any, moves into an unconditional block.
    text = '\n'.join(line for line in text.splitlines()
                     if line.strip() != 'dtoverlay=max98357a,sdmode-pin=16')
    text += '\n\n[all]\n# NB3 tutorial audio (setup-pi.sh)\ndtparam=i2s=on\ndtoverlay=max98357a,sdmode-pin=16\n'
    path.write_text(text)
PY
echo "Configuration backup: $nb3_backup"
if [ -d /sys/module/snd_soc_max98357a ]; then
  echo 'The stock codec is already loaded; reboot to select the NB3 driver.'
  exit 0
fi
modprobe nb3-audio-module
if [ ! -d /proc/device-tree/max98357a ]; then
  if ! dtoverlay max98357a sdmode-pin=16; then
    echo 'Persistent configuration installed. Reboot to activate the audio overlay.'
    exit 0
  fi
fi
udevadm settle
aplay -l
arecord -l
echo 'NB3 driver installed. No audio test or reboot was started automatically.'
