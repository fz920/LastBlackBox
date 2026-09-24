"""Look, turn briefly, stop, look again. All movement limits are enforced locally."""
from collections import deque
import re
import threading
import time

from search_vision import SearchError, SearchVision, validate_result

CONTACT_TIMEOUT = 1.5
SETTLE_SECONDS = 0.35
MAX_CHECKS = 20


def found_sentence(target, location, description):
    """Use only the verified evidence, with one sentence sent to speech."""
    def fragment(text, limit):
        text = re.split(r'(?<!\d)\.|\.(?!\d)|[!?。！？\n]+', text, maxsplit=1)[0]
        return ' '.join(text.split())[:limit].strip(' ,:;—-')
    target = fragment(target, 100) or 'the requested object'
    evidence = fragment(description, 180)
    detail = f' — {evidence}' if evidence else ''
    return f'I think I found {target}, {location}{detail}.'


class ObjectSearch:
    def __init__(self, controller, key_provider, speech=None, vision=None, *, mirrored=False):
        self.controller = controller
        # Share the motor lock: STOP and a decision to turn cannot pass each other.
        self.lock = controller.lock
        self.key_provider = key_provider
        self.speech = speech
        self.vision = vision or SearchVision()
        self.mirrored = mirrored
        self.token = self.driver = None
        self.retired = deque(maxlen=64)
        self.cancelled = threading.Event()
        self.thread = None
        self.closed = False
        self.phase = 'idle'
        self.target = self.message = self.evidence = ''
        self.checks = self.turns = 0
        self.center_turns = 0
        self.centered = False
        self.centering_note = ''
        self.contact = 0
        self.matched_jpeg = None
        self.result_id = 0
        self.announcement = None

    def status(self):
        with self.lock:
            key_ready = bool(self.key_provider())
            return {'enabled': True, 'ready': key_ready and not self.closed,
                    'at': time.monotonic(), 'busy': self.token is not None,
                    'phase': self.phase, 'target': self.target, 'message': self.message,
                    'evidence': self.evidence, 'checks': self.checks, 'turns': self.turns,
                    'max_turns': self.controller.SEARCH_MAX_TURNS, 'max_checks': MAX_CHECKS,
                    'turn_seconds': self.controller.SEARCH_TURN_SECONDS,
                    'center_turns': self.center_turns, 'max_center_turns': self.controller.CENTER_MAX_TURNS,
                    'center_turn_seconds': self.controller.CENTER_TURN_SECONDS,
                    'centered': self.centered, 'centering_note': self.centering_note,
                    'preview': self.controller.serial is None,
                    'result_id': self.result_id if self.matched_jpeg else None,
                    'announcement_error': self.speech.error if self.speech and
                        self.announcement is self.speech.cancel else '',
                    'error': '' if key_ready else 'Add the API key on the Pi to enable search.'}

    @staticmethod
    def _validate_token(token):
        if not isinstance(token, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', token):
            raise ValueError('Invalid search token')

    def _browser_frame(self, timestamp):
        if type(timestamp) not in (int, float) or not 0 <= time.monotonic() - timestamp <= 1.2:
            raise ValueError('Wait for live video before searching.')

    def start(self, token, target, allow_turns, frame_time, stop_generation):
        self._validate_token(token)
        if not isinstance(target, str) or not 1 <= len(target.strip()) <= 100:
            raise ValueError('Describe the object in 1–100 characters.')
        if allow_turns is not True:
            raise ValueError('Enable short turns for this search first.')
        with self.lock:
            if self.closed or token in self.retired:
                raise ValueError('This search was cancelled. Start a new search.')
            if self.token:
                raise ValueError('A search is already running or stopping.')
            if type(stop_generation) is not int or stop_generation != self.controller.stop_generation:
                raise ValueError('Stop was pressed. Wait for the page to update before starting again.')
            self._browser_frame(frame_time)
            self._fresh()
            if not self.key_provider():
                raise ValueError('Add the API key on the Pi to enable search.')
            self.driver = self.controller.claim('slow', kind='search')
            self.token, self.target = token, target.strip()
            self.cancelled = threading.Event()
            self.contact = time.monotonic()
            self.phase, self.message, self.evidence = 'looking', 'Checking the current view…', ''
            self.turns = self.checks = 0
            self.center_turns = 0
            self.centered = False
            self.centering_note = ''
            self.matched_jpeg = None
            self.thread = threading.Thread(target=self._run, name='robot-search', daemon=True)
            self.thread.start()
            return self.status()

    def heartbeat(self, token, frame_time):
        self._validate_token(token)
        with self.lock:
            self.tick()
            if token != self.token or self.cancelled.is_set():
                raise ValueError('Search ended. Start a new search when ready.')
            try:
                self._browser_frame(frame_time)
            except ValueError:
                self._cancel('Video paused in the browser. Search stopped.')
                raise
            self.contact = time.monotonic()
            return self.status()

    def cancel(self, token=None, message='Search stopped.'):
        if token is not None:
            self._validate_token(token)
        with self.lock:
            if token is not None and token not in self.retired:
                self.retired.append(token)
            if token is None or token == self.token or self.token is None:
                self._cancel(message)
            return self.status()

    def _cancel(self, message):
        self.cancelled.set()
        self.controller.release_search(self.driver, message)
        if self.token:
            self.phase, self.message = 'cancelled', message
        self.vision.close()
        if self.speech and self.announcement:
            with self.speech.lock:
                if self.speech.cancel is self.announcement:
                    self.speech.stop()

    def _fresh(self):
        jpeg, timestamp, number, _ = self.controller.frames.latest()
        if not jpeg or time.monotonic() - timestamp > 1.2:
            raise ValueError('Camera paused. Search stopped.')
        return jpeg, timestamp, number

    def tick(self):
        with self.lock:
            if not self.token or self.cancelled.is_set() or self.phase in ('found', 'not_found', 'error', 'clarify'):
                return
            try:
                if time.monotonic() - self.contact > CONTACT_TIMEOUT:
                    raise ValueError('Browser disconnected. Search stopped.')
                self._fresh()
                self.controller.search_contact(self.driver)
            except ValueError as exc:
                self._cancel(str(exc))

    def _guard(self):
        self.tick()
        if self.cancelled.is_set():
            raise SearchError('Search cancelled.')

    def _wait(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self._guard()
            self.cancelled.wait(min(.04, max(0, deadline - time.monotonic())))
        self._guard()

    def _look(self, after, phase='looking'):
        # Require a new image taken after the motors have stopped and settled.
        deadline = time.monotonic() + 1.3
        while True:
            with self.lock:
                self._guard()
                jpeg, timestamp, _ = self._fresh()
                if timestamp > after:
                    break
            if time.monotonic() >= deadline:
                raise SearchError('No new camera image arrived. Search stopped.')
            self._wait(.04)
        with self.lock:
            if self.checks >= MAX_CHECKS:
                raise SearchError('Image check limit reached. I could not confirm a match from here.')
            self.checks += 1
            self.phase = phase
            self.message = {'verifying': 'Checking a second image…',
                            'centering': 'Checking the object’s position…',
                            'confirming_center': 'Confirming the object is near the centre…'}.get(
                                phase, 'Looking for ' + self.target + '…')
            key = self.key_provider()
        result = validate_result(self.vision.inspect(key, self.target, jpeg, self.cancelled))
        with self.lock:
            self._guard()
            self.evidence = result['description']
        return result, jpeg

    def _finish(self, phase, message, jpeg=None, *, centered=False, centering_note=''):
        with self.lock:
            self._guard()
            self.controller.release_search(self.driver)
            self.phase, self.message = phase, message
            self.centered, self.centering_note = centered, centering_note
            if jpeg:
                self.matched_jpeg = jpeg
                self.result_id += 1
            if self.speech:
                try:
                    with self.speech.lock:
                        if phase in ('found', 'clarify'):
                            self.speech.say(message, api_key=self.key_provider())
                        else:
                            self.speech.say(message)
                        self.announcement = self.speech.cancel
                except ValueError:
                    # Results remain visible if someone is already using the speaker.
                    pass

    def _turn_and_settle(self, direction='right', *, centering=False):
        with self.lock:
            self._guard()
            duration = self.controller.search_turn(self.driver, direction, centering=centering)
            if centering:
                self.center_turns += 1
            else:
                self.turns += 1
            self.phase = 'centering' if centering else 'turning'
            self.message = ('Centring: turning ' if centering else 'Turning ') + direction + ' briefly…'
        self._wait(duration + .06)
        with self.lock:
            self.controller.tick()
            self._guard()
            if self.controller.command != 'stop':
                raise SearchError('Turn did not stop as expected. Search stopped.')
            self.phase, self.message = 'settling', 'Stopped. Waiting for a clear image…'
        self._wait(SETTLE_SECONDS)
        return time.monotonic()

    def _finish_match(self, result, jpeg, *, centered=False, note=''):
        location = {'left': 'on the left', 'centre': 'near the centre',
                    'right': 'on the right', 'unknown': 'in this view'}[result['location']]
        self._finish('found', found_sentence(self.target, location, result['description']), jpeg,
                     centered=centered, centering_note=note)

    def _center(self, first, result, jpeg):
        # Two centred images at the same pose are needed; a turn resets the count.
        centre_streak = int(first['location'] == 'centre')
        while True:
            self._guard()
            if result['decision'] != 'match':
                self._finish('not_found', 'I lost a clear view of the object while centring, so I stopped.',
                             centering_note='Target lost or uncertain; no further turns.')
                return
            location = result['location']
            centre_streak = centre_streak + 1 if location == 'centre' else 0
            if centre_streak >= 2:
                self._finish_match(result, jpeg, centered=True, note='Near the centre in two stationary images.')
                return
            if location == 'unknown':
                self._finish_match(result, jpeg, note='Found, but its position is unclear; stopped without centring.')
                return
            if self.checks >= MAX_CHECKS:
                self._finish_match(result, jpeg, note='Found, but the image-check limit prevented confirming centring.')
                return
            if location == 'centre':
                self._wait(SETTLE_SECONDS)
                after = time.monotonic()
                phase = 'confirming_center'
            else:
                if self.center_turns >= self.controller.CENTER_MAX_TURNS:
                    self._finish_match(result, jpeg, note='Found, but the centring turn limit was reached; stopped.')
                    return
                direction = {'left': 'right', 'right': 'left'}[location] if self.mirrored else location
                after = self._turn_and_settle(direction, centering=True)
                centre_streak = 0
                phase = 'centering'
            result, jpeg = self._look(after, phase)

    def _run(self):
        try:
            self._wait(SETTLE_SECONDS)
            after = time.monotonic()
            while True:
                result, _ = self._look(after)
                if result['decision'] == 'match' and self.checks < MAX_CHECKS:
                    self._wait(SETTLE_SECONDS)
                    verified, jpeg = self._look(time.monotonic(), 'verifying')
                    if verified['decision'] == 'match':
                        self._center(result, verified, jpeg)
                        return
                if self.turns >= self.controller.SEARCH_MAX_TURNS or self.checks >= MAX_CHECKS:
                    self._finish('not_found', f'I could not confirm {self.target} from here. The search limit is reached.')
                    return
                after = self._turn_and_settle()
        except Exception as exc:
            with self.lock:
                if not self.cancelled.is_set():
                    self.phase = 'error'
                    self.message = str(exc) if isinstance(exc, (SearchError, ValueError)) else 'Search failed. Motors stopped.'
        finally:
            with self.lock:
                self.controller.release_search(self.driver)
                self.retired.append(self.token)
                self.token = self.driver = None

    def close(self):
        with self.lock:
            self.closed = True
            self._cancel('Search closed.')
        if self.thread:
            self.thread.join(timeout=2)
