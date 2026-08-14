#!/usr/bin/env python3

"""Track which nights the detection pipeline has been run on, and whether it succeeded."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    return data or {}


def save(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def cmd_is_processed(args: argparse.Namespace) -> None:
    entry = load(Path(args.file)).get(args.date)
    sys.exit(0 if entry and entry.get("status") == "success" else 1)


def cmd_list_failed(args: argparse.Namespace) -> None:
    data = load(Path(args.file))
    for date in sorted(d for d, e in data.items() if e.get("status") == "failed"):
        print(date)


def cmd_mark(args: argparse.Namespace) -> None:
    path = Path(args.file)
    data = load(path)
    entry = {
        "run_at": args.run_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": args.status,
    }
    if args.note:
        entry["note"] = args.note
    data[args.date] = entry
    save(path, data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    is_processed = subparsers.add_parser(
        "is-processed", help="exit 0 if the date has a successful run recorded, 1 otherwise"
    )
    is_processed.add_argument("date")
    is_processed.add_argument("--file", required=True)
    is_processed.set_defaults(func=cmd_is_processed)

    list_failed = subparsers.add_parser(
        "list-failed", help="print dates with status=failed, one per line, sorted"
    )
    list_failed.add_argument("--file", required=True)
    list_failed.set_defaults(func=cmd_list_failed)

    mark = subparsers.add_parser("mark", help="record a run for a date")
    mark.add_argument("date")
    mark.add_argument("--file", required=True)
    mark.add_argument("--status", required=True, choices=["success", "failed"])
    mark.add_argument("--note", help="short reason, useful when status is failed")
    mark.add_argument("--run-at", dest="run_at", help="ISO timestamp, defaults to now")
    mark.set_defaults(func=cmd_mark)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
