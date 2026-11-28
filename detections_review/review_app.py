"""
Interactive review website for classifying fireball-detection tiles in
small batches, replacing the one-image-at-a-time rate_events.sh workflow.

human_classification.csv is never written to. New ratings (and the
migrated history from migrate_classifications_to_jsonl.py) live in
fireball_candidates_human_review.jsonl, one JSON object per detection_tile (updated in place via
reviews_store.py, never appended - see that module for details):
    {"detection_tile": "<path>", "rating": "f", "reviewer": "hadry",
     "reviewed_at": "2026-08-12T00:00:00Z", "source": "legacy_csv"}

A "batch" is a run of consecutive time-clustered groups (>=2 detections
from >=2 different source images, within a TIME_TOLERANCE_SECONDS chain
tolerance) with at least one still-unreviewed tile, packed greedily so each
batch has at most BATCH_PENDING_CAP pending tiles. Fully-reviewed groups
never appear at all.

Run:
    python3 review_app.py
Then open http://127.0.0.1:5050/
"""
import getpass
import os
import re
from datetime import datetime, timezone

from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for

from reviews_store import load_reviews, save_reviews

HERE = os.path.dirname(os.path.abspath(__file__))
DATADIR = "/tmp/dfn-snail-ml-fireball"
JSONL_PATH = os.path.join(HERE, "data", "fireball_candidates_human_review.jsonl")

TIME_TOLERANCE_SECONDS = 10
BATCH_PENDING_CAP = 150
SINGLES_PAGE_SIZE = 1000
RATINGS = [
    ("", "not interesting"),
    ("f", "fireball (f)"),
    ("o", "other interesting transient (o)"),
    ("p", "plane (p)"),
    ("s", "satellite (s)"),
]
RATING_LABELS = {
    "": "not interesting",
    "f": "fireball",
    "o": "other interesting transient",
    "p": "plane",
    "s": "satellite",
}

FNAME_RE = re.compile(r'(\d+)_(\d{4}-\d{2}-\d{2})_(\d{6})_')

app = Flask(__name__)

BATCHES = []
REVIEWS = {}
ALLOWED_PATHS = set()
SINGLETONS = []


def parse_path(path):
    fname = os.path.basename(path)
    m = FNAME_RE.match(fname)
    if not m:
        return None
    cam, date, hms = m.groups()
    dt = datetime.strptime(date + hms, '%Y-%m-%d%H%M%S')
    return cam, dt


def scan_tiles():
    detections = []
    for root, _dirs, files in os.walk(DATADIR):
        for fname in files:
            if not fname.endswith('.jpg'):
                continue
            path = os.path.join(root, fname)
            parsed = parse_path(path)
            if parsed is None:
                continue
            cam, dt = parsed
            detections.append({'cam': cam, 'dt': dt, 'path': path})
    return detections


def cluster(detections):
    detections = sorted(detections, key=lambda d: d['dt'])
    groups = []
    cur = []
    for d in detections:
        if cur and (d['dt'] - cur[-1]['dt']).total_seconds() > TIME_TOLERANCE_SECONDS:
            groups.append(cur)
            cur = []
        cur.append(d)
    if cur:
        groups.append(cur)
    return groups


def group_record(g):
    return {
        'time_start': g[0]['dt'].isoformat(),
        'time_end': g[-1]['dt'].isoformat(),
        'cams': sorted(set(d['cam'] for d in g)),
        'detections': [{'cam': d['cam'], 'dt': d['dt'].isoformat(), 'path': d['path']} for d in g],
    }


def pending_count(group_rec, reviews):
    return sum(1 for d in group_rec['detections'] if d['path'] not in reviews)


def build_batches(groups, reviews):
    # keep only groups with >=2 distinct source images and >=1 pending tile
    candidates = []
    for g in groups:
        distinct_images = set((d['cam'], d['dt']) for d in g)
        if len(distinct_images) < 2:
            continue
        rec = group_record(g)
        if pending_count(rec, reviews) == 0:
            continue
        candidates.append(rec)

    # prioritize timestamps caught by the most cameras (most likely to be
    # genuine multi-station events); stable sort keeps chronological order
    # among ties.
    candidates.sort(key=lambda r: -len(r['cams']))

    batches = []
    cur_batch = []
    cur_pending = 0
    for rec in candidates:
        rec_pending = pending_count(rec, reviews)
        if cur_batch and cur_pending + rec_pending > BATCH_PENDING_CAP:
            batches.append(cur_batch)
            cur_batch = []
            cur_pending = 0
        cur_batch.append(rec)
        cur_pending += rec_pending
    if cur_batch:
        batches.append(cur_batch)
    return batches


