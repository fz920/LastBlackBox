"""Bounded, explicit push-to-talk through NB3 ears and mouth."""
from collections import deque
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import threading
import time

from realtime import Realtime, TalkError, websocket
from speech import mouth_device

MAX_RECORDING = 15
MAX_PROMPT = 500
CONTACT_TIMEOUT = 2.5
IDLE_TIMEOUT = 120
KEY_FILE = Path.home() / '.config/nb3/openai-api-key'


class Talk:
    def __init__(self, frames, audio_lock, key_file=KEY_FILE, model='gpt-realtime', device='auto'):
        self.frames, self.audio_lock = frames, audio_lock
        self.key_file, self.device = Path(key_file), device
        self.client = Realtime(model)
        self.lock = threading.RLock()
        self.thread = self.process = None
        self.cancelled = threading.Event()
        self.released = threading.Event()
        self.token = None
        self.retired = deque(maxlen=64)
        self.phase, self.error, self.text = 'idle', '', ''
        self.started = self.contact = self.activity = time.monotonic()
        self.closed = False

    def _key(self):
        key = os.environ.get('OPENAI_API_KEY', '').strip()
        if not key:
            try:
                key = self.key_file.read_text().strip()
            except (OSError, UnicodeError):
                return ''
        return key if key and not any(c.isspace() for c in key) else ''

    def _configuration(self):
        if not websocket:
            return None, 'Run setup-realtime.sh on the Pi first.'
        if not self._key():
            return None, 'API key not configured yet. Add it on the Pi to enable conversation.'
        if not shutil.which('arecord') or not shutil.which('aplay'):
            return None, 'Install ALSA tools arecord and aplay.'
        device = mouth_device() if self.device == 'auto' else self.device
        if not device:
            return None, 'NB3 audio is unavailable. Check its driver.'
        return device, ''

    def _frame(self):
        jpeg, timestamp, _, _ = self.frames.latest()
        if not jpeg or time.monotonic() - timestamp > 1.2:
            raise TalkError('Wait for live video before asking a question.')
        return jpeg

    def status(self):
        with self.lock:
            _, error = self._configuration()
            return {'enabled': True, 'ready': not error and not self.closed,
                    'at': time.monotonic(),
                    'busy': self.token is not None, 'phase': self.phase,
                    'text': self.text, 'error': error or self.error,
                    'model': self.client.model, 'turns': self.client.turns,
                    'seconds': round(time.monotonic() - self.started, 1) if self.phase == 'recording' else 0,
                    'max_seconds': MAX_RECORDING}

    @staticmethod
    def _validate(token):
        if not isinstance(token, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', token):
            raise ValueError('Invalid conversation token')

    def send_text(self, token, prompt):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
            raise ValueError('Enter a question of 1–500 characters.')
        return self.start(token, prompt.strip())

    def start(self, token, prompt=None):
        self._validate(token)
        with self.lock:
            if self.closed or token in self.retired:
                raise ValueError('This recording was cancelled. Press again to start a new question.')
            if self.token:
                raise ValueError('Conversation is busy. Wait or press End conversation.')
            device, error = self._configuration()
            if error:
                raise ValueError(error)
            try:
                self._frame()
            except TalkError as exc:
                raise ValueError(str(exc)) from None
            if not self.audio_lock.acquire(blocking=False):
                raise ValueError('The robot is speaking. Stop its description first.')
            self.cancelled, self.released = threading.Event(), threading.Event()
            self.started = self.contact = self.activity = time.monotonic()
            self.token, self.phase, self.error, self.text = token, 'recording' if prompt is None else 'thinking', '', ''
            self.thread = threading.Thread(target=self._run, args=(device, prompt), daemon=True, name='robot-talk')
            self.thread.start()
            return self.status()

    def command(self, action, token):
        self._validate(token)
        with self.lock:
            if action == 'cancel':
                # A cancel may arrive before its start request. Reject that late start.
                if token not in self.retired:
                    self.retired.append(token)
                if self.token is None or token == self.token:
                    self._cancel('Conversation ended.')
            elif action in ('finish', 'heartbeat'):
                if token != self.token:
                    raise ValueError('Recording ended. Press again to ask another question.')
                self.contact = time.monotonic()
                if action == 'finish':
                    self.released.set()
            else:
                raise ValueError('Unknown conversation action')
            return self.status()

    def _cancel(self, message):
        self.cancelled.set()
        self.error = message
        self.client.close()
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        self.phase = 'cancelling' if self.token else 'idle'

    def tick(self):
        with self.lock:
            now = time.monotonic()
            if self.token and not self.cancelled.is_set():
                if now - self.contact > CONTACT_TIMEOUT:
                    self._cancel('Connection lost. Question cancelled.')
                elif self.phase == 'recording' and now - self.started >= MAX_RECORDING:
                    self._cancel('15-second recording limit reached. Hold again for a shorter question.')
                elif now - self.started > MAX_RECORDING + 60:
                    self._cancel('Conversation timed out. Please try again.')
            elif not self.token and now - self.activity > IDLE_TIMEOUT:
                self.client.close()

    def _spawn(self, command, recording=False):
        with self.lock:
            if self.cancelled.is_set():
                raise TalkError('Conversation cancelled.')
            self.process = subprocess.Popen(command,
                stdin=subprocess.DEVNULL if recording else subprocess.PIPE,
                stdout=subprocess.PIPE if recording else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            return self.process

    def _end_process(self):
        with self.lock:
            process, self.process = self.process, None
        if process:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            for pipe in (process.stdin, process.stdout):
                if pipe:
                    try:
                        pipe.close()
                    except OSError:
                        pass

    def _record(self, device):
        process = self._spawn(['arecord', '-q', '-D', device, '-t', 'raw', '-f', 'S16_LE',
                               '-r', '24000', '-c', '1', '--buffer-time=100000'], recording=True)
        pcm = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while not self.released.is_set() and not self.cancelled.is_set():
                    self.tick()
                    if selector.select(timeout=.05):
                        data = os.read(process.stdout.fileno(), 4800)
                        if not data:
                            raise TalkError('Microphone capture failed. Check the NB3 audio device.')
                        pcm.extend(data)
                        if len(pcm) > MAX_RECORDING * 48000:
                            raise TalkError('Recording limit reached. Ask a shorter question.')
            if self.cancelled.is_set():
                raise TalkError('Conversation cancelled.')
            if len(pcm) < 9600:
                raise TalkError('Hold the button while speaking, then release to send.')
            return bytes(pcm[:len(pcm) // 2 * 2])
        finally:
            self._end_process()

    def _run(self, device, prompt=None):
        try:
            pcm = self._record(device) if prompt is None else None
            jpeg = self._frame()
            with self.lock:
                if self.cancelled.is_set():
                    return
                self.phase = 'thinking'
            options = {'prompt': prompt} if prompt is not None else {}
            self.client.respond(self._key(), pcm, jpeg, self.cancelled,
                                lambda data: self._audio(data, device), self._transcript, **options)
            with self.lock:
                process = self.process
            if process and not self.cancelled.is_set():
                process.stdin.close()
                process.wait(timeout=5)
                if process.returncode:
                    raise TalkError('Speaker playback failed. Check the NB3 audio device.')
        except Exception as exc:
            with self.lock:
                if not self.cancelled.is_set():
                    self.error = str(exc) if isinstance(exc, TalkError) else 'Robot audio failed. Check its audio device and try again.'
            self.client.close()
        finally:
            self._end_process()
            with self.lock:
                self.retired.append(self.token)
                self.token, self.phase = None, 'idle'
                self.activity = time.monotonic()
                self.audio_lock.release()

    def _audio(self, data, device):
        if self.cancelled.is_set():
            raise TalkError('Conversation cancelled.')
        if self.process is None:
            self._spawn(['aplay', '-q', '-D', device, '-t', 'raw', '-f', 'S16_LE', '-r', '24000', '-c', '1'])
            with self.lock:
                self.phase = 'speaking'
        for offset in range(0, len(data), 24000):
            if self.cancelled.is_set():
                raise TalkError('Conversation cancelled.')
            self.process.stdin.write(data[offset:offset + 24000])
            self.process.stdin.flush()

    def _transcript(self, text, replace):
        with self.lock:
            self.text = (text if replace else self.text + text)[:4000]

    def close(self):
        with self.lock:
            self.closed = True
            self._cancel('Conversation ended.')
        if self.thread:
            self.thread.join(timeout=6)
