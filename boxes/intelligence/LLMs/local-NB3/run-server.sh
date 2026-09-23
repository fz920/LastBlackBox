#!/bin/bash
# Standalone local experiment. Stop with Ctrl+C to release the model's RAM.
set -euo pipefail
llm_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)
exec nice -n 10 "$llm_repo/_tmp/llm/runtime/llama-b11139/llama-server" \
  --model "$llm_repo/_tmp/llm/models/qwen2.5-0.5b-instruct-q4_k_m.gguf" \
  --host 127.0.0.1 --port 8081 --offline --no-webui \
  --threads 2 --threads-batch 2 --ctx-size 1024 \
  --batch-size 128 --ubatch-size 64 --parallel 1 --n-predict 64 --n-gpu-layers 0
