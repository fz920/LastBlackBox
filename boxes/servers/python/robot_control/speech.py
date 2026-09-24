"""Local descriptions and cloud search announcements share one cancellable player."""
from collections import Counter
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

from cloud_speech import CloudSpeech


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
    def __init__(self, detector, engine, device='auto', audio_lock=None):
        self.detector, self.engine, self.device = detector, str(engine), device
        self.audio_lock = audio_lock or threading.Lock()
        self.phase = 'idle'
        self.lock = threading.RLock()
        self.busy = False
        self.text = self.error = ''
        self.process = self.thread = None
        self.cancel = threading.Event()
        self.closed = False
        self.cloud = CloudSpeech()

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
                    'phase': self.phase,
                    'error': config_error or self.error}

    def describe(self):
        with self.lock:
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
            return self._begin(describe_objects(result['objects']), device)

    def say(self, text, *, api_key=None):
        """Speak a short result, using OpenAI when an API key is supplied."""
        with self.lock:
            if self.closed or self.busy:
                raise ValueError('Speech is unavailable or already busy.')
            if not isinstance(text, str) or not 1 <= len(text) <= 400:
                raise ValueError('Speech result must be a short sentence.')
            device, error = self._configuration()
            if error:
                raise ValueError(error)
            return self._begin(text, device, api_key)

    def _begin(self, text, device, api_key=None):
        with self.lock:
            if not self.audio_lock.acquire(blocking=False):
                raise ValueError('Conversation is using the audio device. End it first.')
            self.text = text
            self.phase = 'preparing'
            self.error = ''
            self.cancel = threading.Event()
            self.busy = True
            self.thread = threading.Thread(target=self._describe_and_speak,
                args=(self.text, device, self.cancel, api_key), name='robot-speech', daemon=True)
            self.thread.start()
            return self.status()

    def _describe_and_speak(self, text, device, cancel, api_key=None):
        try:
            if api_key is not None:
                pcm = self.cloud.synthesize(api_key, text, cancel)
                if not cancel.is_set():
                    with self.lock:
                        self.phase = 'speaking'
                    self._command(['aplay', '-q', '-D', device, '-t', 'raw',
                                   '-f', 'S16_LE', '-r', '24000', '-c', '1'], pcm, 22, cancel)
            else:
                with self.lock:
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
                self.audio_lock.release()

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
            self.cloud.close()
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
        return self.status()

    def close(self):
        with self.lock:
            self.closed = True
            thread = self.thread
        self.stop()
        if thread:
            thread.join(timeout=3)
