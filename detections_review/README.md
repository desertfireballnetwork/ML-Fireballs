# detections_review

Tools for classifying ML-flagged fireball detection tiles and cross-checking
them against the confirmed-events DB, built up over several iterations.
`human_classification.csv` (the original manual-rating CSV) is being
retired — `fireball_candidates_human_review.jsonl` is now the single source of truth for every
detection's review state.

## Day-to-day tools

### `review_app.py` — rate detections
The interactive review website. Presents unrated candidate detections in
small batches (multi-station events: ≥2 detections from ≥2 different
source images within a ~10s window, capped at ~150 pending tiles per
batch). Ratings save immediately, one click each. Rating key: `f`=fireball,
`o`=other interesting transient, `p`=plane, `s`=satellite, blank=not
interesting.

```
python3 review_app.py
```
→ http://127.0.0.1:5050/

Reads/writes `data/fireball_candidates_human_review.jsonl` via `reviews_store.py`.

### `prepare_detections_for_L2-ingestion.py` — export for manual DB ingestion
Finds multi-station `'f'`-rated detections that aren't already an exact
match in the DB extract, aren't ambiguous (multiple candidate DB events
within ±10s), and haven't been exported before. Writes a timestamped pair
into `data/`:

- `marked_for_ingestion_<UTC timestamp>.csv` — full detail per detection
  (camera, group info, inherited event_codename if a nearby-but-not-exact
  DB entry was found, etc.)
- `marked_for_ingestion_<UTC timestamp>.sh` — executable, one `cp <source>
  <dest>` line per detection, copying into
  `/data/NAS_clone/events_emails_to_process/` with the score/index block
  in the filename replaced by `tile` (e.g. `..._72_....jpg` →
  `..._tile_....jpg`).

Every exported row is marked `L2_ingested: true` in
`data/fireball_candidates_human_review.jsonl` immediately after the CSV
write succeeds, so re-running only ever exports what's newly eligible
since the last run.

```
python3 prepare_detections_for_L2-ingestion.py
```

### `find_duplicate_db_events.py` — DB duplicate-event report
Independent of `fireball_candidates_human_review.jsonl` entirely — reads only the DB extract.
Flags pairs of distinct `event_codename`s whose (single representative,
earliest-row) timestamps fall within 1 minute of each other, a sign the DB
may have catalogued the same sighting twice under different codenames.
Reports shared cameras between flagged pairs as a stronger duplicate
signal than timing alone. Writes `data/possible_duplicate_events.csv`.

```
python3 find_duplicate_db_events.py
```

## `data/` — all data files

- **`fireball_candidates_human_review.jsonl`** — one JSON object per `detection_tile`, updated in
  place (never appended — see `reviews_store.py`): `rating`, `reviewer`,
  `reviewed_at`, `source` (`webapp` or `legacy_csv`), `L2_ingested`,
  `ingestion_date`. The source of truth for everything above.
- **`detection_tiles_L2_images_2026-08-11.csv`** — read-only snapshot of
  the confirmed-events DB extract, cross-referenced by
  `prepare_detections_for_L2-ingestion.py` and
  `find_duplicate_db_events.py`. A fresh export can be obtained with:
  ```
  mongoexport --db=detection_tiles --collection=L2_images --type=csv \
      --fields=datetime,full_path,location,hostname,event_codename \
      --out=/tmp/detection_tiles_L2_images_2026-08-11.csv
  ```
  If you refresh it, update the filename (and the matching `DB_PATH`
  constant in the two scripts above) rather than overwriting the old
  snapshot in place, so it's clear which DB state a given run reflects.
- **`marked_for_ingestion_<timestamp>.csv` / `.sh`** — one pair per
  `prepare_detections_for_L2-ingestion.py` run. Run the `.sh` to actually
  copy files to the ingestion destination once you're happy with a batch.
- **`possible_duplicate_events.csv`** — output of
  `find_duplicate_db_events.py`, for manual review.

`reviews_store.py` (in the project root, not `data/`) provides the shared
`load_reviews()`/`save_reviews()` helpers used by `review_app.py` and
`prepare_detections_for_L2-ingestion.py` to read/write the jsonl file. Not
run directly.

## Where the raw tiles come from

The ML detection tiles (what `review_app.py` scans and rates) are fetched
with:
```
year=2014
rclone copy --filter-from detections_rclone_filter pawsey0639:dfn-ml-fireball-detection-results-${year}/ /tmp/dfn-snail-ml-fireball/
```

Results live in one Acacia bucket per year
(`dfn-ml-fireball-detection-results-YYYY/dfn-l0-YYYYMMDD/`); the local
mirror keeps the flat `/tmp/dfn-snail-ml-fireball/dfn-l0-YYYYMMDD/` layout,
so the review tools don't care which bucket a night came from. (The old
single bucket `dfn-snail-ml-fireball` still holds a copy of everything
processed before the switch to yearly buckets.)

`detections_rclone_filter` is the rclone filter file that command uses
(jpg tiles only, from top-level `dfn-l0-2014*` prefixes only, excludes
`*thumb*`/`*differenced*`).

## `obsolete/` — retired, do not use again

Not tracked in git (listed in the repo's `.gitignore`); kept locally only.

- **`build_group_data.py`** + **`gallery_app.py`** + **`groups.json`** — the
  August 2026 DB-consistency gallery (time-grouped `'f'` tiles split into
  multi-station-no-DB / camera-mismatch / singles). Superseded by
  `review_app.py` (grouping, singletons) and
  `prepare_detections_for_L2-ingestion.py` (DB matching, inheriting
  codenames). The one thing not carried over: listing DB images within
  +/-10s of a group that had no ML detection (a recall check).

- **`human_classification.csv`** — the original manual-rating CSV, retired.
  Everything it had is already migrated into `fireball_candidates_human_review.jsonl`.
- **`migrate_classifications_to_jsonl.py`** — one-time CSV→`fireball_candidates_human_review.jsonl`
  migration. Already run; has nothing left to do once the CSV is gone
  (and would error without it).
- **`backfill_l2_ingested.py`** — one-time backfill of the `L2_ingested`
  key onto pre-existing entries. Already run (idempotent no-op now); every
  new entry gets the key automatically going forward.
- **`migrate_remove_ambiguous_from_ingestion_csv.py`** — one-off fixup for
  a specific historical bug (ambiguous rows exported before that rule
  existed). Already applied to the one file that needed it;
  `prepare_detections_for_L2-ingestion.py` skips ambiguous rows from the
  start now.
- **`rate_events.sh`** — the original one-image-at-a-time `eog` + CSV
  rating script. Fully superseded by `review_app.py`.
- **`fireball_groups_summary.csv`**, **`fireball_grouping_analysis.md`** —
  early exploratory output/write-up from before the DB cross-referencing
  and review-site tooling existed. Superseded and out of date.
