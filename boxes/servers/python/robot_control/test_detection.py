"""Detection geometry, freshness, worker isolation, and HTTP frame alignment."""
import json
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from detection import Detection
from detection_worker import decode_objects
from server import Frames, Controller, Handler


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


if __name__ == '__main__':
    unittest.main()
