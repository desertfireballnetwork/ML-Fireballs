#!/bin/bash

# Re-run the detection pipeline on ONE camera system for ONE night, e.g. a
# camera that check_results_coverage.py reports as missing:
#
#   ./reprocess_system.sh [--time-limit HH:MM:SS] dfn-l0-YYYYMMDD/DFNXXXX [model_path]
#
#   - pull pawsey0639:dfn-l0-YYYYMMDD/DFNXXXX/ to scratch (srun on the "copy" partition)
#   - run fireball_detection.slurm on just that folder, wait for it
#   - if it COMPLETED, push the outputs to
#     pawsey0639:dfn-ml-fireball-detection-results-YYYY/dfn-l0-YYYYMMDD/DFNXXXX/
#   - delete the staged images
#
# Leaves the other cameras of that night, the night's collated
# dfn-l0-YYYYMMDD.json, and the processed-days ledger untouched.
# Exit code 0 only if the outputs were pushed.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_MODEL_PATH="$SCRIPT_DIR/../../data/2015-trained-entire-year_differenced-norm-tiles_yolov8s-pt.onnx"

time_limit=""
args=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --time-limit) time_limit="$2"; shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
set -- "${args[@]}"

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 [--time-limit HH:MM:SS] dfn-l0-YYYYMMDD/DFNXXXX [model_path]"
    echo "  model_path defaults to $DEFAULT_MODEL_PATH"
    exit 1
fi

target="${1%/}"
if ! [[ "$target" =~ ^dfn-l0-([0-9]{8})/([A-Za-z0-9_-]+)$ ]]; then
    echo "Error: '$1' is not of the form dfn-l0-YYYYMMDD/DFNXXXX" >&2
    exit 1
fi
date="${BASH_REMATCH[1]}"
system="${BASH_REMATCH[2]}"
day="dfn-l0-$date"

model_path=$(realpath "${2:-$DEFAULT_MODEL_PATH}")
if [ ! -f "$model_path" ]; then
    echo "Error: Model file '$model_path' does not exist." >&2
    exit 1
fi
if [ -z "$PAWSEY_PROJECT" ] || [ -z "$MYSCRATCH" ] || [ -z "$MYSOFTWARE" ]; then
    echo "Error: PAWSEY_PROJECT, MYSCRATCH and MYSOFTWARE must be set." >&2
    exit 1
fi

RCLONE_REMOTE="pawsey0639"
results_bucket="dfn-ml-fireball-detection-results-${date:0:4}"
SRUN_COPY=(srun --partition=copy --time=00:30:00 --ntasks=1 --cpus-per-task=4)

work_dir="$MYSCRATCH/ML-fireballs-detection"
acacia_jpgs_dir="$work_dir/acacia_JPGs"
results_dir="$work_dir/acacia_dfn-l0_candidates"
stage_dir="$acacia_jpgs_dir/$day/$system"
night_out="$results_dir/$day"

cleanup_stage() {
    case "$stage_dir" in
        "$acacia_jpgs_dir"/dfn-l0-[0-9]*/?*) ;;
        *) echo "Refusing to delete unexpected path '$stage_dir'" >&2; return 1 ;;
    esac
    if [ -d "$stage_dir" ]; then
        echo "+ rm -rf $stage_dir"
        rm -rf -- "$stage_dir"
        rmdir "$acacia_jpgs_dir/$day" 2>/dev/null   # only if no other camera is staged there
    fi
}

echo "=== Reprocessing $day/$system ==="
mkdir -p "$stage_dir" "$night_out" || exit 1

echo "+ srun --partition=copy rclone copy --transfers 24 --local-no-set-modtime $RCLONE_REMOTE:$day/$system/ $stage_dir"
if ! "${SRUN_COPY[@]}" rclone copy --transfers 24 --local-no-set-modtime "$RCLONE_REMOTE:$day/$system/" "$stage_dir"; then
    echo "Error: pull of $day/$system failed." >&2
    cleanup_stage; exit 1
fi
if [ -z "$(ls -A "$stage_dir")" ]; then
    echo "Error: no images found in $RCLONE_REMOTE:$day/$system/" >&2
    cleanup_stage; exit 1
fi

# Same submission as process_subfolders.sh. main.py writes to
# $night_out/$system, replacing any old local copy of that camera's outputs.
job_id=$(sbatch \
    --export=ALL,FOLDER_PATH="$stage_dir",OUTPUT_PATH="$night_out",MODEL_PATH="$model_path",SAVE_ERRONEOUS=false \
    --output="$night_out/slurm-%j-$day-$system.out" \
    --account="$PAWSEY_PROJECT" \
    ${time_limit:+--time="$time_limit"} \
    "$MYSOFTWARE/ML-Fireballs/detection_pipeline/bash_scripts/fireball_detection.slurm" | awk '{print $4}')
if [ -z "$job_id" ]; then
    echo "Error: sbatch failed." >&2
    cleanup_stage; exit 1
fi
echo "$day/$system job id: $job_id (log: $night_out/slurm-$job_id-$day-$system.out)"

echo "+ squeue -j $job_id (polling every 60s until it clears)"
while squeue -j "$job_id" -h 2>/dev/null | grep -q .; do
    sleep 60
done

state=$(sacct -j "$job_id" --format=State --noheader --parsable2 | head -n 1 | tr -d ' ')
if [ "$state" != "COMPLETED" ]; then
    echo "Error: job $job_id finished with state '$state', not pushing results." >&2
    cleanup_stage; exit 1
fi

echo "+ srun --partition=copy rclone mkdir $RCLONE_REMOTE:$results_bucket && rclone copy $night_out/$system/ $RCLONE_REMOTE:$results_bucket/$day/$system/"
if ! "${SRUN_COPY[@]}" bash -c 'rclone mkdir "$1" && rclone copy "$2" "$3"' _ \
        "$RCLONE_REMOTE:$results_bucket" \
        "$night_out/$system/" \
        "$RCLONE_REMOTE:$results_bucket/$day/$system/"; then
    echo "Error: push of $day/$system to $results_bucket failed." >&2
    cleanup_stage; exit 1
fi

cleanup_stage
echo "=== Done $day/$system -> $RCLONE_REMOTE:$results_bucket/$day/$system/ ==="
