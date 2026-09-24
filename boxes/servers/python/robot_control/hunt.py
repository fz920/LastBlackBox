"""Spoken/typed clues feed the existing locally bounded search state machine."""
import os
import selectors
import shutil
import subprocess
import threading
import time

from object_search import ObjectSearch
from realtime import TalkError, websocket
from search_intent import SearchIntent, validate_proposal
from speech import mouth_device

MAX_CLUE = 500
MAX_RECORDING = 15


class ClueMicrophone:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None

    def interrupt(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass

    def record(self, device, cancel, released, guard):
        with self.lock:
            if cancel.is_set():
                raise TalkError('Search cancelled.')
            process = self.process = subprocess.Popen(
                ['arecord', '-q', '-D', device, '-t', 'raw', '-f', 'S16_LE',
                 '-r', '24000', '-c', '1', '--buffer-time=100000'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        pcm = bytearray()
        started = time.monotonic()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while not released.is_set() and not cancel.is_set():
                    guard()
                    if time.monotonic() - started >= MAX_RECORDING:
                        raise TalkError('15-second recording limit reached. Try a shorter clue.')
                    if selector.select(timeout=.04):
                        data = os.read(process.stdout.fileno(), 4800)
                        if not data:
                            raise TalkError('Microphone capture failed. Check the NB3 audio device.')
                        pcm.extend(data)
                        if len(pcm) > MAX_RECORDING * 48000:
                            raise TalkError('15-second recording limit reached. Try a shorter clue.')
            guard()
            if cancel.is_set():
                raise TalkError('Search cancelled.')
            if len(pcm) < 9600:
                raise TalkError('Hold the button while speaking, then release to search.')
            return bytes(pcm[:len(pcm) // 2 * 2])
        finally:
            self.interrupt()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            process.stdout.close()
            with self.lock:
                if self.process is process:
                    self.process = None


class Hunt(ObjectSearch):
    def __init__(self, controller, key_provider, speech=None, vision=None, *,
                 audio_lock, device='auto', model='gpt-realtime', mirrored=False):
        super().__init__(controller, key_provider, speech, vision, mirrored=mirrored)
        self.audio_lock, self.device = audio_lock, device
        self.interpreter = SearchIntent(model)
        self.microphone = ClueMicrophone()
        self.released = threading.Event()
        self.prompt = self.capture_device = None
        self.request_text = ''
        self.record_started = 0
        self.audio_owned = False

    def _voice_device(self):
        if not shutil.which('arecord'):
            return None
        return mouth_device() if self.device == 'auto' else self.device

    def status(self):
        with self.lock:
            state = super().status()
            state.update(clues=True, voice_ready=bool(self._voice_device()) and bool(websocket),
                         request_text=self.request_text, max_clue=MAX_CLUE,
                         max_recording=MAX_RECORDING,
                         seconds=round(time.monotonic() - self.record_started, 1)
                         if self.phase == 'recording' else 0)
            if not websocket:
                state.update(ready=False, error='Run setup-realtime.sh on the Pi first.')
            return state

    def start(self, token, target, allow_turns, frame_time, stop_generation):
        if target is not None and (not isinstance(target, str) or not target.strip() or len(target) > MAX_CLUE):
            raise ValueError('Describe what to find in 1–500 characters.')
        with self.lock:
            if self.token:
                raise ValueError('A search is already running or stopping.')
            if not websocket:
                raise ValueError('Run setup-realtime.sh on the Pi first.')
            device = None
            owned = False
            if target is None:
                device = self._voice_device()
                if not device:
                    raise ValueError('Robot microphone unavailable. Check the audio driver or type a clue.')
                owned = self.audio_lock.acquire(blocking=False)
                if not owned:
                    raise ValueError('Audio is busy. End conversation or stop speech before giving a clue.')
            self.prompt = target.strip() if target is not None else None
            self.request_text = self.prompt or ''
            self.released = threading.Event()
            self.capture_device, self.audio_owned = device, owned
            try:
                super().start(token, 'your clue', allow_turns, frame_time, stop_generation)
            except Exception:
                if owned:
                    self.audio_lock.release()
                    self.audio_owned = False
                raise
            self.record_started = time.monotonic()
            self.phase = 'recording' if self.prompt is None else 'interpreting'
            self.message = 'Listening to your clue…' if self.prompt is None else 'Understanding your clue…'
            return self.status()

    def finish_recording(self, token):
        self._validate_token(token)
        with self.lock:
            self._guard()
            if token != self.token or self.prompt is not None or self.phase != 'recording':
                raise ValueError('Recording ended. Hold again for a new clue.')
            self.released.set()
            return self.status()

    def _cancel(self, message):
        super()._cancel(message)
        self.interpreter.close()
        self.microphone.interrupt()

    def _run(self):
        handed_off = False
        try:
            pcm = None
            try:
                if self.prompt is None:
                    pcm = self.microphone.record(self.capture_device, self.cancelled, self.released, self._guard)
            finally:
                with self.lock:
                    if self.audio_owned:
                        self.audio_lock.release()
                        self.audio_owned = False
            with self.lock:
                self._guard()
                self.phase, self.message = 'interpreting', 'Understanding your clue…'
            proposal = validate_proposal(self.interpreter.interpret(self.key_provider(), pcm, self.prompt, self.cancelled))
            with self.lock:
                self._guard()
                self.request_text = proposal['heard'] if self.prompt is None else self.prompt
                if proposal['decision'] == 'clarify':
                    self._finish('clarify', proposal['question'])
                    return
                self.target = proposal['target']
                self.phase, self.message = 'looking', 'Looking for ' + self.target + '…'
            handed_off = True
            super()._run()
        except Exception as exc:
            with self.lock:
                if not self.cancelled.is_set():
                    self.phase = 'error'
                    self.message = str(exc) if isinstance(exc, (TalkError, ValueError)) else 'Could not start the search. Motors stopped.'
        finally:
            if not handed_off:
                with self.lock:
                    self.controller.release_search(self.driver)
                    self.retired.append(self.token)
                    self.token = self.driver = None
