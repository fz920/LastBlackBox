"""One cancellable OpenAI speech request, with bounded audio held only in RAM."""
import http.client
import json
import socket
import threading
import time


class CloudSpeechError(Exception):
    """Safe to display; never includes raw API errors or credentials."""


class CloudSpeech:
    MAX_BYTES = 24000 * 2 * 20  # At most 20 seconds of mono PCM16.

    def __init__(self):
        self.lock = threading.Lock()
        self.connection = None

    def close(self):
        with self.lock:
            connection, self.connection = self.connection, None
        if connection:
            sock = connection.sock
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

    def synthesize(self, key, text, cancel):
        if cancel.is_set():
            raise CloudSpeechError('Announcement cancelled.')
        body = {'model': 'gpt-4o-mini-tts', 'voice': 'marin', 'input': text,
                'response_format': 'pcm',
                'instructions': 'Speak this single sentence exactly as written in a warm, concise, natural voice; do not add any words.'}
        connection = http.client.HTTPSConnection('api.openai.com', timeout=12)
        with self.lock:
            self.connection = connection
        try:
            if cancel.is_set():
                raise CloudSpeechError('Announcement cancelled.')
            deadline = time.monotonic() + 20
            connection.request('POST', '/v1/audio/speech', body=json.dumps(body), headers={
                'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            if response.status in (401, 403):
                raise CloudSpeechError('OpenAI voice access was rejected. Check the API key and model access.')
            if response.status == 429:
                raise CloudSpeechError('OpenAI voice quota or rate limit reached.')
            if response.status != 200:
                raise CloudSpeechError('OpenAI could not generate the announcement.')
            audio = bytearray()
            while True:
                if cancel.is_set() or time.monotonic() > deadline:
                    raise CloudSpeechError('Announcement cancelled or timed out.')
                chunk = response.read1(min(16384, self.MAX_BYTES + 1 - len(audio)))
                if not chunk:
                    break
                audio.extend(chunk)
                if len(audio) > self.MAX_BYTES:
                    raise CloudSpeechError('Announcement exceeded the audio limit.')
            if not audio or len(audio) % 2:
                raise CloudSpeechError('OpenAI returned invalid announcement audio.')
            return bytes(audio)
        except CloudSpeechError:
            raise
        except Exception:
            raise CloudSpeechError('Voice generation failed or timed out. Check the internet and try again.') from None
        finally:
            with self.lock:
                if self.connection is connection:
                    self.connection = None
            connection.close()
