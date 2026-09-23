#!/bin/bash
# Prepare the isolated Coral detector on this ARM64 Raspberry Pi.
set -euo pipefail
robot_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)
cd "$robot_repo"
robot_uv="$robot_repo/_tmp/coral/tools/bin/uv"
robot_python="$robot_repo/_tmp/coral/detection-venv/bin/python"
if [ ! -x "$robot_uv" ]; then
  echo "Complete boxes/intelligence/NPU/coral/Setup-Trixie.md first." >&2
  exit 1
fi
if [ ! -x "$robot_python" ]; then
  "$robot_uv" venv --python 3.9.25 --seed "$robot_repo/_tmp/coral/detection-venv"
fi
# The 2.14 runtime passed classification but crashed when loading this SSD model.
# Google's wheel matches libedgetpu 16 and works for detection on this Pi.
"$robot_uv" pip install --python "$robot_python" numpy==1.26.4 Pillow==11.3.0 \
  'https://github.com/google-coral/pycoral/releases/download/v2.0.0/tflite_runtime-2.5.0.post1-cp39-cp39-linux_aarch64.whl#sha256=9839c3acb506b5003a9bd3860329a8ae20e675efbae14dbea02659b0054f42c6'
python3 - <<'PY'
import hashlib
from pathlib import Path
from urllib.request import urlopen

root = Path('_tmp/coral/models')
root.mkdir(parents=True, exist_ok=True)
base = 'https://raw.githubusercontent.com/google-coral/test_data/master/'
assets = {
    'ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite': 'b94e2d58222c32f31062c7604e10488e2aba9259ab77462039476a3ba4597fef',
    'coco_labels.txt': 'dc183f003fc753c4c43fae6fdf7f387559449573f13fa32e517fb7453fd380f1',
}
for name, expected in assets.items():
    target = root / name
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
        print(f'Verified {name}')
        continue
    with urlopen(base + name, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit(f'Checksum mismatch for {name}; refusing to install')
    temporary = target.with_suffix('.download')
    temporary.write_bytes(data)
    temporary.replace(target)
    print(f'Installed {name}')
PY
echo 'Ready. Add --detect when starting robot_control/server.py.'
