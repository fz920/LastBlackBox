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
        self.changed = threading.Event()
        self.interrupted = threading.Event()
        self.enabled = True
        self.result = None
        self.error = 'Starting Coral object detection'
        self.process = None
        self.thread = threading.Thread(target=self._run, name='coral-detection', daemon=True)

    def start(self):
        self.thread.start()

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Detection enabled must be true or false')
        with self.lock:
            if self.finished.is_set():
                raise ValueError('Detection is shutting down')
            if enabled != self.enabled:
                self.enabled = enabled
                self.result = None
                self.error = 'Starting Coral object detection' if enabled else ''
                # Interrupt startup, inference and retry delays. The supervisor
                # reaps the old worker before it can launch a replacement.
                self.interrupted.set()
                self.changed.set()
                self._interrupt_process()
        return self.snapshot()[1]

    def _interrupt_process(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    def snapshot(self):
        with self.lock:
            result, error = self.result, self.error
            enabled, running, at = self.enabled, self.process is not None, self.clock()
        live = enabled and result is not None and 0 <= at - result['frame_time'] <= self.max_age and not error
        status = {"available": True, "enabled": enabled, "running": running, "at": at,
                  "live": live, "error": (error or ('' if live else 'Waiting for fresh detection')) if enabled else '',
                  "model": 'SSD MobileNet V2 / COCO', "threshold": 0.5,
                  "objects": result['objects'] if live else [],
                  "frame": result['frame'] if live else None,
                  "inference_ms": result['inference_ms'] if live else None}
        return (result if live else None), status

    def _reply(self, process, timeout, interrupted):
        deadline, data = self.clock() + timeout, bytearray()
        while not self.finished.is_set() and not interrupted.is_set():
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
            with self.lock:
                self.changed.clear()
                if self.finished.is_set():
                    break
                enabled = self.enabled
                interrupted = self.interrupted = threading.Event()
            if not enabled:
                self.changed.wait()
                continue
            process = None
            try:
                with self.lock:
                    if interrupted.is_set() or self.finished.is_set():
                        continue
                    process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                               bufsize=0, start_new_session=True)
                    self.process = process
                # A wedged worker must not trap the supervisor in a pipe write
                # and prevent pause/shutdown from reaching the kill fallback.
                os.set_blocking(process.stdin.fileno(), False)
                if self._reply(process, 15, interrupted) != {"ready": True}:
                    raise ValueError('Invalid Coral startup response')
                previous = None
                while not self.finished.is_set() and not interrupted.is_set():
                    jpeg, timestamp, number, _ = self.frames.latest()
                    if not jpeg or number == previous or self.clock() - timestamp > self.max_age:
                        interrupted.wait(0.05)
                        continue
                    started = self.clock()
                    packet = memoryview(struct.pack('!I', len(jpeg)) + jpeg)
                    while packet and not self.finished.is_set() and not interrupted.is_set():
                        if not select.select([], [process.stdin], [], .1)[1]:
                            continue
                        try:
                            written = os.write(process.stdin.fileno(), packet)
                        except BlockingIOError:
                            continue
                        if not written:
                            raise BrokenPipeError('Coral input closed')
                        packet = packet[written:]
                    answer = self._reply(process, 3, interrupted)
                    if not isinstance(answer.get('objects'), list) or len(answer['objects']) > 10:
                        raise ValueError('Invalid Coral detections')
                    result = {**answer, 'jpeg': jpeg, 'frame_time': timestamp, 'frame': number}
                    with self.lock:
                        if not interrupted.is_set() and self.enabled:
                            self.result, self.error = result, ''
                    previous = number
                    interrupted.wait(max(0, 0.2 - (self.clock() - started)))
            except Exception as exc:
                with self.lock:
                    if not interrupted.is_set() and not self.finished.is_set():
                        self.result, self.error = None, str(exc)
                        print(f'Object detection: {exc}', flush=True)
            finally:
                if process:
                    self._terminate(process)
                with self.lock:
                    self.process = None
            if not interrupted.is_set():
                self.changed.wait(5)

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
        with self.lock:
            self.finished.set()
            self.interrupted.set()
            self.changed.set()
            self._interrupt_process()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
