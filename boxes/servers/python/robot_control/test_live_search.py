"""Realtime image feed tests using a fake socket and simulated motor controller."""
import json
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from live_search import LiveSearchVision, validate_live_settings
from realtime import websocket
from search_vision import SearchError
import test_search as search_tests
from test_search import result


class Socket:
    def __init__(self):
        self.events = queue.Queue()
        self.sent = []
        self.closed = False
        self.auto = True
        self.answer = result('match')
        self.last_request = None

    def settimeout(self, value):
        pass

    def send(self, raw):
        event = json.loads(raw)
        self.sent.append((time.monotonic(), event))
        if event['type'] == 'session.update':
            self.events.put({'type': 'session.updated'})
        if event['type'] == 'response.create':
            self.last_request = event['response']
            if self.auto:
                self.reply()

    def reply(self, **overrides):
        self.events.put({'type': 'response.done', 'response': {
            'status': 'completed', 'metadata': self.last_request['metadata'],
            'output': [{'type': 'function_call', 'name': 'assess_view',
                        'arguments': json.dumps(self.answer)}], **overrides}})

    def recv(self):
        if self.closed:
            return ''
        try:
            return json.dumps(self.events.get(timeout=.01))
        except queue.Empty:
            raise websocket.WebSocketTimeoutException()

    def shutdown(self):
        self.closed = True
        self.events.put({})


