#!/bin/bash
# Install Debian's offline speech engine locally; no sudo or system changes.
set -euo pipefail
speech_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)
speech_root="$speech_repo/_tmp/speech"
mkdir -p "$speech_root/packages" "$speech_root/root" "$speech_root/bin"
cd "$speech_root/packages"
apt-get download espeak-ng libespeak-ng1 espeak-ng-data libpcaudio0 libsonic0
for speech_package in ./*.deb; do
  dpkg-deb -x "$speech_package" "$speech_root/root"
done
cat > "$speech_root/bin/espeak-ng" <<'SH'
#!/bin/bash
set -euo pipefail
speech_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
speech_arch=$(dpkg-architecture -qDEB_HOST_MULTIARCH)
export LD_LIBRARY_PATH="$speech_root/root/usr/lib/$speech_arch${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export ESPEAK_DATA_PATH="$speech_root/root/usr/lib/$speech_arch"
exec "$speech_root/root/usr/bin/espeak-ng" "$@"
SH
chmod +x "$speech_root/bin/espeak-ng"
"$speech_root/bin/espeak-ng" --version
echo 'Speech engine ready. The NB3 audio driver must also be installed.'
