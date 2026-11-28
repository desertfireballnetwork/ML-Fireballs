"""
Exports multi-station fireball detections that still need to be manually
ingested into the confirmed-events DB, and marks them as ingested in
fireball_candidates_human_review.jsonl so they aren't exported again next run.

Writes two files with matching timestamps: marked_for_ingestion_<ts>.csv
(full detail, one row per detection) and marked_for_ingestion_<ts>.sh (one
`cp` command per detection, copying it from its source path into
INGEST_DEST_DIR under a renamed basename - the index/score block replaced
by "tile", e.g. 59_2015-12-31_183558_S_DSC_0962_65_2937-0214-2985-0334.jpg
becomes 59_2015-12-31_183558_S_DSC_0962_tile_2937-0214-2985-0334.jpg).

Source: 'f'-rated entries in fireball_candidates_human_review.jsonl (last line per path wins),
grouped by time (>=2 distinct source images within a 10s chain-cluster,
same "multi-station" rule as review_app.py), cross-referenced against the
DB extract (detection_tiles_L2_images_2026-08-11.csv):

  - exact match: the image itself is already a DB record -> dropped from
    the output entirely, nothing to ingest.
  - inherited: no exact match, but the DB has something else within +/-10s
    of this specific image's own timestamp -> borrow that record's
    event_codename/hostname (nearest in time). If more than one distinct
    event_codename was in range ("ambiguous"), the detection is left out
    entirely for now - not exported, not marked ingested - so it's
    reconsidered on every future run until the ambiguity gets resolved
    some other way.
  - none: nothing found either way.

Only detections not yet marked L2_ingested are exported. After a
successful CSV write, every exported row is marked L2_ingested: true (with
an ingestion_date) in fireball_candidates_human_review.jsonl - only once the write has already
succeeded, so a failed CSV write never falsely marks anything. Exact
matches are left completely untouched either way.

fireball_candidates_human_review.jsonl / human_classification.csv / the DB CSV are all read-only
here except for the ingestion-marking append described above.

Run:
    python3 prepare_detections_for_L2-ingestion.py
"""
import bisect
import csv
import os
import re
import shlex
from datetime import datetime, timedelta, timezone

from reviews_store import load_reviews, save_reviews

HERE = "/opt/ML-Fireballs/detections_review"
DATA_DIR = f"{HERE}/data"
REVIEWS_PATH = f"{DATA_DIR}/fireball_candidates_human_review.jsonl"
DB_PATH = f"{DATA_DIR}/detection_tiles_L2_images_2026-08-11.csv"
INGEST_DEST_DIR = "/data/NAS_clone/events_emails_to_process"

TIME_TOLERANCE = timedelta(seconds=10)

FNAME_RE = re.compile(r'/(\d+)_(\d{4}-\d{2}-\d{2})_(\d{6})_')


def parse_tile_path(path):
    fname = path.split('/')[-1]
    m = FNAME_RE.match('/' + fname)
    if not m:
        return None
    cam, date, hms = m.groups()
    dt = datetime.strptime(date + hms, '%Y-%m-%d%H%M%S')
    return cam, dt


def image_key(path, n=5):
    """First n underscore-separated parts of the basename, extension stripped."""
    fname = os.path.basename(path)
    fname = fname.split('.', 1)[0]
    parts = fname.split('_')
    return '_'.join(parts[:n])


def tile_basename(path):
    """Basename with its second-to-last underscore-separated block (the
    detection index/score, e.g. "65") replaced with "tile", e.g.:
    59_2015-12-31_183558_S_DSC_0962_65_2937-0214-2985-0334.jpg ->
    59_2015-12-31_183558_S_DSC_0962_tile_2937-0214-2985-0334.jpg"""
    fname = os.path.basename(path)
    parts = fname.split('_')
    parts[-2] = 'tile'
    return '_'.join(parts)


# --- load fireball_candidates_human_review.jsonl, keep only rating == 'f' ---
latest_reviews = load_reviews(REVIEWS_PATH)

detections = []
for path, entry in latest_reviews.items():
    if entry['rating'] != 'f':
        continue
    parsed = parse_tile_path(path)
    if parsed is None:
        continue
    cam, dt = parsed
    detections.append({'cam': cam, 'dt': dt, 'path': path})

# --- cluster by time, chain tolerance 10s ---
detections.sort(key=lambda d: d['dt'])
groups = []
cur = []
for d in detections:
    if cur and (d['dt'] - cur[-1]['dt']) > TIME_TOLERANCE:
        groups.append(cur)
        cur = []
    cur.append(d)
if cur:
    groups.append(cur)

