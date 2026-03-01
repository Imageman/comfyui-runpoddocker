#!/usr/bin/env bash

ARGS=("$@" --listen 0.0.0.0 --port 3001)
LOG_FILE="/workspace/logs/comfyui.log"

export PYTHONUNBUFFERED=1
echo "Starting ComfyUI"
mkdir -p /workspace/logs
cd /workspace/ComfyUI
source venv/bin/activate
TCMALLOC="$(ldconfig -p | grep -Po "libtcmalloc.so.\d" | head -n 1)"
export LD_PRELOAD="${TCMALLOC}"
python3 main.py "${ARGS[@]}" 2>&1 | tee "${LOG_FILE}" &
echo "ComfyUI started"
echo "Log file: ${LOG_FILE}"
deactivate
