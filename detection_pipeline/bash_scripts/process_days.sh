#!/bin/bash

# For each date in the given range (inclusive, YYYYMMDD), unless it already
# has a successful run recorded in acacia_dfn-l0_processed_days.json:
#   - pull that night's images from the Acacia bucket pawsey0639:dfn-l0-<date>
#     (via srun on Setonix's "copy" data-mover partition)
#   - run the detection pipeline on that single night (via process_subfolders.sh)
#   - wait for the jobs to finish
#   - push the results to pawsey0639:dfn-snail-ml-fireball/dfn-l0-<date>/
#   - record the date, when it ran, and whether it succeeded, in acacia_dfn-l0_processed_days.json
#
# Results are only pushed, and a date is only marked "success", if the
# collate job for that night completed successfully. Anything else (no data,
# no camera subfolders, job failure) is recorded as "failed" with a note so
# it gets retried on a later run.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_MODEL_PATH="$SCRIPT_DIR/../../data/2015-trained-entire-year_differenced-norm-tiles_yolov8s-pt.onnx"

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "Usage: $0 <start_date YYYYMMDD> <end_date YYYYMMDD> [model_path]"
    echo "  model_path defaults to $DEFAULT_MODEL_PATH"
    exit 1
fi

start_date="$1"
end_date="$2"
model_path=$(realpath "${3:-$DEFAULT_MODEL_PATH}")

if [ ! -f "$model_path" ]; then
    echo "Error: Model file '$model_path' does not exist."
    exit 1
fi

STATE_FILE="$SCRIPT_DIR/../../data/acacia_dfn-l0_processed_days.json"
TRACKER="$SCRIPT_DIR/day_tracker.py"
touch "$STATE_FILE"

RCLONE_REMOTE="pawsey0639"
RESULTS_BUCKET="dfn-snail-ml-fireball"

# Acacia transfers run on Setonix's dedicated data-mover nodes (partition
# "copy"), not the login node or the "work" compute partition (which has no
# internet egress). srun blocks until the transfer finishes, same as running
# rclone inline.
SRUN_COPY=(srun --partition=copy --time=00:30:00 --ntasks=1 --cpus-per-task=4)

acacia_jpgs_dir="/scratch/$PAWSEY_PROJECT/acacia_JPGs"
results_dir="$MYSCRATCH/acacia_dfn-l0_candidates"

current="$start_date"
while [ "$current" -le "$end_date" ]; do

    if python3 "$TRACKER" is-processed "$current" --file "$STATE_FILE"; then
        echo "$current already processed successfully, skipping."
        current=$(date -d "$current +1 day" +%Y%m%d)
        continue
    fi

    echo "=== Processing $current ==="

    day_folder="$acacia_jpgs_dir/dfn-l0-$current"

    # --local-no-set-modtime: without it, rclone stamps pulled files with
    # Acacia's original object modtime (years old, for this data), which
    # makes them look immediately stale to Setonix's /scratch purge policy.
    echo "+ srun --partition=copy rclone copy --transfers 24 --local-no-set-modtime $RCLONE_REMOTE:dfn-l0-$current/ $day_folder"
    "${SRUN_COPY[@]}" rclone copy --transfers 24 --local-no-set-modtime "$RCLONE_REMOTE:dfn-l0-$current/" "$day_folder"

    if [ ! -d "$day_folder" ]; then
        echo "Error: no data pulled for $current, skipping."
        python3 "$TRACKER" mark "$current" --file "$STATE_FILE" --status failed --note "no data pulled from Acacia"
        current=$(date -d "$current +1 day" +%Y%m%d)
        continue
    fi

    echo "+ $SCRIPT_DIR/process_subfolders.sh $day_folder $results_dir $model_path"
    submit_output=$("$SCRIPT_DIR/process_subfolders.sh" "$day_folder" "$results_dir" "$model_path")
    echo "$submit_output"

    collate_job_id=$(echo "$submit_output" | grep "collate_detections job id:" | awk '{print $NF}')

    if [ -z "$collate_job_id" ]; then
        echo "Error: no collate job was submitted for $current (no camera subfolders?), skipping."
        python3 "$TRACKER" mark "$current" --file "$STATE_FILE" --status failed --note "no camera subfolders / no collate job submitted"
        current=$(date -d "$current +1 day" +%Y%m%d)
        continue
    fi

    echo "+ squeue -j $collate_job_id (polling every 60s until it clears)"
    while squeue -j "$collate_job_id" -h 2>/dev/null | grep -q .; do
        sleep 60
    done

    echo "+ sacct -j $collate_job_id --format=State"
    state=$(sacct -j "$collate_job_id" --format=State --noheader --parsable2 | head -n 1 | tr -d ' ')

    if [ "$state" != "COMPLETED" ]; then
        echo "Error: collate job for $current finished with state '$state', not pushing results."
        python3 "$TRACKER" mark "$current" --file "$STATE_FILE" --status failed --note "collate job state: $state"
        current=$(date -d "$current +1 day" +%Y%m%d)
        continue
    fi

    echo "+ srun --partition=copy rclone copy $results_dir/dfn-l0-$current/ $RCLONE_REMOTE:$RESULTS_BUCKET/dfn-l0-$current/"
    "${SRUN_COPY[@]}" rclone copy "$results_dir/dfn-l0-$current/" "$RCLONE_REMOTE:$RESULTS_BUCKET/dfn-l0-$current/"

    python3 "$TRACKER" mark "$current" --file "$STATE_FILE" --status success
    echo "=== Done $current ==="

    current=$(date -d "$current +1 day" +%Y%m%d)
done