class LiveTransportTests(unittest.TestCase):
    def setUp(self):
        self.socket = Socket()
        self.cancel = threading.Event()
        self.number = 0
        self.monitor = False
        self.notices = []
        def source():
            self.number += 1
            return {'jpeg': b'jpeg-' + str(self.number).encode(), 'timestamp': time.monotonic(),
                    'number': self.number, 'target': 'a bottle', 'phase': 'moving' if self.monitor else 'looking',
                    'moving': self.monitor, 'monitor': self.monitor, 'epoch': 1}
        self.source = source
        self.connect = Mock(return_value=self.socket)
        self.client = LiveSearchVision(source, lambda *args: self.notices.append(args),
                                       image_rate=4, connect=self.connect)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.client.close()
        if self.client.thread:
            self.client.thread.join(1)
            self.assertFalse(self.client.thread.is_alive())

    def inspect(self):
        return self.client.inspect_frame('test-secret', 'a bottle', self.cancel, context={
            'after': time.monotonic(), 'allowed_actions': ['inspect', 'stop'], 'max_sequence_length': 1})

    def wait_for(self, predicate, seconds=2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.01)
        self.fail('Timed out waiting for simulated live event')

    def test_reuses_session_frames_correlated_and_no_audio_or_raw_movement_tools(self):
        first, jpeg, stamp = self.inspect()
        second, next_jpeg, next_stamp = self.inspect()
        self.assertEqual(first, result('match'))
        self.assertNotEqual(jpeg, next_jpeg)
        self.assertGreater(next_stamp, stamp)
        self.connect.assert_called_once()
        sent = [e for _, e in self.socket.sent]
        self.assertNotIn('test-secret', json.dumps(sent))
        settings = sent[0]['session']
        self.assertEqual(settings['output_modalities'], ['text'])
        self.assertEqual([t['name'] for t in settings['tools']], ['assess_view'])
        requests = [e['response'] for e in sent if e['type'] == 'response.create']
        self.assertTrue(all(r['conversation'] == 'none' for r in requests))
        self.assertEqual(self.client.status()['assessments'], 2)

    def test_uploads_continue_while_response_waits_and_remote_images_are_bounded(self):
        self.socket.auto = False
        outputs = []
        def run():
            try: outputs.append(self.inspect())
            except SearchError: pass
        thread = threading.Thread(target=run); thread.start()
        self.wait_for(lambda: self.client.images >= 6)
        self.assertTrue(thread.is_alive(), 'Sending frames must not await GPT response')
        events = self.socket.sent
        times = [t for t, e in events if e['type'] == 'conversation.item.create']
        self.assertTrue(all(b - a >= .24 for a, b in zip(times, times[1:])))
        active = set()
        for _, e in events:
            if e['type'] == 'conversation.item.create': active.add(e['item']['id'])
            if e['type'] == 'conversation.item.delete': active.remove(e['item_id'])
        self.assertLessEqual(len(active), 4)
        self.assertIn('liveframe_1', active, 'In-flight image stays pinned')
        self.assertEqual(sum(e['type'] == 'response.create' for _, e in events), 1)
        self.socket.reply(); thread.join(1)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0][1], b'jpeg-1', 'Reply retains the assessed frame, not the newest upload')

    def test_monitoring_is_bounded_and_uses_no_movement_permissions(self):
        self.inspect()
        self.monitor = True
        with patch('live_search.MAX_MONITOR_CHECKS', 1):
            self.wait_for(lambda: len(self.notices) == 1)
            self.wait_for(lambda: bool(self.client.error))
        self.assertIn('limit', self.client.error)
        self.assertEqual(self.notices[0][1]['epoch'], 1)
        context = json.loads(self.socket.last_request['input'][1]['content'][0]['text'])
        self.assertEqual(context['allowed_actions'], ['inspect', 'stop'])
        self.assertTrue(context['monitor_only'])

    def test_close_during_response_wakes_waiter_and_never_delivers_late_observation(self):
        self.socket.auto = False
        outcomes = []
        def run():
            try: self.inspect()
            except SearchError as exc: outcomes.append(str(exc))
        thread = threading.Thread(target=run); thread.start()
        self.wait_for(lambda: self.socket.last_request is not None)
        self.client.close(); self.socket.reply(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(outcomes)
        self.assertEqual(self.notices, [])

    def test_failures_and_malformed_replies_are_sanitized(self):
        self.connect.side_effect = RuntimeError('Bearer test-secret')
        with self.assertRaises(SearchError) as error: self.inspect()
        self.assertNotIn('test-secret', str(error.exception))
        for response in ({'status': 'failed', 'output': []},
                         {'status': 'completed', 'output': [{'type': 'message'}]},
                         {'status': 'completed', 'output': [{'type': 'function_call', 'name': 'drive'}]}):
            with self.assertRaises(SearchError): LiveSearchVision._result(response)

    def test_stalled_response_stops_transport(self):
        self.socket.auto = False
        with patch('live_search.RESPONSE_TIMEOUT', .1), self.assertRaisesRegex(SearchError, 'timed out'):
            self.inspect()
        self.wait_for(lambda: self.socket.closed)

    def test_json_text_is_validated_as_strictly_as_function_arguments(self):
        response = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(result('match'))}]}]}
        self.assertEqual(LiveSearchVision._result(response), result('match'))
        for text in ('Drive forward forever', '{"actions":["forward"]}',
                     json.dumps({**result('uncertain'), 'actions': ['forward']})):
            response['output'][0]['content'][0]['text'] = text
            with self.assertRaises(SearchError): LiveSearchVision._result(response)

    def test_mode_and_rate_validation(self):
        for rate in (.5, 1, 2, 4): validate_live_settings(True, rate)
        for rate in (0, -1, 3, 5, True, '2', None, float('nan'), float('inf')):
            with self.assertRaises(ValueError): validate_live_settings(True, rate)
        with self.assertRaises(ValueError): validate_live_settings('true', 2)

    def test_memory_context_uses_exact_assessed_frame(self):
        self.client.context_provider = lambda jpeg: {'search_memory': {'image_marker': jpeg.decode()}}
        _, jpeg, _ = self.inspect()
        context = json.loads(self.socket.last_request['input'][1]['content'][0]['text'])
        self.assertEqual(context['search_memory']['image_marker'], jpeg.decode())

    def test_only_redundant_stationary_monitoring_is_deferred_and_periodically_rechecked(self):
        from test_search_memory import picture
        jpeg = picture()
        moving = False
        epoch = 1
        def source():
            frame = self.source()
            return {**frame, 'jpeg': jpeg, 'moving': moving, 'epoch': epoch,
                    'phase': 'moving' if moving else 'settling'}
        self.client.source = source
        self.socket.answer = {**result(), 'actions': ['inspect']}
        self.inspect()
        self.monitor = True
        self.wait_for(lambda: self.client.skipped_monitor_frames >= 1)
        self.assertEqual(self.client.monitor_checks, 1)
        self.wait_for(lambda: self.client.monitor_checks >= 2)
        self.assertGreaterEqual(self.client.skipped_monitor_frames, 2)
        # Changed images, changed epochs and moving views require fresh checks.
        for change in ('image', 'epoch', 'moving'):
            count = self.client.monitor_checks
            if change == 'image': jpeg = picture('blue')
            if change == 'epoch': epoch += 1
            if change == 'moving': moving = True
            self.wait_for(lambda: self.client.monitor_checks > count, seconds=.8)
        # Foreground verification bypasses suppression even for identical images.
        moving = False
        before = self.client.assessments
        self.inspect(); self.inspect()
        self.assertGreaterEqual(self.client.assessments, before + 2)


