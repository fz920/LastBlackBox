"""Memory checks with generated images, fake API responses and simulated wheels."""
import io
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from PIL import Image, ImageDraw
from search_memory import MAX_VIEWS, SearchMemory, image_features, similar
from server import Handler
import test_search
from test_search import result


def picture(colour='red', side='left'):
    image = Image.new('RGB', (320, 240), 'white')
    draw = ImageDraw.Draw(image)
    x = 15 if side == 'left' else 190
    draw.rectangle((x, 20, x + 110, 210), fill=colour)
    output = io.BytesIO()
    image.save(output, 'JPEG')
    return output.getvalue()


class MemoryTests(unittest.TestCase):
    def test_similar_views_but_not_colour_position_blank_or_invalid_images(self):
        red = image_features(picture())
        self.assertTrue(similar(red, image_features(picture()), strict=True))
        self.assertFalse(similar(red, image_features(picture('blue'))))
        self.assertFalse(similar(red, image_features(picture(side='right'))))
        white = image_features(picture('white'))
        self.assertFalse(similar(white, white))
        self.assertIsNone(image_features(b'not a jpeg'))
        self.assertFalse(similar(None, red))

    def test_revisit_links_actual_departures_without_inventing_a_map(self):
        memory = SearchMemory()
        a, b = picture(), picture('blue')
        memory.observe(a, time.monotonic(), result(), 'looking')
        memory.action('right', .3)
        memory.observe(b, time.monotonic(), result(), 'looking')
        memory.action('left', .12, interrupted=True)
        memory.observe(a, time.monotonic(), result(), 'looking')
        context = memory.context(a)
        self.assertEqual(context['similar_to_view'], 1)
        self.assertEqual(context['previous_checks_here'], 2)
        self.assertEqual(context['recent_views'][0]['departures'][0]['action'], 'right')
        self.assertEqual(context['recent_views'][0]['next_view'], 2)
        self.assertEqual(memory.entries[1]['departures'][0]['outcome'], 'interrupted')
        self.assertEqual(memory.status()['repeats'], 1)
        self.assertEqual(memory.status()['views'], 2)
        self.assertNotIn('proposed_actions', json.dumps(context))
        self.assertNotIn('thumbnail', json.dumps(context))
        self.assertNotIn('pixels', json.dumps(context))

    def test_stationary_verification_is_not_a_revisit_and_memory_is_bounded(self):
        memory = SearchMemory()
        for _ in range(MAX_VIEWS + 3):
            memory.observe(picture(), time.monotonic(), result('match'), 'verifying')
        self.assertEqual(memory.status()['repeats'], 0)
        self.assertEqual(len(memory.entries), MAX_VIEWS)
        self.assertIsNone(memory.thumbnail(1))
        last = memory.entries[-1]['id']
        with Image.open(io.BytesIO(memory.thumbnail(last))) as thumbnail:
            self.assertLessEqual(thumbnail.width, 160)
            self.assertLessEqual(thumbnail.height, 120)
        self.assertLess(len(json.dumps(memory.context(picture()))), 2500)
        memory.clear()
        self.assertIsNone(memory.thumbnail(last))
        memory.observe(picture(), time.monotonic(), result(), 'looking')
        self.assertGreater(memory.entries[-1]['id'], last, 'Old thumbnail URLs must not resolve to new views')


class MemoryIntegrationTests(unittest.TestCase):
    setUp = test_search.SearchTests.setUp
    tearDown = test_search.SearchTests.tearDown
    start = test_search.SearchTests.start
    finish = test_search.SearchTests.finish
    movements = test_search.SearchTests.movements

    def real_frames(self):
        write = self.frames.write
        jpeg = picture()
        patcher = patch.object(self.frames, 'write', side_effect=lambda _: write(jpeg))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.frames.write(jpeg)

    def test_repeated_view_context_guides_fresh_decisions_and_resets_next_search(self):
        self.real_frames()
        contexts = []
        def inspect(*args, context):
            contexts.append(context)
            # Simulated planner chooses a new direction after seeing its old departure.
            memory = context['search_memory']
            action = 'left' if memory['similar_to_view'] else 'right'
            return {**result(), 'actions': [action]}
        self.vision.inspect.side_effect = inspect
        self.start(); self.finish()
        self.assertEqual([e['completed_action'] for e in self.search.history if 'completed_action' in e],
                         ['right', 'left'])
        repeated = contexts[1]['search_memory']
        self.assertEqual(repeated['similar_to_view'], 1)
        self.assertEqual(repeated['recent_views'][0]['departures'][0]['action'], 'right')
        self.assertEqual(self.search.memory.status()['repeats'], 2)
        old_check = self.search.memory.entries[-1]['id']
        contexts.clear()
        self.start(token=test_search.TOKEN + 'new', target='a cup'); self.finish()
        self.assertEqual(contexts[0]['search_memory']['recent_views'], [])
        self.assertIsNone(self.search.memory.thumbnail(old_check))

    def test_matching_memory_never_replaces_two_fresh_confirmation_calls(self):
        self.real_frames()
        self.vision.inspect.return_value = result('match')
        self.start(); self.finish()
        self.assertEqual(self.vision.inspect.call_count, 2)
        self.assertTrue(self.search.centered)
        self.assertEqual(self.search.memory.status()['repeats'], 0)
        self.assertEqual(len(self.search.memory.entries), 2)
        self.assertEqual(self.movements(), [])

    def test_clear_blocked_during_request_and_cancelled_reply_never_repopulates(self):
        self.real_frames()
        entered, release = threading.Event(), threading.Event()
        def inspect(*args, **kwargs):
            entered.set(); release.wait(2)
            return result()
        self.vision.inspect.side_effect = inspect
        self.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(ValueError, 'Stop'):
            self.search.clear_memory()
        self.search.cancel()
        release.set(); self.finish()
        self.assertEqual(self.search.memory.status()['entries'], [])
        self.search.clear_memory()
        self.assertEqual(self.movements(), [])

    def test_memory_routes_thumbnail_expiry_and_post_protection(self):
        self.search.memory.observe(picture(), time.monotonic(), result(), 'looking')
        check = self.search.memory.entries[-1]['id']
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http.search, http.controller = self.search, self.robot
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{http.server_port}'
        try:
            with urlopen(base + f'/api/search/memory/frame?check={check}') as response:
                self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(response.read(), self.search.memory.thumbnail(check))
            for headers in ({}, {'X-Robot-Control': '1', 'Origin': 'http://evil.example'}):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + '/api/search/memory/clear', data=b'{}', headers=headers))
                self.assertEqual(error.exception.code, 403)
            with urlopen(Request(base + '/api/search/memory/clear', data=b'{}',
                                 headers={'X-Robot-Control': '1'})) as response:
                self.assertEqual(json.load(response)['memory']['entries'], [])
            for query in (str(check), '-1', 'invalid'):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + '/api/search/memory/frame?check=' + query)
                self.assertEqual(error.exception.code, 404)
        finally:
            http.shutdown(); http.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
