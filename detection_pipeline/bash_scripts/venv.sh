#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/setonix_env.sh"

echo "loading $SETONIX_PAWSEYENV_MODULE $SETONIX_PYTORCH_MODULE"
load_setonix_modules || exit 1

echo "entering subshell"
bash << 'EOF'

cd $MYSOFTWARE/ML-Fireballs

echo "creating virtual environment"
python3 -m venv .venv --system-site-packages
source .venv/bin/activate

echo "installing dependencies"

mkdir -p $MYSCRATCH/tmp
export TMPDIR="$MYSCRATCH/tmp"
mkdir -p "$TMPDIR"

pip install --cache-dir="$TMPDIR" -r requirements.txt
pip install --cache-dir="$TMPDIR" onnxruntime

EOF