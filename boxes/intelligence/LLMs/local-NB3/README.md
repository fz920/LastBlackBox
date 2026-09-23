# Small local LLM on NB3

Installed and benchmarked on 2026-09-23: Raspberry Pi 4, 2 GB RAM, Debian 13
ARM64, alongside the running robot camera, Coral detector, and website.

**The model runs, but its answers are not reliable enough to replace the current
object-count descriptions.** This is a standalone text experiment. It receives
object labels/counts, not images, and has no connection to motors or speech output.
The website now offers a [constrained LLM description mode](../../../servers/python/robot_control/README.md#local-llm-descriptions)
alongside deterministic descriptions. It restricts output to verified count/name
phrases and falls back to the basic description on failure. The unrestricted
standalone experiment described here remains separate from speech and motors.

## Installed files and storage

| Component | Version / location | Disk space |
| --- | --- | ---: |
| Qwen2.5-0.5B-Instruct Q4_K_M | `_tmp/llm/models/qwen2.5-0.5b-instruct-q4_k_m.gguf` | 491,400,032 bytes / 469 MiB |
| llama.cpp ARM64 CPU runtime | `_tmp/llm/runtime/llama-b11139/` | about 32 MiB |
| Manifests, reports, logs | `_tmp/llm/` | under 1 MiB |

Total is about **501 MiB**, with **1.37 GiB free** immediately after setup.
Only one model file was downloaded. The 14 MB runtime archive was deleted after
extraction. No source checkout, compiler environment, Docker image, or Hugging
Face model-cache copy was created. No existing user files were removed.
Everything large lives under Git-ignored `_tmp/llm/`; commit the scripts/docs,
not the model. Nothing starts automatically at boot.

Sources: [official Qwen model](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/tree/9217f5db79a29953eb74d5343926648285ec7e67)
and [official llama.cpp release b11139](https://github.com/ggml-org/llama.cpp/releases/tag/b11139).
The installer pins these versions and validates SHA-256 checksums. The runtime
reports version `0.4.1-dev`, build 11139, commit `42916d83f`.

## Reproduce installation

From the repository root:

```bash
python3 boxes/intelligence/LLMs/local-NB3/setup.py
```

Requires this ARM64 Pi, Python 3.13, and internet access for downloads. No sudo
is required. The installer checks disk space before downloading and refuses to
reduce free space below 1 GiB. Verified files are reused on subsequent runs;
there is no second model copy.

## Try a question

Keep the robot website running with Coral detection. In one Pi terminal:

```bash
cd ~/LastBlackBox
bash boxes/intelligence/LLMs/local-NB3/run-server.sh
```

Wait for the server to finish loading. In another Pi terminal:

```bash
cd ~/LastBlackBox
python3 boxes/intelligence/LLMs/local-NB3/ask.py "How many people can you see?"
```

The script prints the detection snapshot, model answer, and elapsed time.
Answers refer to the snapshot taken before generation; the scene may change
while the model is answering. This does not make the robot speak or move.
**Check answers against the printed detections: the model can contradict them.**

Press **Ctrl+C in the server terminal** when finished to release model memory.
The server listens only on `127.0.0.1:8081`. It uses two CPU threads at reduced
scheduling priority, a 1,024-token context, one request slot, and CPU inference.
The question script limits answers to 48 tokens. Downloads are disabled during
inference; the Coral remains dedicated to object detection.

## Benchmark

With the website's camera and detector running, and port 8081 free:

```bash
python3 boxes/intelligence/LLMs/local-NB3/benchmark.py
```

This measures a five-second baseline, starts the LLM, asks six short questions,
and checks website status, fresh JPEG delivery, detections, and memory every
half-second. It sends no motor commands. The model server is shut down in cleanup,
including on failure. If available memory falls below 120 MiB, the benchmark
terminates the model process. Reports overwrite `_tmp/llm/benchmark.json` and
`server.log`, so repeated runs do not accumulate model copies or large logs.

Two runs were made: an initial instruction prompt and one revised prompt that
explicitly lists missing attributes as unknown. Their local reports are
`benchmark-initial.json` and `benchmark.json` respectively.

| Measurement | Observed result |
| --- | --- |
| First model startup, including warmup | 18.2 seconds |
| Second startup with files in OS cache | 4.0 seconds |
| First answer with an uncached prompt | 9.3–9.8 seconds |
| Subsequent short answers with shared prompt cache | 1.6–4.7 seconds |
| Generation speed | about 6.4–7.6 tokens/second |
| Peak sampled model-process RSS | 545–548 MiB |
| Camera capture during questions | 15 fps |
| Median NPU inference during questions | 14.6–17.7 ms |
| p95 combined status/detection/JPEG request time during questions | 21.7–33.2 ms |

All sampled camera and detection states stayed live, there were no HTTP errors,
and motors remained stopped. The first run increased system-wide swap usage
from about 580 to 799 MiB; this Pi was already using swap before the test. Thus,
smooth camera performance does not prove the LLM has no effect on other apps.
These measurements cover short prompts and answers, not long conversations.

## Answer quality

The initial prompt correctly answered “2 people” for a counting question and
named a person in a live snapshot. However:

- It omitted one person from a description of two people and a laptop.
- It invented “The laptop is red” without colour information.
- It invented a left/right relationship without position information.
- It claimed to see objects when given an empty detection list.

The revised prompt reduced some guessing but introduced false refusals and
incorrect “No objects were detected” answers despite non-empty input. Prompt
instructions alone did not make this model dependable on these cases.

For website integration, preserve detector counts and unknown attributes
in application code, validate any generated description, and retain the current
description as a fallback. The LLM should not determine whether a route is clear
or issue motor commands. The linked website integration uses these restrictions;
the standalone benchmark and question script still expose the raw model answers.
