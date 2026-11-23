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

time_limit=""
args=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --time-limit)
            time_limit="$2"
            shift 2
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done
set -- "${args[@]}"

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "Usage: $0 [--time-limit HH:MM:SS] <start_date YYYYMMDD> <end_date YYYYMMDD> [model_path]"
    echo "  model_path defaults to $DEFAULT_MODEL_PATH"
    echo "  --time-limit overrides fireball_detection.slurm's wall time (default 01:00:00)"
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

# ---------------------------------------------------------------------------
# Preflight: make sure the environment is sane before pulling any data or
# submitting any jobs. Catches e.g. a Setonix software-stack update that
# breaks the pytorch module, in which case the slurm jobs would otherwise
# fall back to the system python3.6 and every night would fail.
# ---------------------------------------------------------------------------
source "$SCRIPT_DIR/setonix_env.sh"

preflight_checks() {
    local ok=0
    fail() { echo "  FAIL: $*" >&2; ok=1; }
    pass() { echo "  ok:   $*"; }

    echo "=== Preflight checks ==="

    local var
    for var in PAWSEY_PROJECT MYSCRATCH MYSOFTWARE; do
        if [ -n "${!var}" ]; then pass "\$$var=${!var}"; else fail "\$$var is not set"; fi
    done

    local cmd
    for cmd in python3 sbatch srun squeue sacct rclone realpath; do
        if command -v "$cmd" >/dev/null 2>&1; then pass "$cmd found"; else fail "'$cmd' not found on PATH"; fi
    done

    if command -v rclone >/dev/null 2>&1; then
        if rclone listremotes 2>/dev/null | grep -qx "$RCLONE_REMOTE:"; then
            pass "rclone remote '$RCLONE_REMOTE' configured"
        else
            fail "rclone remote '$RCLONE_REMOTE' is not configured (rclone listremotes)"
        fi
    fi

    if python3 "$TRACKER" list-failed --file "$STATE_FILE" >/dev/null; then
        pass "day tracker can read $STATE_FILE"
    else
        fail "day tracker failed on $STATE_FILE"
    fi

    # The slurm jobs run from $SETONIX_REPO_DIR, not necessarily from this checkout.
    if [ ! -d "$SETONIX_REPO_DIR" ]; then
        fail "repo dir $SETONIX_REPO_DIR (used by the slurm jobs) does not exist"
    elif [ "$(realpath "$SETONIX_REPO_DIR")" != "$(realpath "$SCRIPT_DIR/../..")" ]; then
        echo "  warn: slurm jobs will run code from $SETONIX_REPO_DIR, not from $(realpath "$SCRIPT_DIR/../..")"
    fi
    if [ ! -f "$SETONIX_REPO_DIR/.venv/bin/activate" ]; then
        fail "no virtualenv at $SETONIX_REPO_DIR/.venv (run venv.sh)"
    fi

    # Load the modules exactly as the slurm jobs do, in a subshell so this
    # shell's environment is left untouched, then check the venv's python can
    # import everything the pipeline needs.
    echo "  ...  loading ${SETONIX_PAWSEYENV_MODULE:+$SETONIX_PAWSEYENV_MODULE }$SETONIX_PYTORCH_MODULE and test-importing the pipeline (can take a minute)"
    if (
        load_setonix_modules || exit 1
        bash << 'EOF'
cd "$SETONIX_REPO_DIR" || exit 1
source .venv/bin/activate || exit 1
python3 - << 'PY'
import sys
print(f"  ok:   python {sys.version.split()[0]} at {sys.executable}")
if sys.version_info < (3, 8):
    sys.exit("  FAIL: python is too old; module or venv is broken")
import torch, onnxruntime
print(f"  ok:   torch {torch.__version__}, onnxruntime {onnxruntime.__version__}")
import detection_pipeline.main
import detection_pipeline.core.collate_detections
print("  ok:   detection_pipeline imports")
PY
EOF
    ); then
        pass "module + venv environment"
    else
        fail "module/venv environment is broken (see errors above). If the pytorch module changed, update setonix_env.sh and rebuild: rm -rf $SETONIX_REPO_DIR/.venv && $SCRIPT_DIR/venv.sh"
    fi

    if [ "$ok" -ne 0 ]; then
        echo "=== Preflight FAILED, not processing anything ===" >&2
    else
        echo "=== Preflight passed ==="
    fi
    return "$ok"
}

export SETONIX_REPO_DIR
preflight_checks || exit 1

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

    echo "+ $SCRIPT_DIR/process_subfolders.sh $day_folder $results_dir $model_path false $time_limit"
    submit_output=$("$SCRIPT_DIR/process_subfolders.sh" "$day_folder" "$results_dir" "$model_path" false "$time_limit")
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
