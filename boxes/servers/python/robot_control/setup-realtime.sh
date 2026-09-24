#!/bin/bash
# A small WebSocket client only; no model downloads or system Python changes.
set -euo pipefail
realtime_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)
python3 -m pip install --no-cache-dir --no-compile --only-binary=:all: \
  --target "$realtime_repo/_tmp/robot-control/python" 'websocket-client==1.9.2'
echo 'Realtime dependency installed. Add the API key on the Pi when ready; see README.md.'
