#!/bin/bash

# Re-runs process_days.sh for every date currently marked "failed" in
# acacia_dfn-l0_processed_days.json, one day at a time, with a longer
# wall time for the fireball_detection.slurm job (useful for nights with
# unusually many images, e.g. the Taurids period, that time out at the
# default 1 hour).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_MODEL_PATH="$SCRIPT_DIR/../../data/2015-trained-entire-year_differenced-norm-tiles_yolov8s-pt.onnx"

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ] || [ "$1" != "--time-limit" ]; then
    echo "Usage: $0 --time-limit HH:MM:SS [model_path]"
    echo "  model_path defaults to $DEFAULT_MODEL_PATH"
    exit 1
fi

time_limit="$2"
model_path=$(realpath "${3:-$DEFAULT_MODEL_PATH}")

STATE_FILE="$SCRIPT_DIR/../../data/acacia_dfn-l0_processed_days.json"
TRACKER="$SCRIPT_DIR/day_tracker.py"
touch "$STATE_FILE"

mapfile -t failed_dates < <(python3 "$TRACKER" list-failed --file "$STATE_FILE")

if [ "${#failed_dates[@]}" -eq 0 ]; then
    echo "No failed days recorded in $STATE_FILE."
    exit 0
fi

echo "Retrying ${#failed_dates[@]} failed day(s) with --time-limit $time_limit: ${failed_dates[*]}"

for date in "${failed_dates[@]}"; do
    echo "=== Retrying $date ==="
    "$SCRIPT_DIR/process_days.sh" --time-limit "$time_limit" "$date" "$date" "$model_path"
done

echo "Done retrying: ${failed_dates[*]}"
