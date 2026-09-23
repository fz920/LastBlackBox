#!/usr/bin/env python3
"""Coral-only worker: JPEGs in, detections out. No camera or motor access."""
import argparse
import io
import json
import math
import struct
import sys
import time
from pathlib import Path


def decode_objects(boxes, classes, scores, count, labels, threshold, model_size, resized_size):
    """Undo top-left letterboxing; return bounded, normalized image coordinates."""
    sx, sy = model_size[0] / resized_size[0], model_size[1] / resized_size[1]
    objects = []
    for index in range(min(int(count), len(boxes), len(classes), len(scores))):
        score = float(scores[index])
        coords = [float(value) for value in boxes[index]]
        if not all(math.isfinite(value) for value in [score, *coords]) or score < threshold:
            continue
        ymin, xmin, ymax, xmax = coords
        box = [max(0., min(1., value)) for value in (xmin * sx, ymin * sy, xmax * sx, ymax * sy)]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        category = int(classes[index])
        objects.append({"id": category, "label": labels.get(category, f"Object {category}"),
                        "score": round(score, 3), "box": [round(value, 5) for value in box]})
    return sorted(objects, key=lambda obj: obj["score"], reverse=True)[:10]


class Detector:
    def __init__(self, model, labels, threshold):
        import numpy as np
        import tflite_runtime.interpreter as tflite
        self.np = np
        self.threshold = threshold
        self.labels = {}
        for index, line in enumerate(Path(labels).read_text().splitlines()):
            if line.strip():
                parts = line.split(maxsplit=1)
                if parts[0].isdigit() and len(parts) == 2:
                    self.labels[int(parts[0])] = parts[1]
                else:
                    self.labels[index] = line.strip()
        self.interpreter = tflite.Interpreter(model_path=str(model), num_threads=1,
            experimental_delegates=[tflite.load_delegate('libedgetpu.so.1')])
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.outputs = self.interpreter.get_output_details()
        _, self.height, self.width, channels = self.input['shape']
        if self.input['dtype'] != np.uint8 or channels != 3 or len(self.outputs) != 4:
            raise ValueError('Expected the uint8 SSD MobileNet COCO detector')

    def detect(self, jpeg):
        from PIL import Image
        image = Image.open(io.BytesIO(jpeg)).convert('RGB')
        scale = min(self.width / image.width, self.height / image.height)
        resized = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        tensor = self.np.zeros((1, self.height, self.width, 3), dtype=self.np.uint8)
        tensor[0, :resized[1], :resized[0]] = self.np.asarray(image.resize(resized, Image.Resampling.BILINEAR))
        self.interpreter.set_tensor(self.input['index'], tensor)
        start = time.perf_counter()
        self.interpreter.invoke()
        elapsed = (time.perf_counter() - start) * 1000
        boxes, classes, scores, count = [self.interpreter.get_tensor(item['index'])[0] for item in self.outputs]
        return {"objects": decode_objects(boxes, classes, scores, count, self.labels,
                    self.threshold, (self.width, self.height), resized),
                "inference_ms": round(elapsed, 1), "width": image.width, "height": image.height}


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError('Frame pipe closed')
        data.extend(chunk)
    return bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--labels', required=True)
    parser.add_argument('--threshold', type=float, default=0.5)
    args = parser.parse_args()
    detector = Detector(args.model, args.labels, args.threshold)
    print(json.dumps({"ready": True}), flush=True)
    while True:
        try:
            size = struct.unpack('!I', read_exact(sys.stdin.buffer, 4))[0]
            if not 0 < size <= 2_000_000:
                raise ValueError('Invalid JPEG length')
            result = detector.detect(read_exact(sys.stdin.buffer, size))
            print(json.dumps(result, separators=(',', ':'), allow_nan=False), flush=True)
        except EOFError:
            return


if __name__ == '__main__':
    main()
