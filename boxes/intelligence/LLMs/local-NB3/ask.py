#!/usr/bin/env python3
"""Ask the standalone LLM about a snapshot of current NPU detections."""
import argparse
from collections import Counter
import json
import time
from urllib.error import URLError

from benchmark import SYSTEM, prompt_for, request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question', nargs='?', default='Describe what you see.')
    args = parser.parse_args()
    if len(args.question) > 400:
        parser.error('Keep the question under 400 characters for this small context window.')
    try:
        detections = request('http://127.0.0.1:8000/api/detections')
        if not detections.get('live'):
            raise SystemExit('No fresh Coral detections. Check the robot website first.')
        counts = dict(Counter(obj['label'] for obj in detections['objects']))
        print('Detection snapshot:', json.dumps(counts), flush=True)
        start = time.monotonic()
        response = request('http://127.0.0.1:8081/v1/chat/completions', {
            'messages': [{'role': 'system', 'content': SYSTEM},
                         {'role': 'user', 'content': prompt_for(counts, args.question)}],
            'temperature': 0, 'max_tokens': 48, 'seed': 42, 'stream': False,
        }, timeout=90)
    except URLError as exc:
        raise SystemExit(f'Could not reach robot/LLM: {exc}. Start run-server.sh and wait for it to load.') from exc
    print(response['choices'][0]['message']['content'])
    print(f'Elapsed: {time.monotonic() - start:.1f} seconds')
    if response['choices'][0]['finish_reason'] == 'length':
        print('(Answer reached the short output limit.)')


if __name__ == '__main__':
    main()
