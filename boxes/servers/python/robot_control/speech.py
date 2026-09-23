"""Speak fresh Coral labels locally, with one cancellable utterance at a time."""
from collections import Counter
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time


def object_phrases(objects):
    counts = Counter(obj['label'] for obj in objects if obj['score'] >= 0.5)
    if not counts:
        return []
    plurals = {'person': 'people', 'mouse': 'mice', 'knife': 'knives',
               'bus': 'buses', 'sandwich': 'sandwiches', 'bench': 'benches', 'couch': 'couches',
               'wine glass': 'wine glasses', 'toothbrush': 'toothbrushes',
               'skis': 'pairs of skis', 'scissors': 'pairs of scissors',
               'sheep': 'sheep'}
    parts = []
    # Detector order is confidence order. Keep descriptions short.
    for label, count in list(counts.items())[:3]:
        if count == 1:
            if label in ('skis', 'scissors'):
                parts.append('a pair of ' + label)
            else:
                parts.append(('an ' if label[0].lower() in 'aeiou' else 'a ') + label)
        else:
            parts.append(f"{count} {plurals.get(label, label + 's')}")
    return parts


def describe_objects(objects):
    parts = object_phrases(objects)
    if not parts:
        return "I'm not sure what I'm looking at yet."
    joined = parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]
    return 'I think I can see ' + joined + '.'


def mouth_device(cards_path='/proc/asound/cards'):
    try:
        cards = Path(cards_path).read_text()
    except OSError:
        return None
    for name in re.findall(r'^\s*\d+\s+\[([^\]]+)\]', cards, re.MULTILINE):
        name = name.strip()
        if 'max98357' in name.lower() or 'nb3' in name.lower():
            return f'plughw:CARD={name},DEV=0'
    return None


class Speech:
    def __init__(self, detector, engine, device='auto', llm=None):
        self.detector, self.engine, self.device = detector, str(engine), device
        self.llm = llm
        self.phase = 'idle'
        self.source = self.detail = ''
        self.generation_seconds = None
        self.lock = threading.RLock()
        self.busy = False
        self.text = self.error = ''
        self.process = self.thread = None
        self.cancel = threading.Event()
        self.closed = False

    def _configuration(self):
        if not os.access(self.engine, os.X_OK):
            return None, 'Speech engine missing. Run setup-speech.sh on the Pi.'
        if not shutil.which('aplay'):
            return None, 'ALSA playback tool aplay is missing.'
        device = mouth_device() if self.device == 'auto' else self.device
        if not device:
            return None, 'NB3 mouth not available. Install its audio driver first.'
        return device, ''

    def status(self):
        with self.lock:
            _, config_error = self._configuration()
            return {'enabled': True, 'ready': not config_error and not self.closed,
                    'speaking': self.busy, 'text': self.text,
                    'llm_enabled': self.llm is not None, 'phase': self.phase,
                    'source': self.source, 'detail': self.detail,
                    'generation_seconds': self.generation_seconds,
                    'error': config_error or self.error}

    def describe(self, use_llm=False):
        with self.lock:
            if type(use_llm) is not bool:
                raise ValueError('Choose whether to use the local LLM')
            if self.closed:
                raise ValueError('Speech is shutting down')
            if self.busy:
                raise ValueError('Already speaking. Wait or press Stop speaking.')
            device, error = self._configuration()
            if error:
                raise ValueError(error)
            result, _ = self.detector.snapshot() if self.detector else (None, None)
            if result is None:
                raise ValueError('Wait for fresh Coral detections before describing the scene.')
            self.text = describe_objects(result['objects'])
            self.source = 'basic'
            self.detail = ''
            self.generation_seconds = None
            self.phase = 'preparing'
            if use_llm and self.llm:
                self.text = ''
            self.error = ''
            self.cancel = threading.Event()
            self.busy = True
            self.thread = threading.Thread(target=self._describe_and_speak,
                args=(result['objects'], device, self.cancel, use_llm), name='robot-speech', daemon=True)
            self.thread.start()
            return self.status()

    def _progress(self, phase):
        with self.lock:
            self.phase = phase

    def _describe_and_speak(self, objects, device, cancel, use_llm):
        try:
            text = describe_objects(objects)
            source, detail = 'basic', ''
            parts = object_phrases(objects)
            if use_llm and self.llm and parts:
                started = time.monotonic()
                try:
                    text = self.llm.generate(parts, cancel, self._progress)
                    source = 'llm'
                except Exception as exc:
                    if cancel.is_set():
                        return
                    source, detail = 'fallback', str(exc)
                with self.lock:
                    self.generation_seconds = round(time.monotonic() - started, 1)
                # Generation takes seconds. Recheck the scene before speaking.
                fresh, _ = self.detector.snapshot()
                if fresh is None:
                    raise ValueError('Video or detections paused. Request a new description when live.')
                if sorted(object_phrases(fresh['objects'])) != sorted(parts):
                    text = describe_objects(fresh['objects'])
                    source, detail = 'fallback', 'Scene changed while the model was thinking.'
            elif use_llm:
                source, detail = 'fallback', ('No objects detected confidently.' if not parts else 'Local LLM is not enabled.')
            if cancel.is_set():
                return
            with self.lock:
                self.text, self.source, self.detail = text, source, detail
                self.phase = 'speaking'
            self._speak(text, device, cancel)
        except Exception as exc:
            if not cancel.is_set():
                with self.lock:
                    self.error = str(exc)
        finally:
            with self.lock:
                self.busy = False
                self.phase = 'cancelled' if cancel.is_set() else 'idle'

    def _command(self, command, data, timeout, cancel):
        with self.lock:
            if cancel.is_set():
                return b''
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.process = process
        try:
            output, error = process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError('Speech timed out; try again.')
        finally:
            with self.lock:
                self.process = None
        if process.returncode and not cancel.is_set():
            raise RuntimeError(error.decode(errors='replace').strip()[-300:] or 'Audio playback failed')
        return output

    def _speak(self, text, device, cancel):
        try:
            wav = self._command([self.engine, '--stdout', '--stdin', '-v', 'en-gb',
                                 '-s', '155', '-a', '65'], text.encode(), 10, cancel)
            if not cancel.is_set():
                self._command(['aplay', '-q', '-D', device, '-t', 'wav'], wav, 20, cancel)
        except Exception as exc:
            if not cancel.is_set():
                with self.lock:
                    self.error = str(exc)

    def stop(self):
        with self.lock:
            self.cancel.set()
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
        if self.llm:
            self.llm.close()
        return self.status()

    def close(self):
        with self.lock:
            self.closed = True
            thread = self.thread
        self.stop()
        if thread:
            thread.join(timeout=3)
