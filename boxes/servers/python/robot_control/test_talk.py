"""No credentials, cloud connections, microphones or motor output in these tests."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from realtime import Realtime, TalkError
from server import Controller, Frames, Handler
from talk import Talk

TOKEN = 'test-recording-token-123'


def reply_events():
    return [{'type': 'response.output_audio_transcript.delta', 'delta': 'A cup.'},
            {'type': 'response.output_audio.delta', 'delta': base64.b64encode(b'\x00\x01' * 2400).decode()},
            {'type': 'response.output_audio_transcript.done', 'transcript': 'A cup.'},
            {'type': 'response.done', 'response': {'status': 'completed'}}]


class Socket:
    def __init__(self):
        self.sent = []
        self.events = [{'type': 'session.created'}, {'type': 'session.updated'}]
        self.closed = False

    def send(self, data):
        event = json.loads(data)
        self.sent.append(event)
        if event['type'] == 'response.create':
            self.events.extend(reply_events())

    def recv(self):
        return json.dumps(self.events.pop(0))

    def settimeout(self, value):
        pass

    def shutdown(self):
        self.closed = True


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.socket = Socket()
        self.connect = Mock(return_value=self.socket)
        self.client = Realtime(connect=self.connect)
        self.cancel = threading.Event()
        self.audio, self.transcript = Mock(), Mock()

    def ask(self):
        self.client.respond('test-secret', b'\x00\x01' * 12000, b'jpeg', self.cancel,
                            self.audio, self.transcript)

    def test_manual_turn_protocol_image_audio_and_no_tools(self):
        self.ask()
        sent = self.socket.sent
        session = sent[0]['session']
        self.assertIsNone(session['audio']['input']['turn_detection'])
        self.assertEqual(session['audio']['input']['format'], {'type': 'audio/pcm', 'rate': 24000})
        self.assertEqual(session['tools'], [])
        self.assertEqual(session['output_modalities'], ['audio'])
        self.assertEqual([e['type'] for e in sent], ['session.update', 'conversation.item.create',
            'input_audio_buffer.append', 'input_audio_buffer.commit', 'response.create'])
        self.assertEqual(sent[1]['item']['content'][0]['image_url'], 'data:image/jpeg;base64,anBlZw==')
        self.assertEqual(base64.b64decode(sent[2]['audio']), b'\x00\x01' * 12000)
        self.audio.assert_called_once_with(b'\x00\x01' * 2400)
        self.transcript.assert_called_with('A cup.', True)
        self.assertNotIn('test-secret', json.dumps(sent))

    def test_followup_reuses_session_and_cancel_resets_context(self):
        self.ask()
        self.ask()
        self.assertEqual(self.connect.call_count, 1)
        self.assertEqual(self.client.turns, 2)
        self.client.close()
        self.assertTrue(self.socket.closed)
        self.assertEqual(self.client.turns, 0)

    def test_text_and_image_use_same_audio_session_without_audio_commit(self):
        self.client.respond('test-secret', None, b'jpeg', self.cancel,
                            self.audio, self.transcript, prompt='What can you see?')
        sent = self.socket.sent
        self.assertEqual([e['type'] for e in sent],
                         ['session.update', 'conversation.item.create', 'response.create'])
        self.assertEqual(sent[1]['item']['content'][1],
                         {'type': 'input_text', 'text': 'What can you see?'})
        self.audio.assert_called_once()
        self.ask()
        self.assertEqual(self.connect.call_count, 1)
        self.assertEqual(self.client.turns, 2)

    def test_cancel_before_response_never_sends_question(self):
        self.cancel.set()
        with self.assertRaises(TalkError):
            self.ask()
        self.assertEqual(self.socket.sent, [])
        self.assertTrue(self.socket.closed)

    def test_api_error_does_not_leak_remote_text_or_credentials(self):
        self.socket.events = [{'type': 'error', 'error': {'code': 'invalid_api_key',
                                                       'message': 'test-secret'}}]
        with self.assertRaisesRegex(TalkError, 'key was rejected') as error:
            self.ask()
        self.assertNotIn('test-secret', str(error.exception))
        self.assertTrue(self.socket.closed)

    def test_transport_errors_are_sanitized(self):
        self.connect.side_effect = RuntimeError('Authorization: Bearer test-secret')
        with self.assertRaisesRegex(TalkError, 'Check internet') as error:
            self.ask()
        self.assertNotIn('test-secret', str(error.exception))

    def test_output_audio_is_bounded(self):
        original = self.socket.send
        def send(data):
            original(data)
            if json.loads(data)['type'] == 'response.create':
                self.socket.events = [{'type': 'response.output_audio.delta',
                    'delta': base64.b64encode(b'\x00' * (24000 * 2 * 30 + 2)).decode()}]
        self.socket.send = send
        with self.assertRaisesRegex(TalkError, '30-second'):
            self.ask()
        self.audio.assert_not_called()
        self.assertTrue(self.socket.closed)

    def test_failed_response_closes_context(self):
        with patch(__name__ + '.reply_events', return_value=[{'type': 'response.done', 'response': {'status': 'failed'}}]):
            with self.assertRaisesRegex(TalkError, 'did not finish'):
                self.ask()
        self.assertTrue(self.socket.closed)


class FakeHardwareTalk(Talk):
    def _configuration(self):
        return 'fake-audio', ''

    def _spawn(self, command, recording=False):
        # Real cancellable subprocess pipes, without ALSA hardware access.
        script = ('import os,time\nwhile True:\n os.write(1,b"\\0\\1"*1200); time.sleep(.05)'
                  if recording else 'import sys; sys.stdin.buffer.read()')
        return super()._spawn([sys.executable, '-c', script], recording)


class TalkTests(unittest.TestCase):
    def setUp(self):
        self.frames = Frames()
        self.frames.write(b'jpeg')
        self.audio_lock = threading.Lock()
        self.talk = FakeHardwareTalk(self.frames, self.audio_lock, key_file='/nonexistent-key')
        self.talk.client = Mock(model='mock-model', turns=0)

    def tearDown(self):
        self.talk.close()

    def finish_thread(self):
        self.talk.thread.join(3)
        self.assertFalse(self.talk.thread.is_alive())
        self.assertFalse(self.audio_lock.locked())
        self.assertIsNone(self.talk.process)

    def wait_recorded(self):
        time.sleep(.3)

    def test_no_key_disables_and_cannot_open_microphone(self):
        talk = Talk(self.frames, self.audio_lock, key_file='/nonexistent-key')
        with patch.dict('os.environ', {'OPENAI_API_KEY': ''}), patch('talk.websocket', True), patch('talk.subprocess.Popen') as popen:
            self.assertFalse(talk.status()['ready'])
            with self.assertRaisesRegex(ValueError, 'API key not configured'):
                talk.start(TOKEN)
            popen.assert_not_called()

    def test_release_streams_reply_then_releases_audio(self):
        def respond(key, pcm, jpeg, cancel, audio, transcript):
            self.assertGreaterEqual(len(pcm), 9600)
            self.assertEqual(jpeg, b'fresh-at-release')
            audio(b'\x00\x01' * 2400)
            transcript('A cup.', True)
        self.talk.client.respond.side_effect = respond
        self.talk.start(TOKEN)
        self.wait_recorded()
        self.frames.write(b'fresh-at-release')
        self.talk.command('finish', TOKEN)
        self.finish_thread()
        self.assertEqual(self.talk.status()['text'], 'A cup.')
        self.assertEqual(self.talk.status()['error'], '')
        self.talk.client.respond.assert_called_once()

    def test_cancel_before_start_rejects_late_start(self):
        self.talk.command('cancel', TOKEN)
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            self.talk.start(TOKEN)
        self.assertIsNone(self.talk.process)

    def test_text_question_never_records_and_plays_reply(self):
        def respond(key, pcm, jpeg, cancel, audio, transcript, prompt):
            self.assertIsNone(pcm)
            self.assertEqual(jpeg, b'jpeg')
            self.assertEqual(prompt, 'What is this?')
            audio(b'\x00\x01' * 2400)
            transcript('A cup.', True)
        self.talk.client.respond.side_effect = respond
        with patch.object(self.talk, '_record') as record:
            self.talk.send_text(TOKEN, '  What is this?  ')
            self.finish_thread()
            record.assert_not_called()
        self.assertEqual(self.talk.text, 'A cup.')
        self.assertEqual(self.talk.error, '')

    def test_invalid_text_does_not_start_worker_or_call_api(self):
        for prompt in (None, 1, [], '', '  ', 'x' * 501):
            with self.assertRaisesRegex(ValueError, '1–500'):
                self.talk.send_text(TOKEN, prompt)
        self.assertIsNone(self.talk.thread)
        self.assertFalse(self.audio_lock.locked())
        self.talk.client.respond.assert_not_called()

    def test_text_keeps_freshness_and_cancel_guards(self):
        self.frames.timestamp -= 5
        with self.assertRaisesRegex(ValueError, 'live video'):
            self.talk.send_text(TOKEN, 'Hello')
        self.frames.write(b'jpeg')
        self.talk.command('cancel', TOKEN)
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            self.talk.send_text(TOKEN, 'Hello')
        self.talk.client.respond.assert_not_called()

    def test_cancel_discards_recording_without_cloud_call(self):
        self.talk.start(TOKEN)
        self.wait_recorded()
        process = self.talk.process
        self.talk.command('cancel', TOKEN)
        self.finish_thread()
        self.talk.client.respond.assert_not_called()
        self.assertIsNotNone(process.poll())

    def test_browser_disconnect_stops_capture_without_cloud_call(self):
        self.talk.start(TOKEN)
        self.wait_recorded()
        self.talk.contact -= 3
        self.talk.tick()
        self.finish_thread()
        self.talk.client.respond.assert_not_called()
        self.assertIn('Connection lost', self.talk.error)

    def test_cancel_terminates_in_progress_playback(self):
        playing = threading.Event()
        def respond(key, pcm, jpeg, cancel, audio, transcript):
            audio(b'\x00\x01' * 2400)
            playing.set()
            cancel.wait(3)
            raise TalkError('Conversation cancelled.')
        self.talk.client.respond.side_effect = respond
        self.talk.start(TOKEN)
        self.wait_recorded()
        self.talk.command('finish', TOKEN)
        self.assertTrue(playing.wait(2))
        process = self.talk.process
        self.assertEqual(self.talk.phase, 'speaking')
        self.talk.command('cancel', TOKEN)
        self.finish_thread()
        self.assertIsNotNone(process.poll())
        self.talk.client.close.assert_called()

    def test_recording_limit_discards_without_cloud_call(self):
        self.talk.start(TOKEN)
        self.wait_recorded()
        self.talk.started -= 16
        self.talk.tick()
        self.finish_thread()
        self.talk.client.respond.assert_not_called()
        self.assertIn('15-second', self.talk.error)

    def test_stale_image_at_release_is_not_sent(self):
        self.talk.start(TOKEN)
        self.wait_recorded()
        self.frames.timestamp -= 5
        self.talk.command('finish', TOKEN)
        self.finish_thread()
        self.talk.client.respond.assert_not_called()
        self.assertIn('live video', self.talk.error)

    def test_stale_image_prevents_start(self):
        self.frames.timestamp -= 5
        with self.assertRaisesRegex(ValueError, 'live video'):
            self.talk.start(TOKEN)
        self.assertFalse(self.audio_lock.locked())

    def test_audio_exclusive_and_other_browser_cannot_finish(self):
        self.audio_lock.acquire()
        with self.assertRaisesRegex(ValueError, 'speaking'):
            self.talk.start(TOKEN)
        self.audio_lock.release()
        self.talk.start(TOKEN)
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.talk.start('another-recording-token')
        with self.assertRaisesRegex(ValueError, 'Recording ended'):
            self.talk.command('finish', 'another-recording-token')
        self.talk.command('cancel', 'another-recording-token')
        self.assertFalse(self.talk.cancelled.is_set())

    def test_idle_context_expires(self):
        self.talk.activity -= 121
        self.talk.tick()
        self.talk.client.close.assert_called_once()

    def test_status_never_exposes_key(self):
        with tempfile.TemporaryDirectory() as directory:
            keyfile = Path(directory) / 'key'
            keyfile.write_text('test-secret')
            talk = Talk(self.frames, self.audio_lock, key_file=keyfile)
            with patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
                self.assertEqual(talk._key(), 'test-secret')
                self.assertNotIn('test-secret', json.dumps(talk.status()))

    def test_http_protection_and_no_motor_commands(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http.controller = Controller(self.frames)
        http.talk = self.talk
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        def post(action, headers, prompt=None):
            request = Request(f'http://127.0.0.1:{http.server_port}/api/talk/{action}',
                data=json.dumps({'token': TOKEN, 'prompt': prompt}).encode(), headers=headers)
            try:
                response = urlopen(request, timeout=2)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, json.load(response)
        try:
            self.assertEqual(post('start', {})[0], 403)
            self.assertEqual(post('text', {}, 'Hello')[0], 403)
            self.assertEqual(post('text', {'X-Robot-Control': '1'}, ' ')[0], 409)
            self.assertEqual(post('start', {'X-Robot-Control': '1', 'Origin': 'https://other.example'})[0], 403)
            self.assertEqual(post('start', {'X-Robot-Control': '1'})[0], 200)
            self.assertEqual(post('cancel', {'X-Robot-Control': '1'})[0], 200)
            self.finish_thread()
            # A 500-character Unicode prompt fits the text endpoint's byte limit.
            with patch.object(self.talk, 'send_text', return_value={'accepted': True}) as send_text:
                self.assertEqual(post('text', {'X-Robot-Control': '1'}, '你' * 500)[0], 200)
                send_text.assert_called_once_with(TOKEN, '你' * 500)
            self.assertEqual(http.controller.command, 'stop')
            self.assertIsNone(http.controller.owner)
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
