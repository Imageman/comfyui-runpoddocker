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

COMFY_ARGS=(--listen 127.0.0.1 --port "${COMFY_PORT}" --disable-auto-launch --cache-none)
ARGS_SOURCE="defaults"
if [ -n "${COMFYUI_ARGS:-}" ]; then
    # Parse shell-style quoting without executing shell expressions.
    ARGS_FILE=$(mktemp)
    if /ComfyUI/venv/bin/python - "${ARGS_FILE}" <<'PY'
import os
import shlex
import sys

try:
    args = shlex.split(os.environ["COMFYUI_ARGS"])
    if not args:
        raise ValueError("COMFYUI_ARGS must contain at least one argument")
except ValueError as exc:
    print(f"[startup] Invalid COMFYUI_ARGS: {exc}", file=sys.stderr)
    sys.exit(1)
with open(sys.argv[1], "wb") as output:
    for arg in args:
        output.write(os.fsencode(arg) + b"\0")
PY
    then
        mapfile -d '' -t COMFY_ARGS < "${ARGS_FILE}"
        rm -f "${ARGS_FILE}"
    else
        rm -f "${ARGS_FILE}"
        exit 1
    fi
    ARGS_SOURCE="COMFYUI_ARGS (full override)"
fi
{
    printf '[startup] ComfyUI arguments source: %s\n' "${ARGS_SOURCE}"
    printf '[startup] ComfyUI command:'
    printf ' %q' /ComfyUI/venv/bin/python -u /ComfyUI/main.py "${COMFY_ARGS[@]}"
    printf '\n[startup] Handler COMFY_URL: %s\n' "${COMFY_URL:-127.0.0.1:8188}"
} | tee -a "${LOG_FILE}"

PYTHONUNBUFFERED=1 /ComfyUI/venv/bin/python -u /ComfyUI/main.py \
    "${COMFY_ARGS[@]}" >>"${LOG_FILE}" 2>&1 &
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
