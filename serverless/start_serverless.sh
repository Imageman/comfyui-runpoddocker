#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="${CONTAINER_LOG:-/tmp/comfyui-serverless.log}"
COMFY_PORT="${COMFY_PORT:-8188}"
touch "${LOG_FILE}"

tail -n +1 -F "${LOG_FILE}" &
TAIL_PID=$!

cleanup() {
    kill "${COMFY_PID:-}" "${HANDLER_PID:-}" "${TAIL_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

PYTHONUNBUFFERED=1 /ComfyUI/venv/bin/python -u /ComfyUI/main.py \
    --listen 0.0.0.0 \
    --port "${COMFY_PORT}" \
    --disable-auto-launch >>"${LOG_FILE}" 2>&1 &
COMFY_PID=$!

PYTHONUNBUFFERED=1 /ComfyUI/venv/bin/python -u /serverless/handler.py >>"${LOG_FILE}" 2>&1 &
HANDLER_PID=$!

set +e
wait -n -p EXITED_PID "${COMFY_PID}" "${HANDLER_PID}"
STATUS=$?
set -e

if [ "${EXITED_PID}" = "${COMFY_PID}" ]; then
    echo "ComfyUI exited unexpectedly with status ${STATUS}; stopping the serverless container." >>"${LOG_FILE}"
    if [ "${STATUS}" -eq 0 ]; then
        STATUS=1
    fi
fi
exit "${STATUS}"
