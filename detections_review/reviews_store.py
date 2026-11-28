"""
Shared read/write helpers for fireball_candidates_human_review.jsonl.

fireball_candidates_human_review.jsonl holds exactly one line per detection_tile - the current
state of that detection's review/ingestion status. Updates are made by
rewriting the whole file (atomically, via a temp file + os.replace), not
by appending a new line - so there is never more than one line for a
given path.

No top-level side effects, so this module is safe to import from any of
the other detections_review/ scripts.
"""
import json
import os


def load_reviews(path):
    """Returns {detection_tile: entry} - empty dict if the file doesn't exist yet."""
    reviews = {}
    if not os.path.exists(path):
        return reviews
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            reviews[entry['detection_tile']] = entry
    return reviews


def save_reviews(path, reviews):
    """Rewrites the whole file, one line per entry in `reviews` (a {detection_tile: entry} dict)."""
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w') as f:
        for entry in reviews.values():
            f.write(json.dumps(entry) + '\n')
    os.replace(tmp_path, path)