# keep only multi-station groups (>=2 distinct source images)
groups = [g for g in groups if len(set((d['cam'], d['dt']) for d in g)) >= 2]

# --- load the DB extract: exact-key index + time-sorted list for window lookups ---
db_by_key = {}
db_rows = []
with open(DB_PATH, newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        key = image_key(row['full_path'])
        entry = {
            'dt': datetime.strptime(row['datetime'], '%Y-%m-%dT%H:%M:%S.%fZ'),
            'hostname': row['hostname'],
            'event_codename': row['event_codename'],
        }
        db_by_key.setdefault(key, []).append(entry)
        db_rows.append(entry)

db_rows.sort(key=lambda r: r['dt'])
db_dts = [r['dt'] for r in db_rows]


def nearby_db_rows(dt, tol=TIME_TOLERANCE):
    lo = bisect.bisect_left(db_dts, dt - tol)
    hi = bisect.bisect_right(db_dts, dt + tol)
    return db_rows[lo:hi]


# --- build output rows: skip exact matches and already-ingested detections ---
out_rows = []
n_skipped_ambiguous = 0
for g in groups:
    group_time_start = g[0]['dt'].isoformat()
    group_time_end = g[-1]['dt'].isoformat()
    group_cams = '|'.join(sorted(set(d['cam'] for d in g)))

    for d in g:
        key = image_key(d['path'])
        exact_matches = db_by_key.get(key, [])
        if exact_matches:
            continue

        if latest_reviews[d['path']].get('L2_ingested', False):
            continue

        row = {
            'detection_tile': d['path'],
            'cam': d['cam'],
            'dt': d['dt'].isoformat(),
            'group_time_start': group_time_start,
            'group_time_end': group_time_end,
            'group_cams': group_cams,
            'match_status': 'none',
            'event_codename': '',
            'db_hostname': '',
            'inherited_delta_seconds': '',
            'ambiguous': False,
        }

        candidates = nearby_db_rows(d['dt'])
        if candidates:
            nearest = min(candidates, key=lambda r: abs((r['dt'] - d['dt']).total_seconds()))
            row['match_status'] = 'inherited'
            row['event_codename'] = nearest['event_codename']
            row['db_hostname'] = nearest['hostname']
            row['inherited_delta_seconds'] = round((nearest['dt'] - d['dt']).total_seconds(), 3)
            distinct_codenames = set(r['event_codename'] for r in candidates)
            row['ambiguous'] = len(distinct_codenames) > 1

        # leave ambiguous detections out for now - not exported, not marked
        # ingested, so they'll be reconsidered on every future run until
        # the ambiguity is resolved some other way.
        if row['ambiguous']:
            n_skipped_ambiguous += 1
            continue

        out_rows.append(row)

out_rows.sort(key=lambda r: r['dt'])

now = datetime.now(timezone.utc)
out_stem = f"{DATA_DIR}/marked_for_ingestion_{now.strftime('%Y%m%dT%H%M%SZ')}"
out_path = f"{out_stem}.csv"
out_sh_path = f"{out_stem}.sh"

fieldnames = [
    'detection_tile', 'cam', 'dt', 'group_time_start', 'group_time_end', 'group_cams',
    'match_status', 'event_codename', 'db_hostname', 'inherited_delta_seconds', 'ambiguous',
]
with open(out_path, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(out_rows)

with open(out_sh_path, 'w') as f:
    f.write('#!/bin/bash\n')
    for row in out_rows:
        src = row['detection_tile']
        dst = os.path.join(INGEST_DEST_DIR, tile_basename(src))
        f.write(f"cp {shlex.quote(src)} {shlex.quote(dst)}\n")
os.chmod(out_sh_path, 0o755)

# --- mark every exported row as ingested, only after the CSV write succeeded ---
ingestion_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")
if out_rows:
    for row in out_rows:
        latest_reviews[row['detection_tile']]['L2_ingested'] = True
        latest_reviews[row['detection_tile']]['ingestion_date'] = ingestion_date
    save_reviews(REVIEWS_PATH, latest_reviews)

n_inherited = sum(1 for r in out_rows if r['match_status'] == 'inherited')
n_none = sum(1 for r in out_rows if r['match_status'] == 'none')

print(f"groups: {len(groups)}")
print(f"rows exported: {len(out_rows)} (inherited={n_inherited}, none={n_none})")
print(f"ambiguous rows skipped (left for a future run): {n_skipped_ambiguous}")
print(f"Wrote {out_path}")
print(f"Wrote {out_sh_path}")
if out_rows:
    print(f"Marked {len(out_rows)} row(s) L2_ingested=true in {REVIEWS_PATH}")
