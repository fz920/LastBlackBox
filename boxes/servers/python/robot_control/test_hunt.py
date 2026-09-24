"""Clue interpretation and automatic handoff; all hardware/API calls are simulated."""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hunt import ClueMicrophone, Hunt
from realtime import TalkError
from search_intent import SearchIntent, validate_proposal
from server import Controller, Frames, Handler, slow_command
from test_server import FakeSerial
from test_talk import Socket

TOKEN = 'hunt-test-token-123456'


def proposal():
    return {'decision': 'search', 'target': 'a red drinking container',
            'heard': 'Find something red I could drink from.', 'question': ''}


class IntentTests(unittest.TestCase):
    def socket(self, output=None):
        sock = Socket()
        def send(raw):
            event = json.loads(raw)
            sock.sent.append(event)
            if event['type'] == 'response.create':
                sock.events.append({'type': 'response.done', 'response': {'status': 'completed',
                    'output': output if output is not None else [{'type': 'function_call',
                        'name': 'interpret_clue', 'arguments': json.dumps(proposal())}]}})
        sock.send = send
        return sock

    def test_text_and_audio_produce_only_a_proposal_not_motor_commands(self):
        for prompt in ('Find something red I could drink from.', None):
            with self.subTest(prompt=prompt):
                sock = self.socket()
                client = SearchIntent(connect=Mock(return_value=sock))
                result = client.interpret('test-secret', b'\x00\x01' * 12000, prompt, threading.Event())
                self.assertEqual(result, proposal())
                session = sock.sent[0]['session']
                self.assertEqual(session['output_modalities'], ['text'])
                self.assertEqual(session['tool_choice'], {'type': 'function', 'name': 'interpret_clue'})
                self.assertEqual(len(session['tools']), 1)
                types = [event['type'] for event in sock.sent]
                self.assertEqual('input_audio_buffer.commit' in types, prompt is None)
                self.assertNotIn('test-secret', json.dumps(sock.sent))
                self.assertTrue(sock.closed)

    def test_bad_outputs_and_unknown_tools_are_rejected(self):
        for output in ([], [{'type': 'function_call', 'name': 'drive', 'arguments': '{}'}],
                       [{'type': 'function_call', 'name': 'interpret_clue', 'arguments': 'bad json'}]):
            with self.subTest(output=output):
                sock = self.socket(output)
                with self.assertRaises(TalkError):
                    SearchIntent(connect=lambda *a, **k: sock).interpret('secret', None, 'cup', threading.Event())
                self.assertTrue(sock.closed)

    def test_live_api_json_text_shape_uses_the_same_strict_validation(self):
        for value in (proposal(), {**proposal(), 'motor': 'forward'}, 'Find a cup', {}):
            with self.subTest(value=value):
                sock = self.socket([{'type': 'message', 'status': 'completed', 'role': 'assistant',
                                    'content': [{'type': 'output_text', 'text': json.dumps(value)}]}])
                client = SearchIntent(connect=lambda *a, **k: sock)
                if value == proposal():
                    self.assertEqual(client.interpret('key', None, 'red cup', threading.Event()), value)
                else:
                    with self.assertRaises(TalkError):
                        client.interpret('key', None, 'red cup', threading.Event())
                self.assertTrue(sock.closed)

    def test_invalid_or_empty_proposals_rejected_and_cancel_prevents_connection(self):
        for value in ({**proposal(), 'command': 'forward'}, {**proposal(), 'target': ''},
                      {**proposal(), 'target': 'x' * 101}, {**proposal(), 'decision': 'move'},
                      {**proposal(), 'decision': 'clarify', 'question': ''}):
            with self.assertRaises(TalkError):
                validate_proposal(value)
        connect = Mock()
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(TalkError):
            SearchIntent(connect=connect).interpret('key', None, 'cup', cancel)
        connect.assert_not_called()


class MicrophoneTests(unittest.TestCase):
    def capture(self, payload, *, fail_guard=False, limit=15):
        # A real pipe/process exercises capture and cleanup without touching ALSA.
        child = subprocess.Popen([sys.executable, '-c',
            f'import sys,time; sys.stdout.buffer.write({payload!r}); sys.stdout.flush(); time.sleep(30)'],
            stdout=subprocess.PIPE)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        mic = ClueMicrophone()
        cancel, released = threading.Event(), threading.Event()
        read = os.read
        count = 0
        def consume(fd, size):
            nonlocal count
            data = read(fd, size)
            count += len(data)
            if count >= len(payload): released.set()
            return data
        def guard():
            if fail_guard: raise TalkError('Browser disconnected.')
        with patch('hunt.subprocess.Popen', return_value=child) as start, \
             patch('hunt.os.read', side_effect=consume), patch('hunt.MAX_RECORDING', limit):
            try:
                return mic.record('test-device', cancel, released, guard)
            finally:
                self.assertIsNotNone(child.poll())
                self.assertTrue(child.stdout.closed)
                self.assertIsNone(mic.process)
                self.assertIn('24000', start.call_args.args[0])

    def test_release_returns_bounded_even_pcm_and_closes_capture(self):
        self.assertEqual(self.capture(b'\x01' * 12001), b'\x01' * 12000)

    def test_short_capture_limit_and_guard_failure_cleanup_process(self):
        for options in ({'payload': b'a' * 100}, {'payload': b'a' * 12000, 'limit': .05},
                        {'payload': b'a' * 12000, 'fail_guard': True}):
            with self.subTest(options={k: v for k, v in options.items() if k != 'payload'}):
                with self.assertRaises(TalkError): self.capture(**options)
        cancel = threading.Event(); cancel.set()
        with patch('hunt.subprocess.Popen') as start, self.assertRaises(TalkError):
            ClueMicrophone().record('test', cancel, threading.Event(), lambda: None)
        start.assert_not_called()


