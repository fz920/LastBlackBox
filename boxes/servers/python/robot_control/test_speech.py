"""Descriptions, fresh-frame gating, cancellation, and speech HTTP isolation."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from detection import Detection
from server import Controller, Frames, Handler
from speech import Speech, describe_objects, mouth_device


def objects(*labels):
    return [{'label': label, 'score': .8} for label in labels]


class DescriptionTests(unittest.TestCase):
    def test_counts_plurals_and_short_sentences(self):
        self.assertEqual(describe_objects(objects('person', 'cup', 'cup')),
                         'I think I can see a person and 2 cups.')
        self.assertEqual(describe_objects(objects('person', 'person', 'knife', 'knife')),
                         'I think I can see 2 people and 2 knives.')
        self.assertEqual(describe_objects(objects('umbrella', 'scissors', 'cup', 'chair')),
                         'I think I can see an umbrella, a pair of scissors and a cup.')

    def test_no_detection_does_not_claim_empty_or_safe_scene(self):
        text = "I'm not sure what I'm looking at yet."
        self.assertEqual(describe_objects([]), text)
        self.assertEqual(describe_objects([{'label': 'person', 'score': .4}]), text)

    def test_device_selection_uses_name_and_never_falls_back_to_hdmi(self):
        with tempfile.TemporaryDirectory() as directory:
            cards = Path(directory) / 'cards'
            cards.write_text(' 0 [vc4hdmi0 ]: HDMI\n 2 [Headphones ]: Headphones\n')
            self.assertIsNone(mouth_device(cards))
            cards.write_text(cards.read_text() + ' 4 [MAX98357A      ]: simple-card\n')
            self.assertEqual(mouth_device(cards), 'plughw:CARD=MAX98357A,DEV=0')


class SilentSpeech(Speech):
    """Exercise real process management using a cancellable silent process."""
    def _configuration(self):
        return 'test-device', ''

    def _command(self, command, data, timeout, cancel):
        if command[0] == 'aplay':
            return super()._command([sys.executable, '-c', 'import time; time.sleep(10)'],
                                    b'', 12, cancel)
        self.synthesized = data.decode()
        return b'fake audio'


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.frames = Frames()
        self.frames.write(b'jpeg')
        self.detector = Detection(self.frames, '', '', '')
        self.detector.error = ''
        self.detector.result = {'objects': objects('cup'), 'frame': 1,
                               'frame_time': time.monotonic(), 'inference_ms': 1}
        self.speech = SilentSpeech(self.detector, '/fake-engine')

    def tearDown(self):
        self.speech.close()

    def test_busy_does_not_queue_and_stop_terminates_playback(self):
        state = self.speech.describe()
        self.assertTrue(state['speaking'])
        with self.assertRaisesRegex(ValueError, 'Already speaking'):
            self.speech.describe()
        deadline = time.monotonic() + 2
        while self.speech.process is None and time.monotonic() < deadline:
            time.sleep(.01)
        process = self.speech.process
        self.assertIsNotNone(process)
        self.speech.stop()
        self.speech.thread.join(2)
        self.assertFalse(self.speech.thread.is_alive())
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.speech.status()['speaking'])
        self.assertEqual(self.speech.status()['error'], '')
        self.assertEqual(self.speech.synthesized, 'I think I can see a cup.')

    def test_stale_and_failed_detection_rejected(self):
        self.detector.result['frame_time'] -= 5
        with self.assertRaisesRegex(ValueError, 'fresh Coral'):
            self.speech.describe()
        self.detector.result['frame_time'] = time.monotonic()
        self.detector.error = 'USB disconnected'
        with self.assertRaisesRegex(ValueError, 'fresh Coral'):
            self.speech.describe()
        self.assertFalse(self.speech.busy)

    def test_paused_detection_does_not_speak_old_objects_or_resume_npu(self):
        self.detector.set_enabled(False)
        with self.assertRaisesRegex(ValueError, 'Coral is paused'):
            self.speech.describe()
        self.assertFalse(self.speech.busy)
        self.assertFalse(self.detector.enabled)

    def test_playback_error_visible_and_allows_retry(self):
        with patch.object(self.speech, '_command', side_effect=OSError('Audio device busy')):
            self.speech.describe()
            self.speech.thread.join(2)
        self.assertFalse(self.speech.status()['speaking'])
        self.assertIn('Audio device busy', self.speech.status()['error'])

    def test_missing_engine_reports_actionable_error(self):
        speech = Speech(self.detector, '/missing-engine')
        self.assertFalse(speech.status()['ready'])
        with self.assertRaisesRegex(ValueError, 'setup-speech.sh'):
            speech.describe()

    def test_http_speech_is_protected_and_does_not_claim_or_drive(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http.controller = Controller(self.frames)
        http.speech = self.speech
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{http.server_port}'
        def post(path, headers):
            request = Request(base + path, data=b'{}', headers=headers)
            try:
                response = urlopen(request, timeout=2)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, json.load(response)
        try:
            self.assertEqual(post('/api/speech/describe', {})[0], 403)
            self.assertEqual(post('/api/speech/describe', {'X-Robot-Control': '1', 'Origin': 'https://other.example'})[0], 403)
            code, state = post('/api/speech/describe', {'X-Robot-Control': '1'})
            self.assertEqual(code, 200)
            self.assertTrue(state['speaking'])
            self.assertEqual(state['text'], 'I think I can see a cup.')
            self.assertEqual(post('/api/speech/stop', {'X-Robot-Control': '1'})[0], 200)
            self.assertEqual(http.controller.command, 'stop')
            self.assertIsNone(http.controller.owner)
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
