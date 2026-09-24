"""Autonomous search regression tests. All wheels and API responses are simulated."""
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from object_search import ObjectSearch
from search_vision import SearchError, SearchVision, validate_result
from server import Controller, Frames, Handler, slow_command
from test_server import FakeSerial

TOKEN = 'search-test-token-123456'


def result(decision='absent', location=None):
    return {'decision': decision, 'location': location or ('centre' if decision == 'match' else 'unknown'),
            'actions': ['right' if decision == 'absent' else 'inspect'], 'reason': 'Check the next useful view.',
            'description': 'A bottle is visible.' if decision == 'match' else 'No bottle visible.'}


class SearchMotorTests(unittest.TestCase):
    def setUp(self):
        self.now = time.monotonic()
        self.frames = Frames()
        self.frames.write(b'jpeg')
        self.frames.timestamp = self.now
        self.serial = FakeSerial()
        self.robot = Controller(self.frames, self.serial, clock=lambda: self.now)
        self.token = self.robot.claim(kind='search')

    def test_custom_limits_enforced_at_motor_layer_and_reset_next_claim(self):
        self.robot.release_search(self.token)
        self.token = self.robot.claim(kind='search', explore=True, max_turns=1, max_steps=1)
        self.robot.search_turn(self.token, 'left')
        self.now += .31; self.frames.timestamp = self.now; self.robot.tick()
        self.assertNotIn('left', self.robot.search_actions(self.token))
        with self.assertRaises(ValueError): self.robot.search_turn(self.token)
        self.robot.search_step(self.token, 'forward')
        self.now += .41; self.frames.timestamp = self.now; self.robot.tick()
        with self.assertRaises(ValueError): self.robot.search_step(self.token, 'forward')
        with self.assertRaises(ValueError): self.robot.search_step(self.token, 'backward')
        self.robot.release_search(self.token)
        self.robot.claim(kind='search')
        self.assertEqual(self.robot.search_turn_limit, 8)
        self.assertEqual(self.robot.search_step_limit, 4)

    def test_fixed_slow_turn_deadline_stops_even_without_worker(self):
        self.robot.search_turn(self.token)
        self.assertEqual(self.serial.commands[-1], slow_command('right', self.robot.calibration))
        self.assertEqual(self.robot.speed, 'slow')
        self.now += .31
        self.robot.tick()
        self.assertEqual(self.serial.commands[-1], b'x')
        self.assertEqual(self.robot.command, 'stop')
        self.assertEqual(self.robot.owner, self.token)

    def test_browser_lease_and_total_deadline_release_motors(self):
        self.now += 2.1
        self.robot.tick()
        self.assertIsNone(self.robot.owner)
        token = self.robot.claim(kind='search')
        self.now += self.robot.SEARCH_MAX_SECONDS + .1
        with self.assertRaises(ValueError):
            self.robot.search_contact(token)
        self.assertIsNone(self.robot.owner)

    def test_manual_commands_cannot_use_search_lease(self):
        with self.assertRaises(ValueError):
            self.robot.claim()
        with self.assertRaisesRegex(ValueError, 'Search owns'):
            self.robot.control(self.token, 1, 'forward', self.now)
        self.assertEqual(self.serial.commands, [b'x'])

    def test_eight_turn_limit_and_stop_revocation(self):
        for _ in range(8):
            self.frames.timestamp = self.now
            self.robot.search_turn(self.token)
            self.now += .31
            self.robot.tick()
        with self.assertRaisesRegex(ValueError, 'limit'):
            self.robot.search_turn(self.token)
        self.robot.stop_all()
        with self.assertRaises(ValueError):
            self.robot.search_turn(self.token)
        manual = self.robot.claim()
        self.robot.release_search(self.token)
        self.assertEqual(self.robot.owner, manual)

    def test_centring_has_shorter_deadline_and_independent_six_turn_cap(self):
        for i in range(6):
            self.frames.timestamp = self.now
            direction = 'left' if i % 2 else 'right'
            duration = self.robot.search_turn(self.token, direction, centering=True)
            self.assertEqual(duration, .12)
            self.assertEqual(self.serial.commands[-1], slow_command(direction, self.robot.calibration))
            self.now += .13
            self.robot.tick()
            self.assertEqual(self.robot.command, 'stop')
        with self.assertRaisesRegex(ValueError, 'limit'):
            self.robot.search_turn(self.token, 'left', centering=True)
        self.assertEqual(self.robot.search_turns, 0)

    def test_centring_rejects_forward_backward_and_invalid_flags(self):
        for direction, centering in [('forward', True), ('backward', True), ('up', False), ('right', 'true')]:
            with self.assertRaisesRegex(ValueError, 'Invalid'):
                self.robot.search_turn(self.token, direction, centering=centering)
        self.assertEqual(self.serial.commands, [b'x'])

    def explore(self):
        self.robot.stop_all()
        self.token = self.robot.claim(kind='search', explore=True)

    def complete_step(self, direction):
        self.frames.timestamp = self.now
        duration = self.robot.search_step(self.token, direction)
        self.assertEqual(self.serial.commands[-1], slow_command(direction, self.robot.calibration))
        self.now += duration + .01
        self.robot.tick()
        self.assertEqual(self.robot.command, 'stop')
        return duration

    def test_forward_requires_explore_and_retreat_requires_completed_forward(self):
        with self.assertRaises(ValueError): self.robot.search_step(self.token, 'forward')
        self.explore()
        with self.assertRaises(ValueError): self.robot.search_step(self.token, 'backward')
        duration = self.robot.search_step(self.token, 'forward')
        self.assertEqual(duration, .4)
        with self.assertRaises(ValueError): self.robot.search_step(self.token, 'backward')
        self.now += .41; self.robot.tick()
        self.assertEqual(self.complete_step('backward'), .3)
        with self.assertRaises(ValueError): self.robot.search_step(self.token, 'backward')

    def test_turn_or_elapsed_time_invalidates_retreat(self):
        for mode in ('turn', 'time'):
            with self.subTest(mode=mode):
                self.explore(); self.complete_step('forward')
                self.assertIn('backward', self.robot.search_actions(self.token))
                if mode == 'turn':
                    self.robot.search_turn(self.token, 'left')
                    self.now += .31; self.robot.tick()
                else:
                    self.now += 10.1
                    self.robot.last_contact = self.now
                self.frames.timestamp = self.now
                with self.assertRaises(ValueError): self.robot.search_step(self.token, 'backward')

    def test_step_budget_and_stop_cannot_be_bypassed_by_planner(self):
        self.explore()
        for _ in range(4): self.complete_step('forward')
        with self.assertRaises(ValueError): self.robot.search_step(self.token, 'forward')
        self.assertNotIn('forward', self.robot.search_actions(self.token))
        self.robot.stop_all()
        manual = self.robot.claim()
        with self.assertRaises(ValueError): self.robot.search_step(self.token, 'forward')
        self.robot.release_search(self.token)
        self.assertEqual(self.robot.owner, manual)

    def test_full_search_speed_uses_firmware_commands_and_same_short_deadlines(self):
        self.robot.stop_all()
        self.token = self.robot.claim('full', kind='search', explore=True)
        for direction, expected in [('left', b'R'), ('right', b'L'), ('forward', b'B'), ('backward', b'F')]:
            self.frames.timestamp = self.now
            if direction in ('left', 'right'):
                duration = self.robot.search_turn(self.token, direction)
                self.assertEqual(duration, .3)
            else:
                duration = self.robot.search_step(self.token, direction)
                self.assertEqual(duration, .4 if direction == 'forward' else .3)
            self.assertEqual(self.serial.commands[-1], expected)
            self.now += duration + .01; self.robot.tick()
            self.assertEqual(self.serial.commands[-1], b'x')
        self.frames.timestamp = self.now
        self.robot.search_turn(self.token, 'left', centering=True)
        self.assertEqual(self.serial.commands[-1], slow_command('left', self.robot.calibration))
        self.now += .13; self.robot.tick()
        self.robot.search_turn(self.token, 'right')
        self.assertEqual(self.serial.commands[-1], b'L')
        self.robot.stop_all()
        self.assertEqual(self.robot.search_speed, 'slow')
        with self.assertRaises(ValueError): self.robot.search_step(self.token, 'forward')


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.frames = Frames()
        self.frames.write(b'frame-0')
        self.serial = FakeSerial()
        self.robot = Controller(self.frames, self.serial)
        # Shorten only timing and count for most state-machine tests.
        self.robot.SEARCH_TURN_SECONDS = .04
        self.robot.SEARCH_MAX_TURNS = 2
        self.robot.CENTER_TURN_SECONDS = .025
        self.vision = Mock(model='mock-vision')
        self.vision.inspect.return_value = result()
        self.search = ObjectSearch(self.robot, lambda: 'test-key', vision=self.vision)
        self.done = threading.Event()
        self.feed_frames = True
        self.feed_contact = True
        self.patcher = patch('object_search.SETTLE_SECONDS', .025)
        self.patcher.start()
        def environment():
            n = 0
            while not self.done.wait(.01):
                n += 1
                if self.feed_frames:
                    self.frames.write(f'frame-{n}'.encode())
                if self.feed_contact:
                    self.search.contact = time.monotonic()
                self.robot.tick()
                self.search.tick()
        self.environment = threading.Thread(target=environment, daemon=True)
        self.environment.start()

    def tearDown(self):
        self.search.close()
        self.done.set()
        self.environment.join(1)
        self.patcher.stop()

    def start(self, **overrides):
        args = dict(token=TOKEN, target='a bottle', allow_turns=True,
                    frame_time=self.frames.latest()[1], stop_generation=self.robot.stop_generation)
        args.update(overrides)
        return self.search.start(**args)

    def finish(self):
        self.search.thread.join(3)
        self.assertFalse(self.search.thread.is_alive())
        self.assertEqual(self.robot.command, 'stop')
        self.assertIsNone(self.robot.owner)

    def movements(self):
        return [x for x in self.serial.commands if x != b'x']

    def test_sequence_runs_three_moves_between_images_and_reports_history(self):
        actions = ['forward', 'backward', 'left']
        seen = []
        def inspect(*args, **kwargs):
            self.assertEqual(self.robot.command, 'stop')
            seen.append(len(self.movements()))
            return {**result(), 'actions': actions if len(seen) == 1 else ['stop']}
        self.vision.inspect.side_effect = inspect
        self.start(explore=True, speed='full', max_turns=12, max_steps=6); self.finish()
        self.assertEqual(seen, [0, 3], 'No API check between actions in a plan')
        self.assertEqual(self.movements(), [b'B', b'F', b'R'])
        self.assertEqual((self.search.turns, self.search.steps), (1, 2))
        context = self.vision.inspect.call_args.kwargs['context']
        self.assertEqual((context['remaining_turns'], context['remaining_steps']), (11, 4))
        self.assertEqual(context['max_sequence_length'], 3)
        self.assertEqual([e['completed_action'] for e in context['history'] if 'completed_action' in e], actions)
        commands = self.serial.commands
        for i, command in enumerate(commands):
            if command != b'x': self.assertEqual(commands[i + 1], b'x')

    def test_invalid_whole_plan_never_executes_a_valid_prefix(self):
        cases = [(['left', 'forward'], {}),
                 (['forward', 'left', 'backward'], {'explore': True}),
                 (['left', 'right', 'left'], {'max_turns': 2}),
                 (['forward', 'forward'], {'explore': True, 'max_steps': 1}),
                 (['left', 'right'], {'sequence_length': 1})]
        for i, (actions, options) in enumerate(cases):
            with self.subTest(actions=actions, options=options):
                self.vision.inspect.return_value = {**result(), 'actions': actions}
                self.start(token=TOKEN + str(i), **options); self.finish()
                self.assertEqual(self.search.phase, 'error')
                self.assertEqual(self.movements(), [])

    def test_cancel_between_sequence_moves_prevents_remaining_actions(self):
        self.vision.inspect.return_value = {**result(), 'actions': ['left', 'right']}
        original = self.search._move_and_settle
        def move(*args, **kwargs):
            after = original(*args, **kwargs)
            self.robot.stop_all()
            return after
        with patch.object(self.search, '_move_and_settle', side_effect=move):
            self.start(); self.finish()
        self.assertEqual(len(self.movements()), 1)
        self.assertEqual(self.search.phase, 'cancelled')

    def test_expiry_between_sequence_moves_prevents_remaining_actions(self):
        self.vision.inspect.return_value = {**result(), 'actions': ['left', 'right']}
        original = self.search._move_and_settle
        def move(*args, **kwargs):
            after = original(*args, **kwargs)
            self.search.decision_time -= 9
            return after
        with patch.object(self.search, '_move_and_settle', side_effect=move):
            self.start(); self.finish()
        self.assertEqual(len(self.movements()), 1)
        self.assertIn('expired', self.search.message)

    def test_sequence_stops_on_camera_loss_or_deadline_between_moves(self):
        original = self.search._move_and_settle
        for kind in ('camera', 'deadline'):
            self.serial.commands.clear()
            self.vision.inspect.return_value = {**result(), 'actions': ['left', 'right']}
            def move(*args, **kwargs):
                after = original(*args, **kwargs)
                with self.search.lock:
                    if kind == 'camera':
                        self.feed_frames = False
                        self.frames.timestamp -= 3
                    else:
                        self.robot.search_deadline = time.monotonic() - 1
                return after
            with patch.object(self.search, '_move_and_settle', side_effect=move):
                self.start(token=TOKEN + kind); self.finish()
            self.assertEqual(len(self.movements()), 1)
            self.assertEqual(self.search.phase, 'cancelled')
            self.feed_frames = True
            self.frames.write(b'fresh')

    def test_zero_limits_inspect_without_search_motion_and_custom_limits_reset(self):
        self.start(max_turns=0, max_steps=0, explore=True); self.finish()
        self.assertEqual(self.movements(), [])
        self.assertEqual(self.search.phase, 'not_found')
        self.assertEqual(self.search.status()['max_turns'], 0)
        self.start(token=TOKEN + 'default'); self.finish()
        self.assertEqual(self.search.turns, 2)
        self.assertEqual(self.search.max_turns, self.robot.SEARCH_MAX_TURNS)

    def test_bad_limit_values_rejected_before_claim_or_api(self):
        for field, values in {'max_turns': [-1, 25, True, 1.5, '8'],
                              'max_steps': [-1, 13, False, 2.5, '4'],
                              'sequence_length': [0, 4, True, None, '2']}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.start(**{field: value})
        self.vision.inspect.assert_not_called()
        self.assertEqual(self.serial.commands, [])

    def test_planner_changes_direction_and_receives_completed_action_history(self):
        self.vision.inspect.side_effect = [{**result(), 'actions': ['left']}, result(), result('match'), result('match')]
        self.start(); self.finish()
        self.assertEqual(self.movements(), [slow_command(d, self.robot.calibration) for d in ('left', 'right')])
        contexts = [call.kwargs['context'] for call in self.vision.inspect.call_args_list]
        self.assertEqual(contexts[0]['history'], [])
        self.assertTrue(any(event.get('completed_action') == 'left' for event in contexts[1]['history']))
        self.assertNotIn('forward', contexts[0]['allowed_actions'])
        self.assertTrue(self.search.centered)

    def test_explore_forward_retreat_then_stop_is_bounded_and_reported(self):
        self.vision.inspect.side_effect = [{**result(), 'actions': [action]} for action in ('forward', 'backward', 'stop')]
        self.start(explore=True); self.finish()
        self.assertEqual(self.movements(), [slow_command(d, self.robot.calibration) for d in ('forward', 'backward')])
        self.assertEqual(self.search.steps, 2)
        self.assertEqual(self.search.phase, 'not_found')
        contexts = [call.kwargs['context'] for call in self.vision.inspect.call_args_list]
        self.assertNotIn('backward', contexts[0]['allowed_actions'])
        self.assertIn('backward', contexts[1]['allowed_actions'])
        self.assertNotIn('backward', contexts[2]['allowed_actions'])
        self.assertEqual(self.search.status()['action'], 'stop')

    def test_full_speed_search_then_calibrated_centring_and_next_search_defaults(self):
        self.vision.inspect.side_effect = [{**result(), 'actions': ['forward']}, result('match', 'left'),
            result('match', 'left'), result('match'), result('match')]
        self.start(explore=True, speed='full'); self.finish()
        self.assertEqual(self.movements(), [b'B', slow_command('left', self.robot.calibration)])
        self.assertTrue(self.search.centered)
        self.assertEqual(self.search.status()['speed'], 'full')
        self.vision.inspect.side_effect = [result(), result('match'), result('match')]
        self.start(token=TOKEN + 'next'); self.finish()
        self.assertEqual(self.movements()[-1], slow_command('right', self.robot.calibration))
        self.assertEqual(self.search.status()['speed'], 'slow')

    def test_unarmed_forward_and_backward_after_turn_are_rejected(self):
        self.vision.inspect.return_value = {**result(), 'actions': ['forward']}
        self.start(); self.finish()
        self.assertEqual(self.search.phase, 'error')
        self.assertEqual(self.movements(), [])
        self.vision.inspect.side_effect = [{**result(), 'actions': [action]} for action in ('forward', 'left', 'backward')]
        self.start(explore=True, token=TOKEN + 'explore'); self.finish()
        self.assertEqual(self.search.phase, 'error')
        self.assertEqual(self.movements(), [slow_command(d, self.robot.calibration) for d in ('forward', 'left')])

    def test_latest_verification_decision_replaces_false_candidate(self):
        self.vision.inspect.side_effect = [result('match'), {**result(), 'actions': ['left']},
                                          result('match'), result('match')]
        self.start(); self.finish()
        self.assertEqual(self.movements(), [slow_command('left', self.robot.calibration)])

    def test_old_decision_and_uncertain_movement_are_rejected(self):
        def delayed(*args, **kwargs):
            time.sleep(.02)
            return {**result(), 'actions': ['forward']}
        self.vision.inspect.side_effect = delayed
        with patch('object_search.MAX_DECISION_AGE', .01):
            self.start(explore=True); self.finish()
        self.assertIn('too late', self.search.message)
        self.vision.inspect.side_effect = None
        self.vision.inspect.return_value = {**result('uncertain'), 'actions': ['forward']}
        self.start(explore=True, token=TOKEN + 'uncertain'); self.finish()
        self.assertEqual(self.search.phase, 'error')
        self.assertEqual(self.movements(), [])

    def test_stop_during_forward_and_late_api_reply_cannot_restart_motion(self):
        self.robot.SEARCH_STEP_SECONDS = .3
        self.vision.inspect.return_value = {**result(), 'actions': ['forward']}
        self.start(explore=True)
        deadline = time.monotonic() + 1
        while self.robot.command == 'stop' and time.monotonic() < deadline: time.sleep(.005)
        self.assertEqual(self.robot.command, 'forward')
        self.robot.stop_all(); self.finish()
        self.assertEqual(len(self.movements()), 1)
        entered, release = threading.Event(), threading.Event()
        self.vision.inspect.side_effect = lambda *a, **kw: (entered.set(), release.wait(2), result())[2]
        self.start(explore=True, token=TOKEN + 'late')
        self.assertTrue(entered.wait(1))
        self.search.cancel(); manual = self.robot.claim()
        release.set(); self.search.thread.join(2)
        self.assertEqual(self.robot.owner, manual)
        self.assertEqual(len(self.movements()), 1)

    def test_match_requires_two_different_stationary_images_and_no_turn(self):
        images = []
        def inspect(key, target, jpeg, cancel, **kwargs):
            self.assertEqual(self.robot.command, 'stop')
            images.append(jpeg)
            return result('match')
        self.vision.inspect.side_effect = inspect
        self.start()
        self.finish()
        self.assertEqual(self.search.phase, 'found')
        self.assertEqual(len(images), 2)
        self.assertNotEqual(images[0], images[1])
        self.assertEqual(self.search.matched_jpeg, images[1])
        self.assertEqual(self.movements(), [])
        self.assertTrue(self.search.centered)

    def test_centres_left_and_right_with_new_stopped_images_then_double_confirmation(self):
        for direction in ('left', 'right'):
            with self.subTest(direction=direction):
                self.serial.commands.clear()
                replies = iter([result('match', direction), result('match', direction),
                                result('match'), result('match')])
                images = []
                def inspect(key, target, jpeg, cancel, **kwargs):
                    self.assertEqual(self.robot.command, 'stop')
                    images.append(jpeg)
                    return next(replies)
                self.vision.inspect.side_effect = inspect
                self.start(token=TOKEN + direction)
                self.finish()
                self.assertEqual(self.movements(), [slow_command(direction, self.robot.calibration)])
                self.assertTrue(self.search.centered)
                self.assertEqual(self.search.center_turns, 1)
                self.assertEqual(len(set(images)), 4)
                self.assertEqual(self.search.matched_jpeg, images[-1])

    def test_overshoot_reverses_with_a_new_image_and_does_not_announce_early(self):
        self.vision.inspect.side_effect = [result('match', 'right'), result('match', 'right'),
                                          result('match', 'left'), result('match'), result('match')]
        self.start()
        self.finish()
        self.assertEqual(self.movements(), [slow_command(d, self.robot.calibration) for d in ('right', 'left')])
        self.assertTrue(self.search.centered)
        self.assertEqual(self.search.checks, 5)

    def test_horizontal_camera_flip_reverses_search_and_centring_directions(self):
        self.search.mirrored = True
        self.vision.inspect.side_effect = [result(), result('match', 'left'), result('match', 'left'),
                                          result('match'), result('match')]
        self.start()
        self.finish()
        self.assertEqual(self.movements(), [slow_command(d, self.robot.calibration) for d in ('left', 'right')])
        self.assertTrue(self.search.centered)

    def test_unstable_centre_needs_two_consecutive_images_at_the_same_pose(self):
        self.vision.inspect.side_effect = [result('match'), result('match', 'left'),
            result('match'), result('match', 'right'), result('match'), result('match')]
        self.start()
        self.finish()
        self.assertEqual(self.search.center_turns, 2)
        self.assertEqual(self.search.checks, 6)
        self.assertTrue(self.search.centered)

    def test_lost_or_uncertain_target_stops_without_resuming_search(self):
        for decision in ('absent', 'uncertain'):
            with self.subTest(decision=decision):
                self.serial.commands.clear()
                self.vision.inspect.side_effect = [result('match', 'left'), result('match', 'left'), result(decision)]
                self.start(token=TOKEN + decision)
                self.finish()
                self.assertEqual(self.search.phase, 'not_found')
                self.assertFalse(self.search.centered)
                self.assertIsNone(self.search.matched_jpeg)
                self.assertEqual(len(self.movements()), 1)
                self.assertIn('lost', self.search.message)

    def test_ambiguous_position_never_drives_or_claims_centred(self):
        self.vision.inspect.return_value = result('match', 'unknown')
        self.start()
        self.finish()
        self.assertEqual(self.search.phase, 'found')
        self.assertFalse(self.search.centered)
        self.assertEqual(self.movements(), [])
        self.assertIn('unclear', self.search.centering_note)

    def test_centring_turn_limit_stops_even_if_position_never_improves(self):
        self.vision.inspect.return_value = result('match', 'left')
        self.start()
        self.finish()
        self.assertEqual(self.search.center_turns, 6)
        self.assertEqual(len(self.movements()), 6)
        self.assertEqual(self.search.phase, 'found')
        self.assertFalse(self.search.centered)
        self.assertIn('limit', self.search.centering_note)

    def test_shared_image_budget_cannot_claim_single_centred_image_as_success(self):
        self.vision.inspect.side_effect = [result('match', 'right'), result('match', 'right'), result('match')]
        with patch('object_search.MAX_CHECKS', 3):
            self.start()
            self.finish()
        self.assertEqual(self.search.checks, 3)
        self.assertEqual(self.search.center_turns, 1)
        self.assertFalse(self.search.centered)
        self.assertIn('image-check limit', self.search.centering_note)

    def test_cancel_after_centring_turn_prevents_late_reply_from_moving_new_driver(self):
        entered, release = threading.Event(), threading.Event()
        calls = 0
        def inspect(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                entered.set()
                release.wait(2)
            return result('match', 'left')
        self.vision.inspect.side_effect = inspect
        self.start()
        self.assertTrue(entered.wait(1))
        self.search.cancel(TOKEN)
        manual = self.robot.claim()
        release.set()
        self.search.thread.join(2)
        self.assertEqual(self.robot.owner, manual)
        self.assertEqual(len(self.movements()), 1)
        self.assertEqual(self.search.phase, 'cancelled')

    def test_stop_during_centring_turn_and_total_deadline_revoke_motion(self):
        for kind in ('stop', 'deadline'):
            with self.subTest(kind=kind):
                self.robot.CENTER_TURN_SECONDS = .3
                self.vision.inspect.return_value = result('match', 'right')
                self.start(token=TOKEN + kind)
                deadline = time.monotonic() + 1
                while self.robot.command == 'stop' and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertEqual(self.robot.command, 'right')
                with self.robot.lock:
                    if kind == 'stop':
                        self.robot.stop_all()
                    else:
                        self.robot.search_deadline = time.monotonic() - 1
                        self.robot.tick()
                count = len(self.movements())
                self.finish()
                self.assertEqual(len(self.movements()), count)
                self.assertEqual(self.search.phase, 'cancelled')

    def test_absent_turns_stops_and_starts_new_image_after_settling(self):
        self.start()
        self.finish()
        self.assertEqual(self.search.phase, 'not_found')
        self.assertEqual(self.search.turns, 2)
        self.assertEqual(len(self.movements()), 2)
        self.assertTrue(all(x == slow_command('right', self.robot.calibration) for x in self.movements()))
        self.assertEqual(self.vision.inspect.call_count, 3)
        self.assertEqual(len({c.args[2] for c in self.vision.inspect.call_args_list}), 3)

    def test_found_announces_verified_evidence_with_cloud_voice_after_stopping(self):
        speech = self.search.speech = Mock()
        speech.lock = threading.RLock()
        speech.cancel = threading.Event()
        speech.error = ''
        self.vision.inspect.side_effect = [result('match'),
            {**result('match'), 'description': 'A blue bottle stands on a table.'}]
        def say(text, **kwargs):
            self.assertEqual(self.robot.command, 'stop')
            self.assertIsNone(self.robot.owner)
            self.assertIn('blue bottle stands on a table', text)
            self.assertEqual(text.count('.'), 1)
            self.assertEqual(kwargs, {'api_key': 'test-key'})
        speech.say.side_effect = say
        self.start()
        self.finish()
        speech.say.assert_called_once()
        self.assertEqual(self.search.phase, 'found')
        speech.error = 'Voice quota reached.'
        self.assertEqual(self.search.status()['announcement_error'], speech.error)
        self.search.cancel()
        speech.stop.assert_called_once()

    def test_false_candidate_is_not_announced_as_found(self):
        self.vision.inspect.side_effect = [result('match'), result('uncertain'), result(), result(), result()]
        self.start()
        self.finish()
        self.assertEqual(self.search.phase, 'not_found')
        self.assertIsNone(self.search.matched_jpeg)

    def test_cancel_while_api_blocked_never_moves_after_late_reply(self):
        entered, release = threading.Event(), threading.Event()
        def inspect(*args, **kwargs):
            entered.set()
            release.wait(2)
            return result()
        self.vision.inspect.side_effect = inspect
        self.start()
        self.assertTrue(entered.wait(1))
        self.search.cancel(TOKEN)
        manual = self.robot.claim()
        release.set()
        self.search.thread.join(2)
        self.assertEqual(self.movements(), [])
        self.assertEqual(self.robot.owner, manual, 'Old cleanup must not revoke a new driver')
        self.assertEqual(self.search.phase, 'cancelled')

    def test_global_stop_during_turn_is_immediate_and_final(self):
        self.robot.SEARCH_TURN_SECONDS = .3
        self.start()
        deadline = time.monotonic() + 1
        while not self.movements() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.robot.command, 'right')
        self.robot.stop_all()
        count = len(self.movements())
        self.finish()
        self.assertEqual(len(self.movements()), count)
        self.assertEqual(self.search.phase, 'cancelled')

    def test_stop_before_late_start_and_cancel_before_start(self):
        previous = self.robot.stop_generation
        self.robot.stop_all()
        with self.assertRaisesRegex(ValueError, 'Stop was pressed'):
            self.start(stop_generation=previous)
        self.search.cancel(TOKEN)
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            self.start()
        self.assertEqual(self.movements(), [])

    def test_explicit_enable_valid_target_live_video_and_key_required(self):
        for kwargs in ({'allow_turns': False}, {'allow_turns': 'true'}, {'target': ''},
                       {'target': 'x' * 101}, {'frame_time': 0}, {'stop_generation': True}, {'explore': 'true'},
                       {'speed': 'turbo'}, {'speed': True}):
            with self.assertRaises(ValueError):
                self.start(**kwargs)
        self.search.key_provider = lambda: ''
        with self.assertRaisesRegex(ValueError, 'API key'):
            self.start()
        self.vision.inspect.assert_not_called()
        self.assertEqual(self.serial.commands, [])

    def test_disconnect_and_stale_camera_stop_even_during_api_check(self):
        for kind in ('browser', 'camera'):
            with self.subTest(kind=kind):
                entered, release = threading.Event(), threading.Event()
                def inspect(*args, **kwargs):
                    entered.set()
                    release.wait(2)
                    return result('match')
                self.vision.inspect.side_effect = inspect
                self.start(token=TOKEN + kind)
                self.assertTrue(entered.wait(1))
                with self.search.lock:
                    if kind == 'browser':
                        self.feed_contact = False
                        self.search.contact -= 2
                    else:
                        self.feed_frames = False
                        self.frames.timestamp -= 3
                    self.search.tick()
                self.assertIsNone(self.robot.owner)
                release.set()
                self.finish()
                self.assertEqual(self.search.phase, 'cancelled')
                self.feed_frames = self.feed_contact = True
                self.frames.write(b'new-frame')
        self.assertEqual(self.movements(), [])

    def test_failed_or_malformed_api_result_stops_without_turning(self):
        for reply in ({'decision': 'drive_forward'}, result('nonsense')):
            self.vision.inspect.return_value = reply
            self.start(token=TOKEN + str(len(self.retired_if_any())))
            self.finish()
            self.assertEqual(self.search.phase, 'error')
        self.assertEqual(self.movements(), [])

    def retired_if_any(self):
        return self.search.retired

    def test_max_image_budget_no_unbounded_retry(self):
        self.vision.inspect.return_value = result('uncertain')
        with patch('object_search.MAX_CHECKS', 2):
            self.start()
            self.finish()
        self.assertEqual(self.vision.inspect.call_count, 2)
        self.assertEqual(self.search.turns, 0)

    def test_search_claim_blocks_manual_driver_and_calibration(self):
        entered, release = threading.Event(), threading.Event()
        self.vision.inspect.side_effect = lambda *args, **kwargs: (entered.set(), release.wait(1), result())[2]
        self.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(ValueError):
            self.robot.claim()
        with self.assertRaises(ValueError):
            self.robot.set_calibration(self.robot.calibration)
        self.search.cancel(TOKEN)
        release.set()
        self.finish()

    def test_http_protection_stop_and_matched_image(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http.controller, http.search = self.robot, self.search
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        def post(path, headers, **extra):
            body = {'token': TOKEN, 'target': 'bottle', 'allow_turns': True,
                    'frame_time': self.frames.latest()[1], 'stop_generation': self.robot.stop_generation, **extra}
            req = Request(f'http://127.0.0.1:{http.server_port}'+path,
                          data=json.dumps(body).encode(), headers=headers)
            try:
                response = urlopen(req, timeout=2)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, json.load(response)
        try:
            path = '/api/search/start'
            self.assertEqual(post(path, {})[0], 403)
            self.assertEqual(post(path, {'X-Robot-Control': '1', 'Origin': 'http://evil.example'})[0], 403)
            self.assertEqual(post(path, {'X-Robot-Control': '1'}, allow_turns=False)[0], 409)
            self.assertEqual(post(path, {'X-Robot-Control': '1'}, explore='true')[0], 409)
            self.assertEqual(post(path, {'X-Robot-Control': '1'}, speed='turbo')[0], 409)
            self.assertEqual(post(path, {'X-Robot-Control': '1'}, live='true')[0], 409)
            self.assertEqual(post(path, {'X-Robot-Control': '1'}, image_rate=99)[0], 409)
            self.assertEqual(post(path, {'X-Robot-Control': '1'}, explore=True, speed='full',
                                  max_turns=12, max_steps=6, sequence_length=2)[0], 200)
            self.assertTrue(self.search.explore)
            self.assertEqual(self.search.speed, 'full')
            self.assertEqual((self.search.max_turns, self.search.max_steps, self.search.sequence_length), (12, 6, 2))
            self.assertEqual(post('/api/stop', {'X-Robot-Control': '1'})[0], 200)
            self.finish()
            self.assertEqual(self.search.phase, 'cancelled')
            self.search.matched_jpeg = b'matched-jpeg'
            with urlopen(f'http://127.0.0.1:{http.server_port}/api/search/frame') as response:
                self.assertEqual(response.read(), b'matched-jpeg')
                self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


class VisionTests(unittest.TestCase):
    def test_payload_image_schema_and_no_remote_motion_tools(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps({'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(result('match'))}]}]}).encode()
        with patch('search_vision.http.client.HTTPSConnection', return_value=connection):
            self.assertEqual(SearchVision().inspect('secret-test-key', 'bottle', b'jpeg', threading.Event()), result('match'))
        request = connection.request.call_args
        payload = json.loads(request.kwargs['body'])
        self.assertFalse(payload['store'])
        self.assertNotIn('tools', payload)
        self.assertTrue(payload['text']['format']['strict'])
        self.assertNotIn('secret-test-key', request.kwargs['body'])
        connection.close.assert_called()

    def test_plan_schema_rejects_unbounded_ambiguous_and_malformed_sequences(self):
        for actions in ([], ['left'] * 4, 'left', [None], [{}], ['fly'],
                        ['inspect', 'left'], ['stop', 'forward']):
            with self.subTest(actions=actions), self.assertRaises(SearchError):
                validate_result({**result(), 'actions': actions})
        for decision in ('match', 'uncertain'):
            with self.assertRaises(SearchError):
                validate_result({**result(decision), 'actions': ['left', 'inspect']})
        validate_result({**result(), 'actions': ['forward', 'left', 'inspect']})

    def test_errors_are_sanitized_and_invalid_data_rejected(self):
        with patch('search_vision.http.client.HTTPSConnection') as factory:
            factory.return_value.request.side_effect = RuntimeError('Bearer secret-test-key')
            with self.assertRaises(SearchError) as error:
                SearchVision().inspect('secret-test-key', 'bottle', b'jpeg', threading.Event())
            self.assertNotIn('secret-test-key', str(error.exception))
        with self.assertRaises(SearchError):
            validate_result({**result(), 'turn': 'forward'})
        for change in ({'actions': ['drive_forever']}, {'reason': ''}, {'duration': 999},
                       {'decision': 'match', 'actions': ['forward']}):
            with self.assertRaises(SearchError): validate_result({**result(), **change})


if __name__ == '__main__':
    unittest.main()
