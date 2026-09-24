"""Detection geometry, freshness, worker isolation, and HTTP frame alignment."""
import json
import select
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from detection import Detection
from detection_worker import decode_objects
from server import Frames, Controller, Handler

WORKER = '''
import sys, struct, json
print(json.dumps({'ready': True}), flush=True)
while True:
    header=sys.stdin.buffer.read(4)
    if not header: break
    size=struct.unpack('!I', header)[0]
    sys.stdin.buffer.read(size)
    print(json.dumps({'objects': [], 'width': 640, 'height': 480, 'inference_ms': 1}), flush=True)
'''


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Detector did not reach the expected state')
        time.sleep(.01)


class DecodeTests(unittest.TestCase):
    def test_letterbox_coordinates_match_original_image(self):
        objects = decode_objects([[.1, .2, .6, .8]], [0], [.9], 1,
                                 {0: 'person'}, .5, (300, 300), (300, 225))
        self.assertEqual(objects[0]['box'], [.2, .13333, .8, .8])
        self.assertEqual(objects[0]['label'], 'person')

    def test_low_scores_padding_and_invalid_boxes_are_discarded(self):
        boxes = [[.1, .1, .3, .3], [.8, .1, .9, .3], [0, float('nan'), 1, 1], [0, 0, 1, 1]]
        objects = decode_objects(boxes, [0]*4, [.4, .9, .9, .8], 4,
                                 {0: 'person'}, .5, (300, 300), (300, 225))
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]['box'], [0, 0, 1, 1])


class WorkerTests(unittest.TestCase):
    def worker(self, script=WORKER, payload=b'first JPEG'):
        frames = Frames(); frames.write(payload)
        detector = Detection(frames, '', '', '')
        detector.command = [sys.executable, '-u', '-c', script]
        self.addCleanup(detector.close)
        detector.start()
        return detector

    def test_pause_reaps_worker_clears_results_and_resume_uses_new_frames(self):
        detector = self.worker()
        wait_for(lambda: detector.snapshot()[1]['live'])
        old = detector.process
        status = detector.set_enabled(False)
        self.assertFalse(status['enabled'])
        self.assertEqual(status['objects'], [])
        wait_for(lambda: detector.process is None)
        self.assertIsNotNone(old.poll())
        detector.frames.write(b'camera while paused')
        time.sleep(.25)
        self.assertIsNone(detector.process)
        self.assertIsNone(detector.snapshot()[0])
        detector.set_enabled(True)
        wait_for(lambda: detector.snapshot()[1]['live'])
        self.assertNotEqual(detector.process.pid, old.pid)
        self.assertEqual(detector.snapshot()[0]['jpeg'], b'camera while paused')
        current = detector.process
        detector.set_enabled(True)
        self.assertIs(detector.process, current)

    def test_pause_during_startup_and_immediate_resume_do_not_reuse_worker(self):
        detector = self.worker('import time; time.sleep(30)')
        wait_for(lambda: detector.process is not None)
        old = detector.process
        detector.set_enabled(False)
        detector.command = [sys.executable, '-u', '-c', WORKER]
        detector.set_enabled(True)
        wait_for(lambda: detector.snapshot()[1]['live'])
        self.assertIsNotNone(old.poll())
        self.assertNotEqual(detector.process.pid, old.pid)
        self.assertEqual(detector.error, '')

    def test_pause_interrupts_pending_inference_and_close_while_paused_exits(self):
        detector = self.worker("import time; print('{\"ready\":true}', flush=True); time.sleep(30)")
        wait_for(lambda: detector.process is not None)
        old = detector.process
        time.sleep(.1)
        detector.set_enabled(False)
        wait_for(lambda: detector.process is None)
        self.assertIsNotNone(old.poll())
        self.assertEqual(detector.snapshot()[1]['error'], '')
        detector.close()
        self.assertFalse(detector.thread.is_alive())
        with self.assertRaises(ValueError): detector.set_enabled(True)

    def test_resume_interrupts_error_retry_delay(self):
        detector = self.worker('raise SystemExit(1)')
        wait_for(lambda: detector.error.startswith('Coral worker stopped'))
        detector.set_enabled(False)
        detector.command = [sys.executable, '-u', '-c', WORKER]
        detector.set_enabled(True)
        wait_for(lambda: detector.snapshot()[1]['live'])
        for value in (None, 'false', 0, 1):
            with self.assertRaises(ValueError): detector.set_enabled(value)

    def test_pause_kills_unresponsive_worker_even_with_full_input_pipe(self):
        writing = threading.Event()
        real_select = select.select
        def selecting(readers, writers, errors, timeout):
            if writers: writing.set()
            return real_select(readers, writers, errors, timeout)
        with patch('detection.select.select', side_effect=selecting):
            detector = self.worker("""
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('{"ready":true}', flush=True)
time.sleep(30)
""", payload=b'x' * 128000)
            self.assertTrue(writing.wait(2))
            old = detector.process
            detector.set_enabled(False)
            wait_for(lambda: detector.process is None)
            self.assertIsNotNone(old.poll())
            self.assertFalse(detector.snapshot()[1]['running'])

    def test_stale_results_clear_objects(self):
        detector = Detection(Frames(), '', '', '', clock=lambda: 10)
        detector.error = ''
        detector.result = {'frame_time': 9.5, 'frame': 7, 'jpeg': b'jpeg',
                           'objects': [{'label': 'person'}], 'inference_ms': 8}
        self.assertTrue(detector.snapshot()[1]['live'])
        detector.clock = lambda: 11
        frame, status = detector.snapshot()
        self.assertIsNone(frame)
        self.assertFalse(status['live'])
        self.assertEqual(status['objects'], [])

    def test_worker_preserves_source_frame_and_shuts_down(self):
        frames = Frames()
        frames.write(b'first JPEG')
        timestamp = frames.timestamp
        detector = Detection(frames, '', '', '')
        detector.command = [sys.executable, '-u', '-c', '''
import sys, struct, json
print(json.dumps({'ready': True}), flush=True)
while True:
    header=sys.stdin.buffer.read(4)
    if not header: break
    size=struct.unpack('!I', header)[0]
    data=sys.stdin.buffer.read(size)
    print(json.dumps({'objects': [], 'width': 640, 'height': 480, 'inference_ms': 1}), flush=True)
''']
        detector.start()
        try:
            deadline = time.monotonic() + 3
            while not detector.snapshot()[1]['live'] and time.monotonic() < deadline:
                time.sleep(.01)
            result, status = detector.snapshot()
            self.assertTrue(status['live'], status)
            frames.write(b'newer JPEG')
            self.assertEqual(result['jpeg'], b'first JPEG')
            self.assertEqual(result['frame_time'], timestamp)
            self.assertEqual(result['frame'], 1)
        finally:
            detector.close()
        self.assertFalse(detector.thread.is_alive())

    def test_missing_worker_reports_error_without_touching_camera(self):
        frames = Frames()
        frames.write(b'camera remains live')
        detector = Detection(frames, '/no-such-coral-python', '', '')
        detector.start()
        try:
            deadline = time.monotonic() + 2
            while detector.error.startswith('Starting') and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn('No such file', detector.snapshot()[1]['error'])
            self.assertEqual(frames.latest()[0], b'camera remains live')
        finally:
            detector.close()


class DetectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.frames = Frames()
        self.frames.write(b'new raw image')
        self.detector = Detection(self.frames, '', '', '')
        self.detector.error = ''
        self.detector.result = {'jpeg': b'analysed image', 'frame_time': time.monotonic() - .1,
                               'frame': 42, 'objects': [{'label': 'cup', 'score': .8, 'box': [0, 0, 1, 1]}],
                               'width': 640, 'height': 480, 'inference_ms': 7}
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.controller = Controller(self.frames)
        self.http.detector = self.detector
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def get(self, path):
        return urlopen(f'http://127.0.0.1:{self.http.server_port}{path}', timeout=2)

    def test_detection_frame_and_metadata_are_from_the_same_capture(self):
        with self.get('/api/frame?detect=1') as response:
            self.assertEqual(response.read(), b'analysed image')
            self.assertEqual(response.headers['X-Frame-Number'], '42')
            self.assertEqual(response.headers['X-Detection-State'], 'live')
            self.assertEqual(json.loads(response.headers['X-Detections'])['objects'][0]['label'], 'cup')
        with self.get('/api/frame') as response:
            self.assertEqual(response.read(), b'new raw image')
        self.assertEqual(self.http.controller.command, 'stop')

    def test_detector_failure_falls_back_to_raw_video_without_old_boxes(self):
        self.detector.error = 'USB disconnected'
        with self.get('/api/frame?detect=1') as response:
            self.assertEqual(response.read(), b'new raw image')
            self.assertEqual(response.headers['X-Detection-State'], 'unavailable')
            self.assertIsNone(response.headers.get('X-Detections'))
        with self.get('/api/detections') as response:
            status = json.load(response)
            self.assertFalse(status['live'])
            self.assertEqual(status['objects'], [])

    def test_toggle_requires_same_origin_and_boolean_and_preserves_raw_camera(self):
        def post(body, headers):
            request = Request(f'http://127.0.0.1:{self.http.server_port}/api/detections',
                              data=json.dumps(body).encode(), headers=headers)
            try: response = urlopen(request, timeout=2)
            except HTTPError as exc: response = exc
            with response: return response.status, json.load(response)
        self.assertEqual(post({'enabled': False}, {})[0], 403)
        self.assertEqual(post({'enabled': False}, {'X-Robot-Control': '1', 'Origin': 'http://elsewhere'})[0], 403)
        for value in (None, 'false', 0):
            self.assertEqual(post({'enabled': value}, {'X-Robot-Control': '1'})[0], 409)
        code, status = post({'enabled': False}, {'X-Robot-Control': '1'})
        self.assertEqual(code, 200)
        self.assertFalse(status['enabled'])
        with self.get('/api/frame?detect=1') as response:
            self.assertEqual(response.read(), b'new raw image')
            self.assertIsNone(response.headers.get('X-Detections'))
        with self.get('/api/status') as response:
            self.assertFalse(json.load(response)['detection']['enabled'])
        # GETs from another viewer never turn detection back on.
        self.assertFalse(self.detector.enabled)
        code, status = post({'enabled': True}, {'X-Robot-Control': '1'})
        self.assertEqual(code, 200)
        self.assertTrue(status['enabled'])
        self.assertFalse(status['live'])
        self.assertEqual(self.http.controller.command, 'stop')


if __name__ == '__main__':
    unittest.main()
