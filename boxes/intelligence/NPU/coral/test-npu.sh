#!/bin/bash
# Run the repository tutorial's image classification test on the USB Edge TPU.
set -euo pipefail
coral_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)
coral_python="$coral_repo/_tmp/coral/venv/bin/python"
coral_example="$coral_repo/_tmp/coral/tflite/python/examples/classification"
if [ ! -x "$coral_python" ] || [ ! -f "$coral_example/classify_image.py" ]; then
  echo "Complete boxes/intelligence/NPU/coral/Setup-Trixie.md first." >&2
  exit 1
fi
exec "$coral_python" "$coral_example/classify_image.py" \
  --model "$coral_example/models/mobilenet_v2_1.0_224_inat_bird_quant_edgetpu.tflite" \
  --labels "$coral_example/models/inat_bird_labels.txt" \
  --input "$coral_example/images/parrot.jpg" \
  --count 10
