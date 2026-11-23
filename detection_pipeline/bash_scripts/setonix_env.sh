#!/bin/bash

# The one place that defines the Setonix software environment used by the
# detection pipeline. Sourced by venv.sh, process_days.sh and the .slurm scripts.
#
# When Pawsey rolls out a new software stack, the old pytorch module may only
# be loadable under an older pawseyenv release (Lmod says "exist but cannot be
# loaded as requested ... module load pawseyenv/XXXX.XX pytorch/..."). Either
# switch to a pytorch module from the current stack, or pin the pawseyenv
# release below (empty = use the system default). After changing either,
# rebuild the venv: rm -rf .venv && ./venv.sh

SETONIX_PAWSEYENV_MODULE="${SETONIX_PAWSEYENV_MODULE-}"
SETONIX_PYTORCH_MODULE="${SETONIX_PYTORCH_MODULE:-pytorch/2.13.0-rocm7.2.4}"

# Repo location the .slurm jobs cd into (they don't run from the repo checkout
# the job was submitted from).
SETONIX_REPO_DIR="${SETONIX_REPO_DIR:-$MYSOFTWARE/ML-Fireballs}"

# Load the (optional) pawseyenv + pytorch modules. Returns non-zero (with a message) if
# they didn't load, rather than letting the caller silently fall back to the
# system python3 (3.6 on Setonix, which can't even import dataclasses).
load_setonix_modules() {
    if ! type module >/dev/null 2>&1; then
        echo "Error: the 'module' command is not available in this shell." >&2
        return 1
    fi
    local mods=($SETONIX_PAWSEYENV_MODULE "$SETONIX_PYTORCH_MODULE")
    module load "${mods[@]}" || {
        echo "Error: 'module load ${mods[*]}' failed." >&2
        echo "  Check 'module spider ${SETONIX_PYTORCH_MODULE%%/*}' and update setonix_env.sh." >&2
        return 1
    }
    if ! module is-loaded "$SETONIX_PYTORCH_MODULE" 2>/dev/null; then
        echo "Error: $SETONIX_PYTORCH_MODULE is not loaded after 'module load'." >&2
        echo "  Check 'module spider ${SETONIX_PYTORCH_MODULE%%/*}' and update setonix_env.sh." >&2
        return 1
    fi
}
