"""On-demand local language model; constrained descriptions and no motor access."""
import http.client
from itertools import permutations
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time


def allowed_descriptions(parts):
    """A closed set of phrasings, each preserving every selected object count."""
    sentences = set()
    if not parts:
        return []
    for order in permutations(parts):
        joined = order[0] if len(order) == 1 else ', '.join(order[:-1]) + ' and ' + order[-1]
        for prefix in ('I think I can see ', 'I can see ', 'In view, I can see '):
            sentences.add(prefix + joined + '.')
    return sorted(sentences)


class LocalConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request(path, endpoint, body=None, timeout=1):
    connection = LocalConnection(path, timeout)
    try:
        connection.request('POST' if body is not None else 'GET', endpoint,
                           json.dumps(body) if body is not None else None,
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = response.read(65537)
        if response.status != 200 or len(data) > 65536:
            raise RuntimeError('Local LLM returned an invalid response')
        return json.loads(data)
    finally:
        connection.close()


class LocalLLM:
    def __init__(self, root):
        self.root = Path(root)
        self.runtime = self.root / 'runtime/llama-b11139/llama-server'
        self.model = self.root / 'models/qwen2.5-0.5b-instruct-q4_k_m.gguf'
        self.lock = threading.Lock()
        self.process = None

    def generate(self, parts, cancel, progress):
        if not self.runtime.is_file() or not self.model.is_file():
            raise RuntimeError('Local model is not installed')
        choices = allowed_descriptions(parts)
        if not choices:
            raise ValueError('No detected objects to describe')
        grammar = 'root ::= ' + ' | '.join(json.dumps(text) for text in choices)
        progress('loading')
        # Private socket and no log/model copies. A process exists only for this request.
        with tempfile.TemporaryDirectory(prefix='web-', dir=self.root) as directory:
            path = Path(directory) / 'llm.sock'
            command = ['nice', '-n', '10', str(self.runtime), '-m', str(self.model),
                       '--host', str(path), '--offline', '--no-webui',
                       '-t', '2', '-tb', '2', '-c', '1024', '-b', '128', '-ub', '64',
                       '-np', '1', '-n', '64', '-ngl', '0', '--cache-ram', '0']
            try:
                with self.lock:
                    if cancel.is_set():
                        raise InterruptedError('Description cancelled')
                    self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    process = self.process
                deadline = time.monotonic() + 45
                while not cancel.is_set():
                    if process.poll() is not None:
                        raise RuntimeError('Local model could not start')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Local model loading timed out')
                    try:
                        if request(path, '/health', timeout=.3).get('status') == 'ok':
                            break
                    except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                        pass
                    cancel.wait(.2)
                if cancel.is_set():
                    raise InterruptedError('Description cancelled')
                progress('thinking')
                response = request(path, '/v1/chat/completions', {
                    'messages': [
                        {'role': 'system', 'content': 'Describe the listed objects in one short sentence. Preserve all counts. Add no other facts.'},
                        {'role': 'user', 'content': 'Objects: ' + '; '.join(parts)}],
                    'grammar': grammar, 'temperature': .6, 'max_tokens': 64, 'stream': False,
                }, timeout=30)
                choice = response['choices'][0]
                text = choice['message']['content'].strip()
                # Validate independently of the model server's grammar enforcement.
                if choice['finish_reason'] != 'stop' or text not in choices:
                    raise ValueError('Model description did not match the detected objects')
                return text
            finally:
                self.close()

    def close(self):
        with self.lock:
            process, self.process = self.process, None
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            except ProcessLookupError:
                pass
