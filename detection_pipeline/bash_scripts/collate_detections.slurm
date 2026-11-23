#!/bin/bash

#SBATCH --job-name=collate_detections
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=00:05:00
#SBATCH --partition=work


if [ -z "$OUTPUT_PATH" ]; then
    echo "Error: OUTPUT_PATH is not defined."
    exit 1
fi


# Shared module versions (see setonix_env.sh). Fail the job loudly if the
# modules don't load, instead of running on the system python3.
source "$MYSOFTWARE/ML-Fireballs/detection_pipeline/bash_scripts/setonix_env.sh" || exit 1
load_setonix_modules || exit 1


bash << 'EOF'

cd $MYSOFTWARE/ML-Fireballs

source .venv/bin/activate || { echo "Error: could not activate .venv (run venv.sh)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "Error: wrong python3 ($(command -v python3), $(python3 --version 2>&1)); module/venv broken" >&2; exit 1; }

python3 -m detection_pipeline.core.collate_detections "$OUTPUT_PATH"

EOF
