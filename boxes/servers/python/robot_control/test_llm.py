import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from llm import LocalLLM, allowed_descriptions
from speech import Speech


class LLMTests(unittest.TestCase):
    def test_closed_vocabulary_preserves_counts_and_no_extra_attributes(self):
        choices = allowed_descriptions(['2 people', 'a laptop'])
        self.assertEqual(len(choices), 6)
        self.assertIn('I can see a laptop and 2 people.', choices)
        self.assertNotIn('I can see a person and a laptop.', choices)
        self.assertNotIn('I can see 2 people and a red laptop.', choices)
        self.assertEqual(allowed_descriptions([]), [])

    def model(self, directory):
        model = LocalLLM(directory)
        for path in (model.runtime, model.model):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return model

    def test_real_validation_rejects_invented_truncated_and_count_mismatches(self):
        for answer, reason in [('I can see a red cup.', 'stop'), ('I can see 2 cups.', 'stop'),
                               ('I can see a cup.', 'length')]:
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                model = self.model(directory)
                process = Mock()
                process.poll.return_value = None
                responses = [{'status': 'ok'}, {'choices': [{'message': {'content': answer}, 'finish_reason': reason}]}]
                with patch('llm.subprocess.Popen', return_value=process), patch('llm.request', side_effect=responses):
                    with self.assertRaisesRegex(ValueError, 'did not match'):
                        model.generate(['a cup'], threading.Event(), lambda phase: None)
                process.terminate.assert_called_once()
                self.assertIsNone(model.process)

    def test_valid_generation_uses_grammar_and_releases_model(self):
        with tempfile.TemporaryDirectory() as directory:
            model = self.model(directory)
            process = Mock()
            process.poll.return_value = None
            phases = []
            responses = [{'status': 'ok'}, {'choices': [{'message': {'content': 'I can see a cup.'}, 'finish_reason': 'stop'}]}]
            with patch('llm.subprocess.Popen', return_value=process), patch('llm.request', side_effect=responses) as request:
                text = model.generate(['a cup'], threading.Event(), phases.append)
                self.assertIn('root ::=', request.call_args.args[2]['grammar'])
            self.assertEqual(text, 'I can see a cup.')
            self.assertEqual(phases, ['loading', 'thinking'])
            process.terminate.assert_called_once()
            self.assertFalse(list(Path(directory).glob('web-*')))

    def test_cancel_before_loading_never_starts_process(self):
        with tempfile.TemporaryDirectory() as directory:
            model = self.model(directory)
            cancel = threading.Event()
            cancel.set()
            with patch('llm.subprocess.Popen') as start:
                with self.assertRaises(InterruptedError):
                    model.generate(['a cup'], cancel, lambda phase: None)
                start.assert_not_called()


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.result = {'objects': [{'label': 'cup', 'score': .8}]}
        self.detector = Mock()
        self.detector.snapshot.side_effect = lambda: (self.result, {})
        self.llm = Mock()
        self.llm.generate.return_value = 'I can see a cup.'
        self.speech = Speech(self.detector, '/fake', llm=self.llm)
        self.speech._configuration = lambda: ('test', '')
        self.spoken = []
        self.speech._speak = lambda text, device, cancel: self.spoken.append(text)

    def tearDown(self):
        self.speech.close()

    def finish(self):
        self.speech.describe(True)
        self.speech.thread.join(2)
        self.assertFalse(self.speech.busy)

    def test_llm_speech_and_basic_opt_out(self):
        self.finish()
        self.assertEqual(self.spoken, ['I can see a cup.'])
        self.assertEqual(self.speech.status()['source'], 'llm')
        self.speech.describe(False)
        self.speech.thread.join(2)
        self.assertEqual(self.spoken[-1], 'I think I can see a cup.')
        self.llm.generate.assert_called_once()

    def test_timeout_uses_basic_description(self):
        self.llm.generate.side_effect = TimeoutError('Local model timed out')
        self.finish()
        self.assertEqual(self.spoken, ['I think I can see a cup.'])
        self.assertEqual(self.speech.status()['source'], 'fallback')

    def test_changed_scene_uses_new_facts_and_stale_scene_does_not_speak(self):
        def change(*args):
            self.result = {'objects': [{'label': 'bottle', 'score': .9}]}
            return 'I can see a cup.'
        self.llm.generate.side_effect = change
        self.finish()
        self.assertEqual(self.spoken, ['I think I can see a bottle.'])
        self.assertEqual(self.speech.status()['source'], 'fallback')
        def stale(*args):
            self.result = None
            return 'I can see a bottle.'
        self.llm.generate.side_effect = stale
        self.finish()
        self.assertEqual(len(self.spoken), 1)
        self.assertIn('paused', self.speech.status()['error'])

    def test_empty_scene_skips_llm(self):
        self.result = {'objects': []}
        self.finish()
        self.llm.generate.assert_not_called()
        self.assertIn('not sure', self.spoken[0])

    def test_cancel_during_generation_never_speaks(self):
        entered = threading.Event()
        def wait(parts, cancel, progress):
            entered.set()
            cancel.wait(2)
            raise InterruptedError()
        self.llm.generate.side_effect = wait
        self.speech.describe(True)
        self.assertTrue(entered.wait(1))
        self.speech.stop()
        self.speech.thread.join(2)
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.speech.phase, 'cancelled')


if __name__ == '__main__':
    unittest.main()
