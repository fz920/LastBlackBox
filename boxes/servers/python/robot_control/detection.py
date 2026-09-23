"""Run Coral inference separately from camera capture and motor control."""
import json
import os
import select
import struct
import subprocess
import threading
import time
from pathlib import Path


class Detection:
    def __init__(self, frames, python, model, labels, max_age=1.2, clock=time.monotonic):
        self.frames = frames
        self.command = [str(python), '-u', str(Path(__file__).with_name('detection_worker.py')),
                        '--model', str(model), '--labels', str(labels)]
        self.max_age, self.clock = max_age, clock
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.result = None
        self.error = 'Starting Coral object detection'
        self.process = None
        self.thread = threading.Thread(target=self._run, name='coral-detection', daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self.lock:
            result, error = self.result, self.error
        live = result is not None and 0 <= self.clock() - result['frame_time'] <= self.max_age and not error
        status = {"enabled": True, "live": live, "error": error or ('' if live else 'Waiting for fresh detection'),
                  "model": 'SSD MobileNet V2 / COCO', "threshold": 0.5,
                  "objects": result['objects'] if live else [],
                  "frame": result['frame'] if live else None,
                  "inference_ms": result['inference_ms'] if live else None}
        return (result if live else None), status

    def _reply(self, process, timeout):
        deadline, data = self.clock() + timeout, bytearray()
        while not self.finished.is_set():
            if self.clock() >= deadline:
                raise TimeoutError('Coral inference timed out')
            if not select.select([process.stdout], [], [], 0.1)[0]:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Coral worker stopped; check its runtime and USB connection')
            data.extend(chunk)
            if len(data) > 65536:
                raise ValueError('Coral response too large')
            if b'\n' in data:
                return json.loads(data)
        raise InterruptedError('Detection stopping')

    def _run(self):
        while not self.finished.is_set():
            process = None
            try:
                process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           bufsize=0, start_new_session=True)
                self.process = process
                if self._reply(process, 15) != {"ready": True}:
                    raise ValueError('Invalid Coral startup response')
                previous = None
                while not self.finished.is_set():
                    jpeg, timestamp, number, _ = self.frames.latest()
                    if not jpeg or number == previous or self.clock() - timestamp > self.max_age:
                        self.finished.wait(0.05)
                        continue
                    started = self.clock()
                    packet = memoryview(struct.pack('!I', len(jpeg)) + jpeg)
                    while packet and not self.finished.is_set():
                        written = process.stdin.write(packet)
                        if not written:
                            raise BrokenPipeError('Coral input closed')
                        packet = packet[written:]
                    answer = self._reply(process, 3)
                    if not isinstance(answer.get('objects'), list) or len(answer['objects']) > 10:
                        raise ValueError('Invalid Coral detections')
                    result = {**answer, 'jpeg': jpeg, 'frame_time': timestamp, 'frame': number}
                    with self.lock:
                        self.result, self.error = result, ''
                    previous = number
                    self.finished.wait(max(0, 0.2 - (self.clock() - started)))
            except Exception as exc:
                with self.lock:
                    self.result, self.error = None, str(exc)
                if not self.finished.is_set():
                    print(f'Object detection: {exc}', flush=True)
            finally:
                if process:
                    self._terminate(process)
                self.process = None
            self.finished.wait(5)

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            except ProcessLookupError:
                pass
        process.stdin.close()
        process.stdout.close()

    def close(self):
        self.finished.set()
        process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        if self.thread.is_alive():
            self.thread.join(timeout=3)