class HuntTests(unittest.TestCase):
    def setUp(self):
        self.frames = Frames(); self.frames.write(b'image')
        self.serial = FakeSerial()
        self.robot = Controller(self.frames, self.serial)
        self.robot.SEARCH_TURN_SECONDS = self.robot.CENTER_TURN_SECONDS = .02
        self.audio_lock = threading.Lock()
        self.vision = Mock()
        self.vision.inspect.return_value = {'decision': 'match', 'location': 'centre',
                                          'actions': ['inspect'], 'reason': 'Confirm the red container.',
                                          'description': 'A red cup sits on the desk.'}
        self.speech = Mock(lock=threading.RLock(), cancel=threading.Event(), error='')
        self.hunt = Hunt(self.robot, lambda: 'test-key', self.speech, self.vision, audio_lock=self.audio_lock)
        self.hunt.interpreter = Mock()
        self.hunt.interpreter.interpret.return_value = proposal()
        self.hunt.microphone = Mock()
        self.hunt._voice_device = Mock(return_value='test-device')
        self.patcher = patch('object_search.SETTLE_SECONDS', .01); self.patcher.start()
        self.done = threading.Event()
        self.feed_contact = True
        self.feed_frames = True
        def environment():
            frame = 0
            while not self.done.wait(.01):
                frame += 1
                if self.feed_frames: self.frames.write(f'image-{frame}'.encode())
                if self.feed_contact: self.hunt.contact = time.monotonic()
                self.robot.tick(); self.hunt.tick()
        self.environment = threading.Thread(target=environment, daemon=True); self.environment.start()

    def tearDown(self):
        self.hunt.close(); self.done.set(); self.environment.join(1); self.patcher.stop()

    def start(self, target='Find something red I could drink from.', **extra):
        params = {'token': TOKEN, 'target': target, 'allow_turns': True,
                  'frame_time': self.frames.latest()[1], 'stop_generation': self.robot.stop_generation, **extra}
        return self.hunt.start(**params)

    def finish(self):
        self.hunt.thread.join(3)
        self.assertFalse(self.hunt.thread.is_alive())
        self.assertEqual(self.robot.command, 'stop')
        self.assertFalse(self.audio_lock.locked())

    def moves(self):
        return [command for command in self.serial.commands if command != b'x']

    def test_typed_clue_automatically_searches_centres_and_announces(self):
        match = self.vision.inspect.return_value
        self.vision.inspect.side_effect = [{**match, 'decision': 'absent', 'location': 'unknown', 'actions': ['right']},
            {**match, 'location': 'left'}, {**match, 'location': 'left'}, match, match]
        self.start(); self.finish()
        self.hunt.microphone.record.assert_not_called()
        self.assertEqual(self.hunt.target, proposal()['target'])
        self.assertEqual(self.hunt.phase, 'found')
        self.assertTrue(self.hunt.centered)
        self.assertEqual(self.moves(), [slow_command(d, self.robot.calibration) for d in ('right', 'left')])
        self.assertTrue(all(call.args[1] == proposal()['target'] for call in self.vision.inspect.call_args_list))
        self.speech.say.assert_called_once()
        self.assertEqual(self.speech.say.call_args.kwargs, {'api_key': 'test-key'})
        self.assertNotIn('test-key', json.dumps(self.hunt.status()))

    def test_voice_holds_wheels_stopped_and_releases_audio_before_search(self):
        entered = threading.Event()
        def record(device, cancel, released, guard):
            self.assertTrue(self.audio_lock.locked())
            entered.set()
            while not released.wait(.01): guard()
            return b'fake-pcm'
        self.hunt.microphone.record.side_effect = record
        self.start(None)
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.robot.command, 'stop')
        with self.assertRaises(ValueError): self.robot.claim()
        with self.assertRaises(ValueError): self.hunt.finish_recording('another-test-token-123')
        self.hunt.finish_recording(TOKEN); self.finish()
        self.hunt.interpreter.interpret.assert_called_once_with('test-key', b'fake-pcm', None, self.hunt.cancelled)
        self.assertEqual(self.hunt.request_text, proposal()['heard'])
        self.assertEqual(self.hunt.phase, 'found')

    def test_clarification_speaks_and_never_starts_visual_checks_or_motion(self):
        self.hunt.interpreter.interpret.return_value = {'decision': 'clarify', 'target': '',
            'heard': 'Find that thing.', 'question': 'What does the object look like?'}
        self.start('Find that thing.'); self.finish()
        self.assertEqual(self.hunt.phase, 'clarify')
        self.assertEqual(self.moves(), [])
        self.vision.inspect.assert_not_called()
        self.speech.say.assert_called_once_with('What does the object look like?', api_key='test-key')

    def test_stop_disconnect_and_camera_loss_while_interpreting_prevent_handoff(self):
        for kind in ('stop', 'disconnect', 'camera'):
            with self.subTest(kind=kind):
                entered, release = threading.Event(), threading.Event()
                self.hunt.interpreter.interpret.side_effect = lambda *args: (entered.set(), release.wait(2), proposal())[2]
                self.start(token=TOKEN + kind)
                self.assertTrue(entered.wait(1))
                with self.robot.lock:
                    if kind == 'stop': self.robot.stop_all()
                    elif kind == 'disconnect':
                        self.feed_contact = False; self.hunt.contact -= 3
                    else:
                        self.feed_frames = False; self.frames.timestamp -= 3
                    self.hunt.tick()
                manual = self.robot.claim()
                release.set(); self.finish()
                self.assertEqual(self.robot.owner, manual)
                self.assertEqual(self.hunt.phase, 'cancelled')
                self.robot.stop_all()
                self.feed_contact = self.feed_frames = True; self.frames.write(b'fresh')
        self.assertEqual(self.moves(), [])
        self.vision.inspect.assert_not_called()

    def test_voice_cancel_does_not_submit_audio_and_releases_lock(self):
        entered = threading.Event()
        def record(device, cancel, released, guard):
            entered.set(); cancel.wait(2); guard()
        self.hunt.microphone.record.side_effect = record
        self.start(None); self.assertTrue(entered.wait(1))
        self.hunt.cancel(TOKEN); self.finish()
        self.hunt.microphone.interrupt.assert_called()
        self.hunt.interpreter.interpret.assert_not_called()
        self.assertEqual(self.moves(), [])

    def test_bad_proposal_and_failed_recording_do_not_move(self):
        self.hunt.interpreter.interpret.return_value = {**proposal(), 'motor': 'forward'}
        self.start(); self.finish()
        self.assertEqual(self.hunt.phase, 'error')
        self.hunt.microphone.record.side_effect = TalkError('Microphone capture failed.')
        self.start(None, token=TOKEN + 'voice'); self.finish()
        self.assertEqual(self.hunt.phase, 'error')
        self.assertEqual(self.moves(), [])
        self.vision.inspect.assert_not_called()

    def test_input_limits_audio_ownership_and_cancel_before_start(self):
        for target in ('', 'x' * 501, 7):
            with self.assertRaises(ValueError): self.start(target)
        self.audio_lock.acquire()
        with self.assertRaises(ValueError): self.start(None)
        self.audio_lock.release()
        self.hunt.cancel(TOKEN)
        with self.assertRaises(ValueError): self.start(None)
        self.assertFalse(self.audio_lock.locked())
        self.hunt.interpreter.interpret.assert_not_called()

    def test_http_voice_routes_protected_and_unicode_clue_fits(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http.controller, http.search = self.robot, self.hunt
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        def post(action, headers, **extra):
            body = {'token': TOKEN, 'allow_turns': True, 'frame_time': self.frames.latest()[1],
                    'stop_generation': self.robot.stop_generation, **extra}
            req = Request(f'http://127.0.0.1:{http.server_port}/api/search/{action}',
                          data=json.dumps(body).encode(), headers=headers)
            try: response = urlopen(req, timeout=2)
            except HTTPError as exc: response = exc
            with response: return response.status
        try:
            for action in ('listen', 'finish'):
                self.assertEqual(post(action, {}), 403)
                self.assertEqual(post(action, {'X-Robot-Control': '1', 'Origin': 'http://elsewhere'}), 403)
            with patch.object(self.hunt, 'start', return_value={'accepted': True}) as start:
                self.assertEqual(post('start', {'X-Robot-Control': '1'}, target='你' * 500, live=True, image_rate=4), 200)
                self.assertEqual(start.call_args.args[1], '你' * 500)
                self.assertTrue(start.call_args.kwargs['live'])
                self.assertEqual(start.call_args.kwargs['image_rate'], 4)
                self.assertEqual(post('listen', {'X-Robot-Control': '1'},
                                      max_turns=18, max_steps=8, sequence_length=2, live=True, image_rate=.5), 200)
                self.assertEqual(start.call_args.kwargs['max_turns'], 18)
                self.assertEqual(start.call_args.kwargs['max_steps'], 8)
                self.assertEqual(start.call_args.kwargs['sequence_length'], 2)
                self.assertTrue(start.call_args.kwargs['live'])
                self.assertEqual(start.call_args.kwargs['image_rate'], .5)
                self.assertIsNone(start.call_args.args[1])
            with patch.object(self.hunt, 'finish_recording', return_value={'accepted': True}) as finish:
                self.assertEqual(post('finish', {'X-Robot-Control': '1'}), 200)
                finish.assert_called_once_with(TOKEN)
        finally:
            http.shutdown(); http.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
