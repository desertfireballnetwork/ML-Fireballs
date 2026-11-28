#!/usr/bin/env python3

"""
For every night in the processed-days ledger, compare the camera systems
present in the raw data bucket (pawsey0639:dfn-l0-YYYYMMDD/<SYSTEM>/) with
the systems that have outputs in the yearly results bucket
(pawsey0639:dfn-ml-fireball-detection-results-YYYY/dfn-l0-YYYYMMDD/<SYSTEM>/).

A system counts as having outputs if its per-camera log
(<SYSTEM>/dfn-l0-YYYYMMDD_<SYSTEM>.log) is there. Every detection job
writes one, even when it finds nothing, so a missing log means that camera's
results were never pushed.

Read-only: only lists buckets. Run it directly (e.g. on a Setonix login
node, where rclone is configured):

    ./check_results_coverage.py [--all-statuses] [--csv report.csv] [--missing missing.txt]

--missing writes one dfn-l0-YYYYMMDD/SYSTEM per line, the form
reprocess_system.sh takes:

    while read t; do ./reprocess_system.sh "$t"; done < missing.txt

Kept python3.6-compatible, since that is Setonix's system python3.

Exit code 0 if every checked night is complete, 1 otherwise.
"""

import argparse
import csv
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RESULTS_BUCKET_PREFIX = "dfn-ml-fireball-detection-results-"
DEFAULT_LEDGER = Path(__file__).resolve().parent.parent.parent / "data" / "acacia_dfn-l0_processed_days.json"


def rclone_lsf(path, extra):
    """List a remote path. Returns (entries, error). A missing bucket/dir
    is not an error: it just has no entries."""
    proc = subprocess.run(
        ["rclone", "lsf", "--fast-list"] + extra + [path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    if proc.returncode == 0:
        return [l for l in proc.stdout.splitlines() if l], None
    # rclone exit code 3 = directory not found
    if proc.returncode == 3 or "directory not found" in proc.stderr.lower():
        return [], None
    return [], proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "rclone exit %d" % proc.returncode


def check_day(remote, date):
    day = "dfn-l0-" + date

    raw, raw_err = rclone_lsf("%s:%s/" % (remote, day), ["--dirs-only"])
    systems = sorted(d.rstrip("/") for d in raw)

    results_path = "%s:%s%s/%s/" % (remote, RESULTS_BUCKET_PREFIX, date[:4], day)
    logs, res_err = rclone_lsf(results_path, ["-R", "--files-only", "--max-depth", "2", "--include", "/*/*.log"])
    done = set()
    for entry in logs:
        system, _, name = entry.partition("/")
        if name == "%s_%s.log" % (day, system):
            done.add(system)

    return {
        "date": date,
        "systems": systems,
        "missing": [s for s in systems if s not in done],
        "extra": sorted(done - set(systems)),
        "error": raw_err or res_err,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="default: %(default)s")
    parser.add_argument("--remote", default="pawsey0639")
    parser.add_argument("--all-statuses", action="store_true",
                        help="also check nights marked failed (default: only status=success)")
    parser.add_argument("--workers", type=int, default=16, help="parallel rclone listings")
    parser.add_argument("--csv", help="write a per-night report to this CSV file")
    parser.add_argument("--missing", help="write each missing camera as dfn-l0-YYYYMMDD/SYSTEM, one per line "
                                          "(input for reprocess_system.sh)")
    args = parser.parse_args()

    with open(args.ledger) as f:
        ledger = json.load(f) or {}

    dates = sorted(d for d, e in ledger.items() if args.all_statuses or e.get("status") == "success")
    skipped = sorted(d for d in ledger if d not in dates)
    print("Ledger: %d nights, checking %d%s" % (
        len(ledger), len(dates), " (skipping %d not marked success)" % len(skipped) if skipped else ""))
    sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda d: check_day(args.remote, d), dates))

    bad = 0
    for r in results:
        status = ledger[r["date"]].get("status")
        if r["error"]:
            line = "ERROR   listing failed: %s" % r["error"]
        elif not r["systems"]:
            line = "NO-RAW  no systems found in %s:dfn-l0-%s" % (args.remote, r["date"])
        elif r["missing"]:
            line = "MISSING %d/%d systems: %s" % (len(r["missing"]), len(r["systems"]), " ".join(r["missing"]))
        else:
            line = None
        if r["extra"]:
            line = (line + "; " if line else "") + "outputs for systems not in raw data: " + " ".join(r["extra"])
        if line:
            bad += 1
            print("%s [%s] %s" % (r["date"], status, line))

    ok = len(results) - bad
    print("\n%d/%d nights complete, %d with problems" % (ok, len(results), bad))

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "ledger_status", "n_systems", "n_missing", "missing", "extra", "error"])
            for r in results:
                w.writerow([r["date"], ledger[r["date"]].get("status"), len(r["systems"]), len(r["missing"]),
                            " ".join(r["missing"]), " ".join(r["extra"]), r["error"] or ""])
        print("Report written to %s" % args.csv)

    if args.missing:
        targets = ["dfn-l0-%s/%s" % (r["date"], s) for r in results for s in r["missing"]]
        with open(args.missing, "w") as f:
            f.write("".join(t + "\n" for t in targets))
        print("%d missing camera folders written to %s" % (len(targets), args.missing))

    sys.exit(0 if bad == 0 else 1)


if __name__ == "__main__":
    main()