class LiveSearchIntegrationTests(unittest.TestCase):
    # Reuse the simulated frame producer and motor watchdog without inheriting tests.
    setUp = search_tests.SearchTests.setUp
    tearDown = search_tests.SearchTests.tearDown
    start = search_tests.SearchTests.start
    finish = search_tests.SearchTests.finish
    movements = search_tests.SearchTests.movements

    def live_fake(self, actions=None):
        client = Mock()
        client.status.return_value = {'error': '', 'images_sent': 0, 'assessments': 0}
        def inspect(*args, **kwargs):
            stamp = time.monotonic()
            return {**result(), 'actions': actions or ['stop']}, b'live-frame', stamp
        client.inspect_frame.side_effect = inspect
        self.search.live_factory = Mock(return_value=client)
        return client

    def test_live_opt_in_passes_rate_and_uses_live_image_for_confirmation(self):
        client = self.live_fake()
        client.inspect_frame.side_effect = lambda *a, **kw: (result('match'), b'live-match', time.monotonic())
        self.start(live=True, image_rate=.5); self.finish()
        self.vision.inspect.assert_not_called()
        self.assertTrue(self.search.centered)
        self.assertEqual(self.search.matched_jpeg, b'live-match')
        self.assertEqual(self.search.live_factory.call_args.kwargs['image_rate'], .5)
        client.close.assert_called()
        self.assertEqual(self.search.status()['image_rate'], .5)

    def test_candidate_interrupts_pulse_and_discards_remaining_plan(self):
        client = self.live_fake(['forward', 'left', 'right'])
        original = self.search._wait
        interrupted = False
        def wait(duration, **kwargs):
            nonlocal interrupted
            if kwargs.get('interruptible') and not interrupted:
                interrupted = True
                self.search._live_observation(self.search.driver, result('match'), {
                    'timestamp': time.monotonic(), 'epoch': self.search.plan_epoch})
                self.assertEqual(self.robot.command, 'stop')
                self.assertEqual(self.robot.retreat_until, 0)
                client.inspect_frame.side_effect = lambda *a, **kw: (result('match'), b'verified', time.monotonic())
            original(duration, **kwargs)
        with patch.object(self.search, '_wait', side_effect=wait):
            self.start(live=True, explore=True); self.finish()
        self.assertTrue(self.search.centered)
        self.assertEqual(len(self.movements()), 1)
        self.assertTrue(any('interrupted_action' in e for e in self.search.history))

    def test_stop_observation_cancels_all_remaining_motion(self):
        self.live_fake(['left', 'right'])
        original = self.search._wait
        def wait(duration, **kwargs):
            if kwargs.get('interruptible'):
                self.search._live_observation(self.search.driver, {**result(), 'actions': ['stop']}, {
                    'timestamp': time.monotonic(), 'epoch': self.search.plan_epoch})
            original(duration, **kwargs)
        with patch.object(self.search, '_wait', side_effect=wait):
            self.start(live=True); self.finish()
        self.assertEqual(len(self.movements()), 1)
        self.assertEqual(self.search.phase, 'cancelled')
        self.assertIn('Live view requested', self.search.message)

    def test_old_epoch_or_old_frame_cannot_interrupt_new_plan_or_driver(self):
        self.search.driver = self.robot.claim(kind='search')
        self.search.phase = 'moving'; self.search.plan_epoch = 3
        self.robot.search_turn(self.search.driver)
        for epoch, stamp in ((2, time.monotonic()), (3, time.monotonic() - 3)):
            self.search._live_observation(self.search.driver, result('match'), {'epoch': epoch, 'timestamp': stamp})
            self.assertEqual(self.robot.command, 'right')
        driver = self.search.driver
        self.robot.stop_all(); manual = self.robot.claim()
        self.search._live_observation(driver, result('match'), {'epoch': 3, 'timestamp': time.monotonic()})
        self.assertEqual(self.robot.owner, manual)

    def test_live_connection_failure_stops_and_closes_without_fallback(self):
        client = self.live_fake(['left', 'right'])
        client.inspect_frame.side_effect = SearchError('Live image API timed out. Search stopped.')
        self.start(live=True); self.finish()
        self.assertEqual(self.search.phase, 'error')
        self.assertEqual(self.movements(), [])
        self.vision.inspect.assert_not_called()
        client.close.assert_called()

    def test_invalid_live_options_rejected_before_motor_claim(self):
        for kwargs in ({'live': 'yes'}, {'image_rate': True}, {'image_rate': 10}):
            with self.assertRaises(ValueError): self.start(**kwargs)
        self.assertEqual(self.serial.commands, [])
