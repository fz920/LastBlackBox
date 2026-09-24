"""Small, per-search visual diary. Similarity is advice, never motor permission."""
from collections import deque
import io
import time

from PIL import Image, ImageStat, UnidentifiedImageError

MAX_VIEWS = 30


def image_features(jpeg):
    """Bound decoding and retain spatial colour, not just average brightness."""
    try:
        with Image.open(io.BytesIO(jpeg)) as source:
            if source.width * source.height > 1920 * 1080:
                return None
            rgb = source.convert('RGB')
            small = rgb.resize((32, 24), Image.Resampling.BILINEAR)
            # Blank/very blurred walls must not be treated as a known location.
            textured = ImageStat.Stat(small.convert('L')).stddev[0] >= 12
            rgb.thumbnail((160, 120))
            output = io.BytesIO()
            rgb.save(output, 'JPEG', quality=60)
            return {'pixels': small.tobytes(), 'textured': textured, 'thumbnail': output.getvalue()}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def similar(a, b, *, strict=False):
    if not a or not b or not a['textured'] or not b['textured']:
        return False
    differences = [abs(x - y) for x, y in zip(a['pixels'], b['pixels'])]
    # Strict comparison is only for deferring redundant, stationary monitoring.
    return (sum(differences) / len(differences) <= (1.5 if strict else 7)
            and max(differences) <= (6 if strict else 65))


class SearchMemory:
    """Caller serializes access using the existing search/controller lock."""
    def __init__(self):
        self.revision = self.next_id = 0
        self.clear()

    def clear(self):
        self.entries = deque(maxlen=MAX_VIEWS)
        self.started = time.monotonic()
        self.repeats = self.motion = 0
        self.revision += 1

    def _match(self, features):
        return next((e for e in reversed(self.entries) if similar(features, e['features'])), None)

    def context(self, jpeg):
        match = self._match(image_features(jpeg))
        # Relevant previous departures plus a couple of recent views; no images
        # or growing transcript are sent to GPT as memory.
        related = [e for e in self.entries if match and e['view'] == match['view']]
        selected = {e['id']: e for e in related[-3:] + list(self.entries)[-2:]}
        return {'similar_to_view': match['view'] if match else None,
                'previous_checks_here': len(related),
                'recent_views': [self._summary(e) for e in selected.values()][-5:]}

    def _summary(self, entry):
        return {'view': entry['view'], 'check': entry['id'], 'decision': entry['decision'],
                'evidence': entry['evidence'][:140],
                'departures': [dict(a) for a in entry['departures']],
                'next_view': entry.get('next_view')}

    def observe(self, jpeg, timestamp, result, phase):
        features = image_features(jpeg)
        match = self._match(features)
        previous = self.entries[-1] if self.entries else None
        revisited = bool(match and previous and previous['motion'] != self.motion)
        self.next_id += 1
        view = match['view'] if match else self.next_id
        if previous and previous['motion'] != self.motion:
            previous['next_view'] = view
        entry = {'id': self.next_id, 'view': view, 'features': features,
                 'seconds': round(max(0, timestamp - self.started), 1),
                 'decision': result['decision'], 'evidence': result['description'],
                 'phase': phase, 'repeat': revisited, 'motion': self.motion, 'departures': []}
        self.entries.append(entry)
        self.repeats += int(revisited)
        self.revision += 1

    def action(self, direction, duration, *, interrupted=False):
        self.motion += 1
        if self.entries:
            # At most three planned moves occur between checks.
            self.entries[-1]['departures'].append({
                'action': direction, 'seconds': round(duration, 3),
                'outcome': 'interrupted' if interrupted else 'completed'})
            self.entries[-1]['departures'] = self.entries[-1]['departures'][-3:]
        self.revision += 1

    def status(self):
        return {'revision': self.revision, 'capacity': MAX_VIEWS,
                'views': len({e['view'] for e in self.entries}), 'repeats': self.repeats,
                'entries': [{**self._summary(e), 'seconds': e['seconds'], 'phase': e['phase'],
                             'repeat': e['repeat'], 'thumbnail': bool(e['features'])}
                            for e in reversed(self.entries)]}

    def thumbnail(self, check):
        return next((e['features']['thumbnail'] for e in self.entries
                     if e['id'] == check and e['features']), None)
