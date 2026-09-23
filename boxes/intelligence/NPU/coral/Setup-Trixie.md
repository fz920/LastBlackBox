# Coral USB setup on this Raspberry Pi

Verified on 2026-09-23: Raspberry Pi 4, 64-bit Debian 13 (Trixie), Coral USB
Accelerator connected at USB 3 speed. This follows the [repo tutorial](README.md)
using its TensorFlow Lite example, with compatibility adjustments for this OS.

## Run the test again

From a terminal on the Pi:

```bash
cd ~/LastBlackBox
bash boxes/intelligence/NPU/coral/test-npu.sh
```

No sudo is needed for inference. The test uses a supplied parrot image; it does
not open the robot camera, audio devices, or motor controller.

The initial successful run reported:

```text
Ara macao (Scarlet Macaw): 0.77734
```

First inference: 17.1 ms including model loading. Subsequent inference: roughly
4.2–4.6 ms. These times cover model invocation, not camera capture or a complete
vision application. The test explicitly loads the Edge TPU delegate and a
model compiled for the Edge TPU.

## What was installed

For live camera object detection, see the
[robot website instructions](../../../servers/python/robot_control/README.md#show-what-the-coral-detects).
That feature uses a separate Python 3.9 / Google's TFLite 2.5 environment:
the Python 3.11 / TFLite 2.14 setup below passed the bird classification test but
crashed when loading the SSD detector. Do not use it for that detector.

| Component | Location / version |
| --- | --- |
| Standard-speed Edge TPU runtime | `libedgetpu1-std` 16.0, installed through APT |
| Coral-only Python environment | `_tmp/coral/venv`, Python 3.11.16 |
| Python packages | `tflite-runtime==2.14.0`, `numpy==1.26.4`, `Pillow==12.3.0` |
| Python provisioning tool | `uv==0.12.18` in `_tmp/coral/tools` |
| Tutorial example checkout | `_tmp/coral/tflite` |
| Example revision tested | `eced31ac01e9c2636150decef7d3c335d0feb304` |

The OS Python and existing `_tmp/LBB` environment remain available. The Coral
environment is separate and does not include the camera/audio dependencies
needed by `look-NB3` or `listen-NB3`. PyCoral is not required for this tutorial.

To use this environment interactively:

```bash
cd ~/LastBlackBox
source _tmp/coral/venv/bin/activate
python --version
# Run Coral Python programs here.
deactivate
```

## Reproduce the setup

Run these commands from `~/LastBlackBox` on the Pi.

### 1. Install the system runtime and USB permissions

Review [setup-system.sh](setup-system.sh), then run:

```bash
sudo bash boxes/intelligence/NPU/coral/setup-system.sh
```

This adds the official Coral APT feed with a scoped `signed-by` key, installs
the standard-speed runtime, and configures USB access for the `plugdev` group.
The user `fengzhe` already belongs to that group. The rules cover both device
identities: `1a6e:089a` before initialization and `18d1:9302` after the runtime
loads firmware. Both are normal. If USB access fails after installation, unplug
and reconnect the Coral, then retry the test.

System files managed by the script:

- `/usr/share/keyrings/coral-edgetpu.asc`
- `/etc/apt/sources.list.d/coral-edgetpu.list`
- `/etc/udev/rules.d/71-edgetpu.rules`

### 2. Create a compatible Python environment

The Pi's default Python is 3.13. This setup uses Python 3.11 for the published
ARM64 TensorFlow Lite wheel. NumPy is pinned below 2 for that wheel's binary
compatibility. On a fresh checkout, provision the environment as follows:

```bash
python3 -m venv _tmp/coral/bootstrap
_tmp/coral/bootstrap/bin/python -m pip install --target _tmp/coral/tools uv==0.12.18
_tmp/coral/tools/bin/uv venv --python 3.11.16 --seed _tmp/coral/venv
_tmp/coral/tools/bin/uv pip install --python _tmp/coral/venv/bin/python \
  tflite-runtime==2.14.0 numpy==1.26.4 Pillow==12.3.0
```

The Python download is managed by uv in the user's home directory. This does
not replace `/usr/bin/python3`.

### 3. Download the tutorial and test data

For a fresh setup:

```bash
git clone https://github.com/google-coral/tflite.git _tmp/coral/tflite
git -C _tmp/coral/tflite checkout eced31ac01e9c2636150decef7d3c335d0feb304
```

The original `install_requirements.sh` installs unpinned dependencies. Instead,
the packages are pinned above and the same test assets are downloaded here.
The old example also uses `Image.ANTIALIAS`, which current Pillow replaces with
`Image.Resampling.LANCZOS`:

```bash
python3 - <<'PY'
from pathlib import Path
from urllib.request import urlretrieve

root = Path('_tmp/coral/tflite/python/examples/classification')
base = 'https://raw.githubusercontent.com/google-coral/edgetpu/master/test_data/'
assets = [
    ('models', 'mobilenet_v2_1.0_224_inat_bird_quant_edgetpu.tflite'),
    ('models', 'mobilenet_v2_1.0_224_inat_bird_quant.tflite'),
    ('models', 'inat_bird_labels.txt'),
    ('images', 'parrot.jpg'),
]
for folder, name in assets:
    target = root / folder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    urlretrieve(base + name, target)
script = root / 'classify_image.py'
script.write_text(script.read_text().replace('Image.ANTIALIAS', 'Image.Resampling.LANCZOS'))
PY
bash boxes/intelligence/NPU/coral/test-npu.sh
```

Source: [Coral USB Accelerator setup](https://coral.ai/docs/accelerator/get-started/)
and the [Google Coral TensorFlow Lite examples](https://github.com/google-coral/tflite).
