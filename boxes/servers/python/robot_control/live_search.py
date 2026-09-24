"""Bounded Realtime image feed; this transport never sends motor commands."""
import base64
from collections import deque
import json
import threading
import time
from urllib.parse import quote

from realtime import websocket
from search_memory import image_features, similar
from search_vision import INSTRUCTIONS, SCHEMA, SearchError, validate_result

IMAGE_RATES = (0.5, 1, 2, 4)
MAX_MONITOR_CHECKS = 20
RESPONSE_TIMEOUT = 8
MAX_IMAGE_AGE = 1.2
MONITOR_RECHECK_SECONDS = 1.0


def validate_live_settings(live, image_rate):
    if type(live) is not bool:
        raise ValueError('Live search must be true or false.')
    if type(image_rate) not in (int, float) or image_rate not in IMAGE_RATES:
        raise ValueError('Choose 0.5, 1, 2 or 4 images per second.')


class LiveSearchVision:
    """One socket owner, one in-flight response, one waiting foreground request.

    Upload cadence is independent of response latency. Old images are deleted
    from the remote session, except the one pinned by an in-flight assessment.
    Results are tied to the exact frame and plan generation that produced them.
    """
    def __init__(self, source, observation, *, image_rate=2, model='gpt-realtime', connect=None,
                 context_provider=None):
        validate_live_settings(True, image_rate)
        self.source, self.observation = source, observation
        self.context_provider = context_provider
        self.image_rate, self.model = image_rate, model
        self.connect = connect or (websocket.create_connection if websocket else None)
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.ws = self.thread = self.pending = None
        self.error = ''
        self.images = self.assessments = self.monitor_checks = 0
        self.sent_times = deque(maxlen=40)
        self.latency = None
        self.connected = False
        self.skipped_monitor_frames = 0

    def status(self):
        with self.lock:
            times = list(self.sent_times)
            rate = ((len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0)
            if not self.connected or not times or time.monotonic() - times[-1] > max(2.5, 2 / self.image_rate):
                rate = 0
            return {'connected': self.connected, 'images_sent': self.images,
                    'assessments': self.assessments, 'monitor_checks': self.monitor_checks,
                    'max_monitor_checks': MAX_MONITOR_CHECKS, 'actual_image_rate': round(rate, 1),
                    'skipped_monitor_frames': self.skipped_monitor_frames,
                    'latency': self.latency, 'error': self.error}

    def close(self):
        # Nonblocking: the caller may own the motor lock needed by source().
        self.closed.set()
        with self.lock:
            ws, self.ws = self.ws, None
            self.connected = False
        if ws:
            try:
                ws.shutdown()
            except Exception:
                pass

    def inspect_frame(self, key, target, cancelled, *, context):
        job = {'target': target, 'context': context, 'done': threading.Event()}
        with self.lock:
            if self.closed.is_set() or self.error:
                raise SearchError(self.error or 'Live search closed.')
            if self.pending:
                raise SearchError('An image assessment is already waiting.')
            self.pending = job
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, args=(key, cancelled),
                                               name='robot-live-vision', daemon=True)
                self.thread.start()
        deadline = time.monotonic() + 18
        while not job['done'].wait(.04):
            with self.lock:
                error = self.error
            if error or cancelled.is_set() or self.closed.is_set() or time.monotonic() > deadline:
                self.close()
                raise SearchError(error or 'Live image assessment cancelled or timed out.')
        if cancelled.is_set() or self.closed.is_set():
            raise SearchError('Live search cancelled.')
        return job['result'], job['frame']['jpeg'], job['frame']['timestamp']

    @staticmethod
    def _send(ws, kind, **fields):
        ws.send(json.dumps({'type': kind, **fields}))

    @staticmethod
    def _result(response):
        output = response.get('output', [])
        if response.get('status') != 'completed' or len(output) != 1:
            raise SearchError('Live image assessment did not complete. Search stopped.')
        item = output[0]
        if item.get('type') == 'function_call' and item.get('name') == 'assess_view':
            arguments = item.get('arguments', '')
        elif item.get('type') == 'message':
            # Realtime may return the assessment as JSON text. Apply exactly the
            # same strict local validator; never interpret prose as a motor command.
            parts = item.get('content', [])
            if not parts or any(p.get('type') not in ('text', 'output_text') for p in parts):
                raise SearchError('Live image assessment was invalid. Search stopped.')
            arguments = ''.join(p.get('text', '') for p in parts)
        else:
            raise SearchError('Live image assessment was invalid. Search stopped.')
        if not isinstance(arguments, str) or len(arguments) > 4096:
            raise SearchError('Live image assessment was invalid. Search stopped.')
        try:
            return validate_result(json.loads(arguments))
        except (ValueError, TypeError):
            raise SearchError('Live image assessment was invalid. Search stopped.') from None

    def _run(self, key, cancelled):
        ws = None
        try:
            if not self.connect:
                raise SearchError('Run setup-realtime.sh on the Pi first.')
            ws = self.connect('wss://api.openai.com/v1/realtime?model=' + quote(self.model, safe=''),
                              header={'Authorization': 'Bearer ' + key}, timeout=4,
                              enable_multithread=True, suppress_origin=True)
            with self.lock:
                if self.closed.is_set() or cancelled.is_set():
                    return
                self.ws = ws
            ws.settimeout(.05)
            self._send(ws, 'session.update', session={
                'type': 'realtime', 'output_modalities': ['text'], 'instructions': INSTRUCTIONS + '\n' + (
                    'This session also receives frames captured during movement. For monitor_only checks, '
                    'report the target evidence and choose inspect, or stop for a visible hazard. '
                    'Never propose movement while monitoring. A possible match will be rechecked after stopping.'),
                'audio': {'input': {'turn_detection': None}}, 'max_output_tokens': 450,
                'tools': [{'type': 'function', 'name': 'assess_view',
                           'description': 'Report visual evidence and a proposed plan; this does not move hardware.',
                           'parameters': SCHEMA}],
                'tool_choice': {'type': 'function', 'name': 'assess_view'}})
            ready, next_send, inflight, latest = False, 0, None, None
            startup_deadline = time.monotonic() + RESPONSE_TIMEOUT
            remote = []
            last_number = None
            last_monitor = None
            while not self.closed.is_set() and not cancelled.is_set():
                now = time.monotonic()
                if (not ready and now > startup_deadline) or (inflight and now > inflight['deadline']):
                    raise SearchError('Live image API timed out. Search stopped.')
                if ready and now >= next_send:
                    # Sample only now, never accumulate an unsent frame queue.
                    frame = self.source()
                    if frame is None:
                        return
                    if not 0 <= time.monotonic() - frame['timestamp'] <= MAX_IMAGE_AGE:
                        raise SearchError('Live camera paused. Search stopped.')
                    if frame['number'] != last_number:
                        with self.lock:
                            number = self.images + 1
                        item_id = 'liveframe_' + str(number)
                        self._send(ws, 'conversation.item.create', item={
                            'id': item_id, 'type': 'message', 'role': 'user', 'content': [
                                {'type': 'input_text', 'text': json.dumps({'frame': number,
                                    'phase': frame['phase'], 'moving': frame['moving']})},
                                {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,' +
                                 base64.b64encode(frame['jpeg']).decode('ascii')}]})
                        frame = {**frame, 'id': item_id}
                        remote.append(item_id)
                        latest, last_number = frame, frame['number']
                        with self.lock:
                            self.images += 1
                            self.sent_times.append(time.monotonic())
                    next_send = time.monotonic() + 1 / self.image_rate
                with self.lock:
                    pending = self.pending
                if ready and not inflight and latest and 0 <= now - latest['timestamp'] <= MAX_IMAGE_AGE:
                    job = None
                    # A foreground request needs a frame sampled after the request's boundary.
                    if pending and latest['timestamp'] > pending['context']['after'] and not latest['moving']:
                        job = pending
                        with self.lock:
                            self.pending = None
                    monitor = not pending and latest['monitor']
                    # Only defer near-identical stopped/settling views after an
                    # absent result. Moving, changed, uncertain, new-epoch and
                    # foreground checks are never suppressed by memory.
                    if (monitor and not latest['moving'] and latest['phase'] == 'settling'
                            and last_monitor and last_monitor['epoch'] == latest['epoch']
                            and 0 <= now - last_monitor['timestamp'] < MONITOR_RECHECK_SECONDS
                            and similar(image_features(latest['jpeg']), last_monitor['features'], strict=True)):
                        latest = {**latest, 'monitor': False}
                        monitor = False
                        with self.lock:
                            self.skipped_monitor_frames += 1
                    if monitor and self.monitor_checks >= MAX_MONITOR_CHECKS:
                        raise SearchError('Live monitoring check limit reached. Search stopped.')
                    if job or monitor:
                        context = job['context'] if job else {
                            'phase': 'monitoring', 'allowed_actions': ['inspect', 'stop'],
                            'max_sequence_length': 1, 'remaining_turns': 0, 'remaining_steps': 0,
                            'history': [], 'monitor_only': True}
                        if job and self.context_provider:
                            # Match against the exact selected image, which may
                            # be newer than the one available when queued.
                            context = {**context, **self.context_provider(latest['jpeg'])}
                        request_id = 'assessment_' + str(self.assessments + 1)
                        self._send(ws, 'response.create', response={
                            'conversation': 'none', 'metadata': {'request_id': request_id},
                            'output_modalities': ['text'], 'max_output_tokens': 450,
                            'tools': [{'type': 'function', 'name': 'assess_view',
                                       'description': 'Report visual evidence and a proposed plan; do not execute it.',
                                       'parameters': SCHEMA}],
                            'tool_choice': {'type': 'function', 'name': 'assess_view'},
                            'input': [{'type': 'item_reference', 'id': latest['id']},
                                      {'type': 'message', 'role': 'user', 'content': [
                                          {'type': 'input_text', 'text': json.dumps({
                                              'target': job['target'] if job else latest['target'], **context})}]}]})
                        inflight = {'frame': latest, 'job': job, 'id': request_id,
                                    'started': now, 'deadline': now + RESPONSE_TIMEOUT}
                        with self.lock:
                            if monitor:
                                self.monitor_checks += 1
                        # Never assess the same monitored image repeatedly.
                        latest = {**latest, 'monitor': False}
                # Retain at most three recent images plus a pinned in-flight image.
                pinned = inflight['frame']['id'] if inflight else None
                for item_id in list(remote[:-3]):
                    if item_id != pinned:
                        self._send(ws, 'conversation.item.delete', item_id=item_id)
                        remote.remove(item_id)
                try:
                    raw = ws.recv()
                except Exception as exc:
                    if websocket and isinstance(exc, websocket.WebSocketTimeoutException):
                        continue
                    raise
                if not raw:
                    raise SearchError('Live API connection closed. Search stopped.')
                if len(raw) > 65536:
                    raise SearchError('Live API response too large. Search stopped.')
                event = json.loads(raw)
                kind = event.get('type')
                if kind == 'session.updated':
                    ready = True
                    with self.lock:
                        self.connected = True
                elif kind == 'error':
                    code = event.get('error', {}).get('code')
                    if code in ('insufficient_quota', 'rate_limit_exceeded'):
                        raise SearchError('Live API quota or rate limit reached. Search stopped.')
                    raise SearchError('Live API rejected the request. Check key and model access.')
                elif kind == 'response.done':
                    response = event.get('response', {})
                    if not inflight or response.get('metadata', {}).get('request_id') != inflight['id']:
                        raise SearchError('Live API response could not be matched to its image.')
                    result = self._result(response)
                    finished, inflight = inflight, None
                    with self.lock:
                        self.assessments += 1
                        self.latency = round(time.monotonic() - finished['started'], 2)
                    if self.closed.is_set() or cancelled.is_set():
                        break
                    if finished['job']:
                        job = finished['job']
                        job.update(result=result, frame=finished['frame'])
                        job['done'].set()
                    else:
                        # Monitoring may interrupt a plan, never authorize another movement.
                        if result['actions'] not in (['inspect'], ['stop']):
                            raise SearchError('Live monitor proposed movement. Search stopped.')
                        frame = finished['frame']
                        last_monitor = ({'features': image_features(frame['jpeg']),
                                         'timestamp': frame['timestamp'], 'epoch': frame['epoch']}
                                        if result['decision'] == 'absent' and result['actions'] == ['inspect']
                                        and not frame['moving'] and frame['phase'] == 'settling' else None)
                        self.observation(result, finished['frame'])
        except Exception as exc:
            if not self.closed.is_set() and not cancelled.is_set():
                with self.lock:
                    self.error = str(exc) if isinstance(exc, SearchError) else 'Live image connection failed. Search stopped.'
        finally:
            with self.lock:
                self.connected = False
                if self.ws is ws:
                    self.ws = None
            if ws:
                try:
                    ws.shutdown()
                except Exception:
                    pass