def build_singletons(groups):
    # detections whose time-cluster has only one distinct source image, i.e.
    # never qualify for a review batch (which requires >=2 distinct images)
    singles = []
    for g in groups:
        distinct_images = set((d['cam'], d['dt']) for d in g)
        if len(distinct_images) >= 2:
            continue
        singles.extend(g)
    singles.sort(key=lambda d: d['dt'])
    return singles


def reload_data():
    global BATCHES, REVIEWS, ALLOWED_PATHS, SINGLETONS
    REVIEWS = load_reviews(JSONL_PATH)
    detections = scan_tiles()
    groups = cluster(detections)
    BATCHES = build_batches(groups, REVIEWS)
    SINGLETONS = build_singletons(groups)
    ALLOWED_PATHS = {d['path'] for batch in BATCHES for g in batch for d in g['detections']}
    ALLOWED_PATHS |= {d['path'] for d in SINGLETONS}


def batch_pending_count(batch):
    return sum(pending_count(g, REVIEWS) for g in batch)


def next_pending_batch_index(start):
    for i in range(start, len(BATCHES)):
        if batch_pending_count(BATCHES[i]) > 0:
            return i
    return None


PAGE_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Detection review</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1.5rem; background: #111; color: #eee; }
  h1 { font-size: 1.2rem; margin: 0 0 0.25rem; }
  .banner { position: sticky; top: 0; background: #111; padding: 0.75rem 0 1rem; z-index: 10; border-bottom: 1px solid #333; margin-bottom: 1.25rem; }
  .banner .progress { color: #999; font-size: 0.85rem; }
  .banner .note { color: #2a5; font-size: 0.8rem; margin-top: 0.35rem; }
  .banner .nav a { color: #6cf; margin-right: 1rem; }
  .group { border: 1px solid #333; border-radius: 8px; margin-bottom: 1.5rem; padding: 1rem; background: #1a1a1a; }
  .group-header { margin-bottom: 0.75rem; }
  .group-header .time { font-weight: 600; }
  .cam-badge { display: inline-block; background: #2a5; color: #fff; border-radius: 4px; padding: 0 6px; font-size: 0.75rem; margin-right: 4px; }
  .group-actions { margin-top: 0.5rem; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .group-actions-label { color: #888; font-size: 0.75rem; margin-right: 4px; }
  .bulk-btn { cursor: pointer; border: 1px solid #557; background: #223; color: #adf; border-radius: 4px; padding: 3px 8px; font-size: 0.75rem; }
  .bulk-btn:hover { background: #335; border-color: #6cf; }
  .tiles { display: flex; flex-wrap: wrap; gap: 0.75rem; }
  .tile { text-align: center; width: 220px; }
  .tile img { max-width: 220px; max-height: 220px; display: block; border-radius: 4px; border: 1px solid #333; background: #000; margin: 0 auto; }
  .tile .cap { font-size: 0.75rem; color: #aaa; margin-top: 0.25rem; word-break: break-all; }
  .rate-form { display: flex; flex-wrap: wrap; gap: 4px; justify-content: center; margin-top: 0.4rem; }
  .rate-form button { cursor: pointer; border: 1px solid #444; background: #222; color: #eee; border-radius: 4px; padding: 3px 8px; font-size: 0.75rem; }
  .rate-form button:first-child { background: #333; border-color: #666; font-weight: 600; }
  .rate-form button:hover { background: #2a5; border-color: #2a5; }
  .reviewed { font-size: 0.75rem; margin-top: 0.4rem; padding: 3px 0; }
  .reviewed .rating { font-weight: 600; }
  .reviewed.blank .rating { color: #888; }
  .reviewed:not(.blank) .rating { color: #2a5; }
  .reviewed .who { color: #777; }
  a { color: #6cf; }
  .bulk-uninteresting { margin: 2rem 0 1rem; padding-top: 1rem; border-top: 1px solid #333; text-align: center; }
  .danger-btn { cursor: pointer; border: 1px solid #733; background: #311; color: #f99; border-radius: 4px; padding: 8px 14px; font-size: 0.85rem; }
  .danger-btn:hover { background: #522; border-color: #a55; }
</style>
</head>
<body>
<div class="banner">
  <h1>Detection review</h1>
  <div class="progress">Batch {{ n + 1 }} / {{ total_batches }} &middot; <span id="pending-num">{{ pending }}</span> pending tile(s) in this batch</div>
  <div class="note">Ratings save immediately &mdash; safe to close this tab any time.</div>
  <div class="nav">
    {% if n > 0 %}<a href="{{ url_for('batch', n=n-1) }}">&larr; previous batch</a>{% endif %}
    {% if n + 1 < total_batches %}<a href="{{ url_for('batch', n=n+1) }}">next batch &rarr;</a>{% endif %}
    <a href="{{ url_for('reload_route') }}">reload</a>
    <a href="{{ url_for('singles') }}">singles mosaic</a>
  </div>
</div>

{% for g in groups %}
<div class="group">
  <div class="group-header">
    <span class="time">{{ g.time_start }} &rarr; {{ g.time_end }}</span>
    &middot;
    {% for c in g.cams %}<span class="cam-badge">{{ c }}</span>{% endfor %}
    <div class="group-actions">
      <span class="group-actions-label">Rate all not-yet-selected:</span>
      <button type="button" class="bulk-btn" data-rating="f">all fireballs</button>
      <button type="button" class="bulk-btn" data-rating="s">all satellites</button>
      <button type="button" class="bulk-btn" data-rating="">all not interesting</button>
    </div>
  </div>
  <div class="tiles">
    {% for d in g.detections %}
    <div class="tile" data-path="{{ d.path }}">
      <a href="{{ url_for('image') }}?path={{ d.path | urlencode }}" target="_blank">
        <img src="{{ url_for('image') }}?path={{ d.path | urlencode }}" loading="lazy">
      </a>
      <div class="cap">cam {{ d.cam }} &middot; {{ d.dt }}</div>
      <div class="result">
      {% if d.path in reviews %}
      {% set r = reviews[d.path] %}
      <div class="reviewed {{ 'blank' if not r.rating else '' }}">
        <span class="rating">{{ rating_labels.get(r.rating, r.rating) if r.rating else 'not interesting' }}</span>
        <div class="who">{{ r.reviewer }} &middot; {{ r.reviewed_at }}</div>
      </div>
      {% else %}
      <form class="rate-form" method="post" action="{{ url_for('rate') }}">
        <input type="hidden" name="path" value="{{ d.path }}">
        <input type="hidden" name="batch" value="{{ n }}">
        {% for value, label in ratings %}
        <button type="submit" name="rating" value="{{ value }}">{{ label }}</button>
        {% endfor %}
      </form>
      {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
</div>
{% endfor %}

{% if pending %}
<div class="bulk-uninteresting">
  <form method="post" action="{{ url_for('mark_batch_uninteresting') }}"
        onsubmit="return confirm('Mark all ' + document.getElementById('pending-num').textContent + ' remaining non-classified candidate(s) in this batch as not interesting? This cannot be undone.');">
    <input type="hidden" name="batch" value="{{ n }}">
    <button type="submit" class="danger-btn">Mark all remaining in this batch as uninteresting</button>
  </form>
</div>
{% endif %}

<script>
function updateTileDOM(tile, rating, data) {
  var result = tile.querySelector('.result');
  var div = document.createElement('div');
  div.className = 'reviewed' + (rating ? '' : ' blank');
  var ratingSpan = document.createElement('span');
  ratingSpan.className = 'rating';
  ratingSpan.textContent = data.rating_label;
  var whoDiv = document.createElement('div');
  whoDiv.className = 'who';
  whoDiv.textContent = data.reviewer + ' \\u00b7 ' + data.reviewed_at;
  div.appendChild(ratingSpan);
  div.appendChild(whoDiv);
  result.innerHTML = '';
  result.appendChild(div);
}

function rateTile(path, batch, rating) {
  var body = new URLSearchParams();
  body.set('path', path);
  body.set('batch', batch);
  body.set('rating', rating);
  return fetch('{{ url_for("rate") }}', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'X-Requested-With': 'fetch'
    },
    body: body.toString()
  }).then(function (resp) { return resp.json(); });
}

function afterRate(data) {
  var pendingEl = document.getElementById('pending-num');
  if (pendingEl) { pendingEl.textContent = data.pending; }
  if (data.batch_done) {
    window.location.href = data.next_batch_url || '{{ url_for("index") }}';
  }
}

document.addEventListener('submit', function (event) {
  var form = event.target;
  if (!form.classList.contains('rate-form')) return;
  event.preventDefault();

  var rating = event.submitter ? event.submitter.value : '';
  var tile = form.closest('.tile');
  var path = tile.getAttribute('data-path');
  var batch = form.querySelector('input[name="batch"]').value;

  rateTile(path, batch, rating)
    .then(function (data) {
      updateTileDOM(tile, rating, data);
      afterRate(data);
    })
    .catch(function (err) {
      console.error('rating failed', err);
      alert('Failed to save rating, please try again.');
    });
});

document.addEventListener('click', function (event) {
  var btn = event.target.closest('.bulk-btn');
  if (!btn) return;

  var rating = btn.getAttribute('data-rating');
  var groupEl = btn.closest('.group');
  var forms = Array.prototype.slice.call(groupEl.querySelectorAll('.rate-form'));

  var chain = Promise.resolve();
  forms.forEach(function (form) {
    var tile = form.closest('.tile');
    var path = tile.getAttribute('data-path');
    var batch = form.querySelector('input[name="batch"]').value;
    chain = chain.then(function () {
      return rateTile(path, batch, rating).then(function (data) {
        updateTileDOM(tile, rating, data);
        return data;
      });
    });
  });
  chain
    .then(function (lastData) {
      if (lastData) { afterRate(lastData); }
    })
    .catch(function (err) {
      console.error('bulk rating failed', err);
      alert('Failed to save one of the ratings in this group, stopping.');
    });
});
</script>
</body>
</html>
"""

DONE_TEMPLATE = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Detection review</title>
<style>body { font-family: system-ui, sans-serif; background: #111; color: #eee; padding: 2rem; }
a { color: #6cf; }</style>
</head>
<body>
<h1>No pending batches</h1>
<p>Everything currently on disk that qualifies (>=2 detections from >=2 different images, within {{ tol }}s) has been reviewed.</p>
<p><a href="{{ url_for('reload_route') }}">Reload</a> to check for new tiles.</p>
<p><a href="{{ url_for('singles') }}">singles mosaic</a></p>
</body>
</html>
"""

SINGLES_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Singles mosaic</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1.5rem; background: #111; color: #eee; }
  h1 { font-size: 1.2rem; margin: 0 0 0.25rem; }
  .banner { position: sticky; top: 0; background: #111; padding: 0.75rem 0 1rem; z-index: 10; border-bottom: 1px solid #333; margin-bottom: 1.25rem; }
  .banner .note { color: #999; font-size: 0.85rem; }
  .banner .nav a { color: #6cf; margin-right: 1rem; }
  .mosaic { display: flex; flex-wrap: wrap; gap: 4px; align-items: flex-end; }
  .tile { text-align: center; }
  .tile img { display: block; border-radius: 3px; border: 1px solid #333; background: #000; }
  .tile .cap { font-size: 0.65rem; color: #888; margin-top: 0.15rem; word-break: break-all; }
  .tile .rating { font-size: 0.65rem; color: #2a5; }
  .fireball-btn { cursor: pointer; border: 1px solid #733; background: #211; color: #f96; border-radius: 3px; padding: 1px 5px; font-size: 0.6rem; margin-top: 2px; }
  .fireball-btn:hover { background: #522; border-color: #a55; }
  a { color: #6cf; }
</style>
</head>
<body>
<div class="banner">
  <h1>Singles mosaic</h1>
  <div class="note">Page {{ n + 1 }} / {{ total_pages }} &middot; {{ count }} unrated detection(s) whose time-cluster has only one source image (never appear in the review batches). Click &#128293; to tag as fireball.</div>
  <div class="nav">
    <a href="{{ url_for('index') }}">&larr; back to review</a>
    {% if n > 0 %}<a href="{{ url_for('singles_page', n=n-1) }}">&larr; previous page</a>{% endif %}
    {% if n + 1 < total_pages %}<a href="{{ url_for('singles_page', n=n+1) }}">next page &rarr;</a>{% endif %}
    <a href="{{ url_for('reload_route') }}">reload</a>
  </div>
</div>
<div class="mosaic">
  {% for d in detections %}
  <div class="tile" data-path="{{ d.path }}">
    <a href="{{ url_for('image') }}?path={{ d.path | urlencode }}" target="_blank">
      <img src="{{ url_for('image') }}?path={{ d.path | urlencode }}" loading="lazy">
    </a>
    <div class="cap">cam {{ d.cam }} &middot; {{ d.dt }}</div>
    <button type="button" class="fireball-btn" data-path="{{ d.path }}">&#128293; fireball</button>
  </div>
  {% endfor %}
</div>
<script>
document.addEventListener('click', function (event) {
  var btn = event.target.closest('.fireball-btn');
  if (!btn) return;

  var path = btn.getAttribute('data-path');
  var body = new URLSearchParams();
  body.set('path', path);
  body.set('batch', '-1');
  body.set('rating', 'f');

  btn.disabled = true;
  fetch('{{ url_for("rate") }}', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'X-Requested-With': 'fetch'
    },
    body: body.toString()
  })
    .then(function (resp) { return resp.json(); })
    .then(function () {
      btn.closest('.tile').remove();
    })
    .catch(function (err) {
      console.error('rating failed', err);
      alert('Failed to tag as fireball, please try again.');
      btn.disabled = false;
    });
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    if not BATCHES:
        return render_template_string(DONE_TEMPLATE, tol=TIME_TOLERANCE_SECONDS)
    return redirect(url_for('batch', n=0))


@app.route("/batch/<int:n>")
def batch(n):
    if n < 0 or n >= len(BATCHES):
        return render_template_string(DONE_TEMPLATE, tol=TIME_TOLERANCE_SECONDS)
    return render_template_string(
        PAGE_TEMPLATE,
        groups=BATCHES[n],
        n=n,
        total_batches=len(BATCHES),
        pending=batch_pending_count(BATCHES[n]),
        reviews=REVIEWS,
        ratings=RATINGS,
        rating_labels=RATING_LABELS,
    )


@app.route("/rate", methods=["POST"])
def rate():
    path = request.form["path"]
    rating = request.form.get("rating", "")
    n = int(request.form["batch"])

    if path not in ALLOWED_PATHS:
        abort(404)

    entry = {
        "detection_tile": path,
        "rating": rating,
        "reviewer": getpass.getuser(),
        "reviewed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "webapp",
        "L2_ingested": False,
    }
    REVIEWS[path] = entry
    save_reviews(JSONL_PATH, REVIEWS)

    batch_done = 0 <= n < len(BATCHES) and batch_pending_count(BATCHES[n]) == 0
    next_batch_url = None
    if batch_done:
        nxt = next_pending_batch_index(n + 1)
        next_batch_url = url_for('batch', n=nxt) if nxt is not None else url_for('index')

    if request.headers.get("X-Requested-With") == "fetch":
        return {
            "rating_label": RATING_LABELS.get(rating, rating) if rating else "not interesting",
            "reviewer": entry["reviewer"],
            "reviewed_at": entry["reviewed_at"],
            "pending": batch_pending_count(BATCHES[n]) if 0 <= n < len(BATCHES) else 0,
            "batch_done": batch_done,
            "next_batch_url": next_batch_url,
        }

    if batch_done:
        return redirect(next_batch_url)
    return redirect(url_for('batch', n=n))


@app.route("/mark_batch_uninteresting", methods=["POST"])
def mark_batch_uninteresting():
    n = int(request.form["batch"])
    if n < 0 or n >= len(BATCHES):
        abort(404)

    reviewer = getpass.getuser()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for g in BATCHES[n]:
        for d in g['detections']:
            if d['path'] in REVIEWS:
                continue
            REVIEWS[d['path']] = {
                "detection_tile": d['path'],
                "rating": "",
                "reviewer": reviewer,
                "reviewed_at": now,
                "source": "webapp_bulk_uninteresting",
                "L2_ingested": False,
            }
    save_reviews(JSONL_PATH, REVIEWS)

    nxt = next_pending_batch_index(n + 1)
    return redirect(url_for('batch', n=nxt) if nxt is not None else url_for('index'))


@app.route("/image")
def image():
    path = request.args.get("path", "")
    if path not in ALLOWED_PATHS:
        abort(404)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


@app.route("/reload")
def reload_route():
    reload_data()
    return redirect(url_for('index'))


@app.route("/singles")
def singles():
    return redirect(url_for('singles_page', n=0))


@app.route("/singles/<int:n>")
def singles_page(n):
    unrated = [d for d in SINGLETONS if d['path'] not in REVIEWS]
    total_pages = max(1, -(-len(unrated) // SINGLES_PAGE_SIZE))
    if n < 0 or n >= total_pages:
        n = 0
    page = unrated[n * SINGLES_PAGE_SIZE:(n + 1) * SINGLES_PAGE_SIZE]
    return render_template_string(
        SINGLES_TEMPLATE,
        detections=page,
        count=len(unrated),
        n=n,
        total_pages=total_pages,
    )


reload_data()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
