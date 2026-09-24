"""Cloud announcement transport, cancellation and shared audio ownership."""
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

from cloud_speech import CloudSpeech, CloudSpeechError
from object_search import found_sentence
from speech import Speech


class CloudSpeechTests(unittest.TestCase):
    def test_request_uses_exact_sentence_and_marin_pcm(self):
        client = CloudSpeech()
        response = io.BytesIO(b'\x00\x00' * 24)
        response.status = 200
        connection = Mock()
        connection.getresponse.return_value = response
        with patch('cloud_speech.http.client.HTTPSConnection', return_value=connection):
            audio = client.synthesize('test-key', 'I found a red cup.', threading.Event())
        self.assertEqual(len(audio), 48)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('POST', '/v1/audio/speech'))
        body = json.loads(kwargs['body'])
        self.assertEqual(body['input'], 'I found a red cup.')
        self.assertEqual(body['voice'], 'marin')
        self.assertEqual(body['model'], 'gpt-4o-mini-tts')
        self.assertEqual(body['response_format'], 'pcm')
        self.assertIsNone(client.connection)
        connection.close.assert_called_once()

    def test_errors_are_sanitized_and_audio_is_bounded(self):
        for status, audio in [(401, b''), (429, b''), (500, b''), (200, b''),
                              (200, b'x'), (200, b'0' * (CloudSpeech.MAX_BYTES + 1))]:
            with self.subTest(status=status, size=len(audio)):
                response = io.BytesIO(audio)
                response.status = status
                connection = Mock()
                connection.getresponse.return_value = response
                with patch('cloud_speech.http.client.HTTPSConnection', return_value=connection):
                    with self.assertRaises(CloudSpeechError) as error:
                        CloudSpeech().synthesize('secret-key', 'A cup.', threading.Event())
                self.assertNotIn('secret-key', str(error.exception))

    def test_cancel_prevents_request_and_interrupts_open_socket(self):
        cancel = threading.Event()
        cancel.set()
        client = CloudSpeech()
        with patch('cloud_speech.http.client.HTTPSConnection') as connect:
            with self.assertRaises(CloudSpeechError):
                client.synthesize('key', 'A cup.', cancel)
            connect.assert_not_called()
        connection = client.connection = Mock()
        client.close()
        connection.sock.shutdown.assert_called_once()
        connection.close.assert_called_once()

    def test_announcement_has_one_sentence_and_verified_detail(self):
        text = found_sentence('a cup! More words.', 'on the right',
                              'A red ceramic cup sits on the desk. A second sentence!')
        self.assertEqual(text, 'I think I found a cup, on the right — A red ceramic cup sits on the desk.')
        self.assertEqual(text.count('.'), 1)
        self.assertNotIn('second sentence', text)
        self.assertIn('1.5 litre bottle', found_sentence('a 1.5 litre bottle',
                      'near the centre', 'A bottle is visible.'))


class AnnouncementPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.speech = Speech(None, '/unused')
        self.speech._configuration = Mock(return_value=('test-device', ''))
        self.speech.cloud = Mock()
        self.speech.cloud.synthesize.return_value = b'\x00\x00' * 24
        self.speech._command = Mock()

    def tearDown(self):
        self.speech.close()

    def test_cloud_speech_uses_shared_player_and_releases_audio(self):
        self.speech.say('I found a red cup.', api_key='test-key')
        self.speech.thread.join(2)
        self.assertFalse(self.speech.busy)
        self.assertFalse(self.speech.audio_lock.locked())
        command, pcm, _, _ = self.speech._command.call_args.args
        self.assertEqual(command[0], 'aplay')
        self.assertIn('24000', command)
        self.assertEqual(pcm, b'\x00\x00' * 24)

    def test_cancel_during_generation_prevents_late_playback(self):
        entered, release = threading.Event(), threading.Event()
        def synthesize(*args):
            entered.set()
            release.wait(2)
            return b'\x00\x00'
        self.speech.cloud.synthesize.side_effect = synthesize
        self.speech.say('I found a cup.', api_key='test-key')
        self.assertTrue(entered.wait(1))
        with self.assertRaises(ValueError):
            self.speech.say('Another announcement.', api_key='test-key')
        self.speech.stop()
        release.set()
        self.speech.thread.join(2)
        self.speech._command.assert_not_called()
        self.assertFalse(self.speech.audio_lock.locked())
        self.assertEqual(self.speech.phase, 'cancelled')

    def test_api_failure_is_visible_without_local_voice_fallback(self):
        self.speech.cloud.synthesize.side_effect = CloudSpeechError('Voice quota reached.')
        self.speech.say('I found a cup.', api_key='test-key')
        self.speech.thread.join(2)
        self.assertEqual(self.speech.error, 'Voice quota reached.')
        self.speech._command.assert_not_called()
        self.assertFalse(self.speech.audio_lock.locked())


if __name__ == '__main__':
    unittest.main()
