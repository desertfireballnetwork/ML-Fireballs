#!/bin/bash

#SBATCH --job-name=fireball_detection
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=128
#SBATCH --time=01:00:00
#SBATCH --partition=work


if [ -z "$FOLDER_PATH" ]; then
    echo "Error: FOLDER_PATH is not defined."
    exit 1
fi

if [ -z "$OUTPUT_PATH" ]; then
    echo "Error: OUTPUT_PATH is not defined."
    exit 1
fi

if [ -z "$MODEL_PATH" ]; then
    echo "Error: MODEL_PATH is not defined."
    exit 1
fi


# Shared module versions (see setonix_env.sh). Fail the job loudly if the
# modules don't load, instead of running on the system python3.
source "$MYSOFTWARE/ML-Fireballs/detection_pipeline/bash_scripts/setonix_env.sh" || exit 1
load_setonix_modules || exit 1

# Don't load other python modules, it messes with the
# package versions that pytorch needs...

# definitely not these
# module load py-pandas/2.1.2
# module load py-scikit-learn/1.3.2

# these might be fine (at your own risk)
# module load py-pip/23.1.2-py3.11.6
# module load py-matplotlib/3.8.1

# important! start bash subshell in the singularity container
bash << 'EOF'

cd $MYSOFTWARE/ML-Fireballs

source .venv/bin/activate || { echo "Error: could not activate .venv (run venv.sh)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "Error: wrong python3 ($(command -v python3), $(python3 --version 2>&1)); module/venv broken" >&2; exit 1; }

# On a hard crash (SIGBUS/SIGSEGV, e.g. exit code 135/139) print the Python
# stack of every thread, so the log shows which file/library it died in.
export PYTHONFAULTHANDLER=1

python3 -m detection_pipeline.main \
    --folder_path "$FOLDER_PATH" \
    --output_path "$OUTPUT_PATH" \
    --model_path "$MODEL_PATH" \
    --detector ONNX \
    --processes "$SLURM_CPUS_PER_TASK" \
    $( [[ "$SAVE_ERRONEOUS" == "true" ]] && echo "--save_erroneous" ) \
    $( [[ "$NO_OVERWRITE" == "true" ]] && echo "--no_overwrite" )

EOF
