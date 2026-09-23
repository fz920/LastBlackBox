#!/usr/bin/env python3
"""Measure local answers and robot responsiveness. Never sends motor commands."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import subprocess
import threading
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[4] / '_tmp/llm'
HERE = Path(__file__).resolve().parent
SYSTEM = (
    'You are a robot reporting object detections. Answer in one short sentence. '
    'Use only the facts below and preserve exact counts. '
    'For any unknown fact, answer "I cannot tell from the detections." '
    'If no objects were detected, say "No objects were detected." '
    'Never guess or give movement instructions.'
)


def prompt_for(counts, question):
    facts = '; '.join(f'{label}: {count}' for label, count in counts.items()) or 'No objects detected'
    return (f'Object counts: {facts}.\nColours: unknown. Positions: unknown. '
            f'Activities: unknown. Identities: unknown. Distances: unknown.\nQuestion: {question}')


def request(url, body=None, timeout=3):
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(url, data=data, headers={'Content-Type': 'application/json'}), timeout=timeout) as response:
        return json.load(response)


def memory(path='/proc/meminfo'):
    return {line.split(':')[0]: int(line.split()[1]) for line in Path(path).read_text().splitlines()
            if line.startswith(('MemAvailable:', 'SwapFree:', 'SwapTotal:', 'VmRSS:', 'VmHWM:'))}


def summary(samples):
    valid = [s for s in samples if 'error' not in s]
    if not valid:
        return {'samples': len(samples), 'errors': len(samples)}
    latencies = sorted(s['http_ms'] for s in valid)
    inference_times = [s['inference_ms'] for s in valid if s['inference_ms'] is not None]
    return {
        'samples': len(samples), 'errors': len(samples) - len(valid),
        'camera_live_samples': sum(s['camera_live'] for s in valid),
        'detector_live_samples': sum(s['detector_live'] for s in valid),
        'motor_stopped_samples': sum(s['command'] == 'stop' for s in valid),
        'http_p95_ms': round(latencies[min(len(latencies) - 1, int(len(latencies) * .95))], 1),
        'camera_fps': round((valid[-1]['frame'] - valid[0]['frame']) / max(.001, valid[-1]['time'] - valid[0]['time']), 1),
        'inference_median_ms': statistics.median(inference_times) if inference_times else None,
        'minimum_available_mib': round(min(s['memory']['MemAvailable'] for s in valid) / 1024, 1),
        'peak_llm_rss_mib': round(max(s.get('rss_kib', 0) for s in valid) / 1024, 1),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--robot-url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    # Refuse to connect to an unrelated server on the experiment's port.
    import socket
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', 8081)) == 0:
            raise SystemExit('Port 8081 is already occupied. Stop the standalone LLM server first.')
    status = request(args.robot_url + '/api/status')
    detections = request(args.robot_url + '/api/detections')
    if not status['camera_live'] or not detections['live']:
        raise SystemExit('Start the robot camera and NPU website before benchmarking.')
    stopped = threading.Event()
    samples, answers = [], []
    phase, process = 'baseline', None

    def monitor():
        while not stopped.is_set():
            sample = {'time': time.monotonic(), 'phase': phase, 'memory': memory()}
            try:
                start = time.monotonic()
                state = request(args.robot_url + '/api/status')
                detection = request(args.robot_url + '/api/detections')
                with urlopen(args.robot_url + '/api/frame', timeout=3) as frame:
                    frame.read()
                sample.update(http_ms=(time.monotonic() - start) * 1000,
                              frame=state['frame'], camera_live=state['camera_live'],
                              detector_live=detection['live'], command=state['command'],
                              inference_ms=detection.get('inference_ms'))
                if process and process.poll() is None:
                    sample['rss_kib'] = memory(f'/proc/{process.pid}/status').get('VmRSS', 0)
                    if sample['memory']['MemAvailable'] < 120 * 1024:
                        process.terminate()
                        sample['error'] = 'Stopped LLM because available RAM fell below 120 MiB'
            except Exception as exc:
                sample['error'] = str(exc)
            samples.append(sample)
            stopped.wait(.5)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    log = (ROOT / 'server.log').open('w')
    report = {'runtime': 'b11139', 'model': 'Qwen2.5-0.5B-Instruct Q4_K_M',
              'threads': 2, 'context': 1024, 'max_tokens': 48, 'system_prompt': SYSTEM}
    try:
        print('Measuring camera/NPU baseline for five seconds...', flush=True)
        time.sleep(5)
        phase = 'loading'
        started = time.monotonic()
        process = subprocess.Popen(['bash', str(HERE / 'run-server.sh')], stdout=log, stderr=log)
        while time.monotonic() - started < 120:
            if process.poll() is not None:
                raise RuntimeError('LLM server exited; see _tmp/llm/server.log')
            try:
                if request('http://127.0.0.1:8081/health')['status'] == 'ok':
                    break
            except OSError:
                pass
            time.sleep(.5)
        else:
            raise TimeoutError('LLM startup exceeded 120 seconds')
        report['startup_seconds'] = round(time.monotonic() - started, 2)
        print(f"Model ready in {report['startup_seconds']} seconds", flush=True)
        phase = 'questions'
        fixtures = [
            ('description', {'person': 2, 'laptop': 1}, 'Describe what you see.'),
            ('count', {'person': 2, 'laptop': 1}, 'How many people can you see?'),
            ('unknown_colour', {'person': 2, 'laptop': 1}, 'What colour is the laptop?'),
            ('empty', {}, 'What can you see?'),
            ('unknown_position', {'cup': 1, 'bottle': 1}, 'Is the cup to the left of the bottle?'),
        ]
        live = request(args.robot_url + '/api/detections')
        if live['live']:
            fixtures.append(('live_camera', dict(Counter(obj['label'] for obj in live['objects'])), 'Describe what you see.'))
        for name, counts, question in fixtures:
            prompt = prompt_for(counts, question)
            start = time.monotonic()
            response = request('http://127.0.0.1:8081/v1/chat/completions', {
                'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}],
                'temperature': 0, 'seed': 42, 'max_tokens': 48, 'stream': False,
            }, timeout=90)
            result = {'case': name, 'counts': counts, 'question': question,
                      'answer': response['choices'][0]['message']['content'],
                      'seconds': round(time.monotonic() - start, 2),
                      'finish_reason': response['choices'][0]['finish_reason'],
                      'usage': response.get('usage'), 'timings': response.get('timings')}
            answers.append(result)
            print(json.dumps(result), flush=True)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        stopped.set()
        thread.join(timeout=10)
        log.close()
        report['phases'] = {name: summary([s for s in samples if s['phase'] == name])
                            for name in ('baseline', 'loading', 'questions')}
        report['answers'] = answers
        report['samples'] = samples
        report['memory_after'] = memory()
        (ROOT / 'benchmark.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report['phases'], indent=2), flush=True)
        print('LLM stopped; model RAM released. Report: _tmp/llm/benchmark.json', flush=True)


if __name__ == '__main__':
    main()
