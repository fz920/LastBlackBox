"""Look, execute a bounded plan, stop, look again. All movement limits are enforced locally."""
from collections import deque
import re
import threading
import time

from live_search import LiveSearchVision, validate_live_settings
from search_memory import SearchMemory
from search_vision import SearchError, SearchVision, validate_result

CONTACT_TIMEOUT = 1.5
SETTLE_SECONDS = 0.35
MAX_CHECKS = 20
MAX_DECISION_AGE = 8


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
        self.explore = False
        self.speed = 'slow'
        self.steps = 0
        self.max_turns = controller.SEARCH_MAX_TURNS
        self.max_steps = controller.SEARCH_MAX_STEPS
        self.sequence_length = 3
        self.plan = []
        self.plan_index = 0
        self.decision_time = 0
        self.history = deque(maxlen=8)
        self.memory = SearchMemory()
        self.action = self.action_reason = ''
        self.live = False
        self.image_rate = 2
        self.live_client = None
        self.live_factory = LiveSearchVision
        self.live_interrupted = False
        self.plan_epoch = 0

    def status(self):
        with self.lock:
            key_ready = bool(self.key_provider())
            return {'enabled': True, 'ready': key_ready and not self.closed,
                    'at': time.monotonic(), 'busy': self.token is not None,
                    'phase': self.phase, 'target': self.target, 'message': self.message,
                    'evidence': self.evidence, 'checks': self.checks, 'turns': self.turns,
                    'live': self.live, 'image_rate': self.image_rate,
                    'live_status': self.live_client.status() if self.live_client else None,
                    'memory': self.memory.status(),
                    'adaptive': True, 'explore': self.explore, 'speed': self.speed, 'steps': self.steps,
                    'max_steps': self.max_steps, 'sequence_length': self.sequence_length,
                    'plan': list(self.plan), 'plan_index': self.plan_index,
                    'turn_cap': self.controller.SEARCH_TURN_CAP, 'step_cap': self.controller.SEARCH_STEP_CAP,
                    'action': self.action, 'action_reason': self.action_reason,
                    'max_turns': self.max_turns, 'max_checks': MAX_CHECKS,
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

    def start(self, token, target, allow_turns, frame_time, stop_generation, *, explore=False, speed='slow',
              max_turns=None, max_steps=None, sequence_length=3, live=False, image_rate=2):
        self._validate_token(token)
        if not isinstance(target, str) or not 1 <= len(target.strip()) <= 100:
            raise ValueError('Describe the object in 1–100 characters.')
        if allow_turns is not True:
            raise ValueError('Enable short turns for this search first.')
        if type(explore) is not bool:
            raise ValueError('Explore must be true or false.')
        if speed not in ('slow', 'full'):
            raise ValueError('Choose calibrated or full search speed.')
        validate_live_settings(live, image_rate)
        limits = self.controller.search_limits(max_turns, max_steps, sequence_length)
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
            self.driver = self.controller.claim(speed, kind='search', explore=explore,
                                                max_turns=limits[0], max_steps=limits[1])
            self.max_turns, self.max_steps, self.sequence_length = limits
            self.plan, self.plan_index = [], 0
            self.live, self.image_rate = live, image_rate
            self.live_client = None
            self.live_interrupted = False
            self.plan_epoch = 0
            self.speed = speed
            self.token, self.target = token, target.strip()
            self.cancelled = threading.Event()
            self.contact = time.monotonic()
            self.phase, self.message, self.evidence = 'looking', 'Checking the current view…', ''
            self.turns = self.checks = 0
            self.center_turns = 0
            self.centered = False
            self.centering_note = ''
            self.matched_jpeg = None
            self.explore, self.steps = explore, 0
            self.history.clear()
            self.memory.clear()
            self.action = self.action_reason = ''
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

    def clear_memory(self):
        with self.lock:
            if self.token:
                raise ValueError('Stop the search before clearing its memory.')
            self.memory.clear()
            self.history.clear()
            return self.status()

    def _memory_context(self, jpeg):
        with self.lock:
            return {'search_memory': self.memory.context(jpeg)}

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
        if self.live_client:
            self.live_client.close()
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

    def _live_frame(self, driver):
        with self.lock:
            if (self.driver != driver or self.cancelled.is_set() or
                    self.controller.owner != driver or self.phase in ('found', 'not_found', 'error', 'clarify')):
                return None
            self._guard()
            jpeg, timestamp, number = self._fresh()
            return {'jpeg': jpeg, 'timestamp': timestamp, 'number': number,
                    'target': self.target, 'phase': self.phase, 'epoch': self.plan_epoch,
                    'moving': self.controller.command != 'stop',
                    'monitor': self.phase in ('moving', 'settling') and not self.live_interrupted}

    def _live_observation(self, driver, result, frame):
        with self.lock:
            # Late observations cannot affect another plan or a newly claimed driver.
            if (self.driver != driver or self.controller.owner != driver or self.cancelled.is_set() or
                    frame['epoch'] != self.plan_epoch or self.phase not in ('moving', 'settling') or
                    not 0 <= time.monotonic() - frame['timestamp'] <= 2):
                return
            if result['actions'] == ['stop']:
                self._cancel('Live view requested a stop: ' + result['reason'])
            elif result['decision'] in ('match', 'uncertain'):
                self.controller.pause_search(driver)
                self.live_interrupted = True
                self.evidence = result['description']
                self.message = 'Live view changed. Stopped to check a fresh stationary image…'

    def _guard(self):
        self.tick()
        if self.cancelled.is_set():
            raise SearchError('Search cancelled.')
        if self.live_client and self.live_client.status()['error']:
            raise SearchError(self.live_client.status()['error'])

    def _wait(self, duration, *, interruptible=False):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self._guard()
            if interruptible and self.live_interrupted:
                return
            self.cancelled.wait(min(.04, max(0, deadline - time.monotonic())))
        self._guard()

    def _look(self, after, phase='looking'):
        with self.lock:
            self.plan_epoch += 1
            self.live_interrupted = False
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
            context = {'allowed_actions': self.controller.search_actions(self.driver)
                       if phase in ('looking', 'verifying') else ['inspect', 'stop'],
                       'history': list(self.history), 'phase': phase, 'explore': self.explore,
                       'movement_speed': self.speed, 'centring_speed': 'calibrated',
                       'remaining_checks': MAX_CHECKS - self.checks,
                       'max_sequence_length': self.sequence_length,
                       'remaining_turns': self.max_turns - self.turns,
                       'remaining_steps': self.max_steps - self.steps if self.explore else 0}
            if self.live and self.live_client is None:
                driver = self.driver
                self.live_client = self.live_factory(
                    lambda: self._live_frame(driver),
                    lambda result, frame: self._live_observation(driver, result, frame),
                    image_rate=self.image_rate, context_provider=self._memory_context)
        if self.live:
            result, jpeg, timestamp = self.live_client.inspect_frame(
                key, self.target, self.cancelled, context={**context, 'after': after})
            result = validate_result(result)
        else:
            context.update(self._memory_context(jpeg))
            result = validate_result(self.vision.inspect(key, self.target, jpeg, self.cancelled, context=context))
        with self.lock:
            self._guard()
            if time.monotonic() - timestamp > MAX_DECISION_AGE:
                raise SearchError('The visual decision arrived too late. Search stopped; try again.')
            self.evidence = result['description']
            self.plan, self.plan_index = list(result['actions']), 0
            self.decision_time = timestamp
            self.action, self.action_reason = self.plan[0], result['reason']
            self.memory.observe(jpeg, timestamp, result, phase)
            self.history.append({'observation': self.evidence, 'decision': result['decision'],
                                 'proposed_actions': list(self.plan)})
        return result, jpeg

    def _finish(self, phase, message, jpeg=None, *, centered=False, centering_note=''):
        with self.lock:
            self._guard()
            self.controller.release_search(self.driver)
            if self.live_client:
                self.live_client.close()
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

    def _move_and_settle(self, direction, *, centering=False):
        with self.lock:
            self._guard()
            if self.live_interrupted and not centering:
                return time.monotonic()
            physical = {'left': 'right', 'right': 'left'}.get(direction, direction) if self.mirrored else direction
            if direction in ('forward', 'backward'):
                duration = self.controller.search_step(self.driver, direction)
                self.steps += 1
            else:
                duration = self.controller.search_turn(self.driver, physical, centering=centering)
            if centering:
                self.plan, self.plan_index = [direction], 1
                self.center_turns += 1
            elif direction in ('left', 'right'):
                self.turns += 1
            self.action = direction
            self.phase = 'centering' if centering else 'moving'
            self.message = ('Centring: ' if centering else 'Exploring: ') + physical + ' briefly…'
            movement_started = time.monotonic()
        try:
            self._wait(duration + .06, interruptible=not centering)
        except Exception:
            with self.lock:
                self.memory.action(direction, min(duration, time.monotonic() - movement_started), interrupted=True)
            raise
        with self.lock:
            self.controller.tick()
            self._guard()
            if self.controller.command != 'stop':
                raise SearchError('Movement did not stop as expected. Search stopped.')
            self.history.append({('interrupted_action' if self.live_interrupted else 'completed_action'): direction,
                                 'motor_direction': physical,
                                 'duration_seconds': min(duration, time.monotonic() - movement_started)
                                 if self.live_interrupted else duration})
            self.memory.action(direction, min(duration, time.monotonic() - movement_started)
                               if self.live_interrupted else duration, interrupted=self.live_interrupted)
            self.phase, self.message = 'settling', 'Stopped. Settling after movement…'
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
            if result['actions'] == ['stop']:
                self._finish_match(result, jpeg, note='Centring stopped: ' + result['reason'])
                return
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
                self.action_reason = 'The confirmed target is toward the ' + location + ' of the image.'
                after = self._move_and_settle(location, centering=True)
                centre_streak = 0
                phase = 'centering'
            result, jpeg = self._look(after, phase)

    def _validate_plan(self, actions):
        # Validate the entire plan before any motor command, simulating budgets and retreat.
        with self.lock:
            self._guard()
            allowed = self.controller.search_actions(self.driver)
            turns, steps = self.controller.search_turns, self.controller.search_steps
            retreat = 'backward' in allowed
            if len(actions) > self.sequence_length:
                raise SearchError('The proposed sequence exceeds Moves per image. Search stopped.')
            for action in actions:
                if action in ('left', 'right'):
                    turns += 1
                    retreat = False
                elif action in ('forward', 'backward'):
                    if not self.explore or (action == 'backward' and not retreat):
                        raise SearchError('The proposed exploration sequence is not permitted. Search stopped.')
                    steps += 1
                    retreat = action == 'forward'
                if turns > self.max_turns or steps > self.max_steps:
                    raise SearchError('The proposed sequence exceeds the remaining search limits. Search stopped.')

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
                    result = verified
                if result['actions'] == ['stop']:
                    self._finish('not_found', 'Search stopped: ' + result['reason'])
                    return
                if self.checks >= MAX_CHECKS or (self.turns >= self.max_turns and
                        (not self.explore or self.steps >= self.max_steps)):
                    self._finish('not_found', f'I could not confirm {self.target} from here. The search limit is reached.')
                    return
                self._validate_plan(result['actions'])
                for index, action in enumerate(result['actions'], 1):
                    with self.lock:
                        self._guard()
                        if self.live_interrupted:
                            break
                        if time.monotonic() - self.decision_time > MAX_DECISION_AGE:
                            raise SearchError('The movement plan expired. Search stopped; try again.')
                        if action not in self.controller.search_actions(self.driver):
                            raise SearchError('The proposed action is not currently permitted. Search stopped.')
                        self.plan_index, self.action = index, action
                    if action == 'stop':
                        self._finish('not_found', 'Search stopped: ' + result['reason'])
                        return
                    if action == 'inspect':
                        self._wait(SETTLE_SECONDS)
                        after = time.monotonic()
                    else:
                        after = self._move_and_settle(action)
                    if self.live_interrupted:
                        # Drop the unexecuted suffix; the next stationary look must replan.
                        break
        except Exception as exc:
            with self.lock:
                if not self.cancelled.is_set():
                    self.phase = 'error'
                    self.message = str(exc) if isinstance(exc, (SearchError, ValueError)) else 'Search failed. Motors stopped.'
        finally:
            with self.lock:
                self.controller.release_search(self.driver)
                if self.live_client:
                    self.live_client.close()
                self.retired.append(self.token)
                self.token = self.driver = None

    def close(self):
        with self.lock:
            self.closed = True
            self._cancel('Search closed.')
        if self.thread:
            self.thread.join(timeout=2)
