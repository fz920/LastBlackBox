#!/usr/bin/env python3
"""Install one pinned GGUF and an ARM64 CPU runtime, keeping 1 GiB free."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[4]
ROOT = REPO / '_tmp/llm'
RESERVE = 1024**3
RUNTIME_TAG = 'b11139'
RUNTIME_SHA = '4fbd343b3cb43514cf4ff6ec4b8adb9c5db7e76d2f7d6e3792c2526d9cbefe5b'
RUNTIME_URL = f'https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_TAG}/llama-{RUNTIME_TAG}-bin-ubuntu-arm64.tar.gz'
MODEL_NAME = 'qwen2.5-0.5b-instruct-q4_k_m.gguf'
MODEL_REVISION = '9217f5db79a29953eb74d5343926648285ec7e67'
MODEL_SHA = '74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db'
MODEL_SIZE = 491400032
MODEL_URL = f'https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/{MODEL_REVISION}/{MODEL_NAME}'


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def download(url, target, expected_hash, expected_size):
    if target.is_file() and target.stat().st_size == expected_size and digest(target) == expected_hash:
        print(f'Already verified: {target.name}', flush=True)
        return
    if shutil.disk_usage(ROOT).free < RESERVE + expected_size + 64 * 1024**2:
        raise SystemExit('Insufficient space: refusing to reduce free storage below 1 GiB.')
    temporary = target.with_suffix(target.suffix + '.part')
    try:
        total, report_at, checksum = 0, 0, hashlib.sha256()
        with urlopen(url, timeout=60) as source, temporary.open('wb') as output:
            while block := source.read(1024**2):
                total += len(block)
                if total > expected_size or shutil.disk_usage(ROOT).free < RESERVE + len(block):
                    raise RuntimeError('Unexpected download size or low disk space')
                output.write(block)
                checksum.update(block)
                if total >= report_at:
                    print(f'{target.name}: {total / 1024**2:.0f}/{expected_size / 1024**2:.0f} MiB', flush=True)
                    report_at = total + 100 * 1024**2
        if total != expected_size or checksum.hexdigest() != expected_hash:
            raise RuntimeError(f'Download checksum/size mismatch: {target.name}')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    if platform.machine() != 'aarch64':
        raise SystemExit('This installer is pinned for the ARM64 Raspberry Pi.')
    ROOT.mkdir(parents=True, exist_ok=True)
    runtime = ROOT / 'runtime' / f'llama-{RUNTIME_TAG}'
    manifest = ROOT / 'runtime-manifest.json'
    expected = {'tag': RUNTIME_TAG, 'url': RUNTIME_URL, 'sha256': RUNTIME_SHA}
    if not ((runtime / 'llama-server').is_file() and manifest.is_file()
            and json.loads(manifest.read_text()) == expected):
        archive = ROOT / 'runtime.tar.gz'
        download(RUNTIME_URL, archive, RUNTIME_SHA, 13597715)
        with tarfile.open(archive) as source:
            if sum(member.size for member in source.getmembers()) > 64 * 1024**2:
                raise SystemExit('Unexpected runtime archive size')
            source.extractall(ROOT / 'runtime', filter='data')
        manifest.write_text(json.dumps(expected, indent=2) + '\n')
        archive.unlink()
    subprocess.run([str(runtime / 'llama-server'), '--version'], check=True, timeout=15)
    (ROOT / 'models').mkdir(exist_ok=True)
    download(MODEL_URL, ROOT / 'models' / MODEL_NAME, MODEL_SHA, MODEL_SIZE)
    (ROOT / 'model-manifest.json').write_text(json.dumps({
        'url': MODEL_URL, 'revision': MODEL_REVISION, 'sha256': MODEL_SHA,
        'bytes': MODEL_SIZE}, indent=2) + '\n')
    print(f'Ready. Free storage: {shutil.disk_usage(ROOT).free / 1024**3:.2f} GiB')


if __name__ == '__main__':
    main()
