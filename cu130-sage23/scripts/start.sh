#!/usr/bin/env bash
set -euo pipefail

mkdir -p /workspace/logs

start_nginx() {
    echo "NGINX: starting"
    nginx
}

start_jupyter() {
    local token="${JUPYTER_LAB_PASSWORD:-${JUPYTER_PASSWORD:-}}"

    if [ -z "${token}" ]; then
        echo "JUPYTER: warning: no password/token is configured"
    fi

    echo "JUPYTER: starting on port 8888"
    cd /workspace
    nohup python3.13 -m jupyter lab \
        --allow-root \
        --no-browser \
        --port=8888 \
        --ip=0.0.0.0 \
        --FileContentsManager.delete_to_trash=False \
        --ContentsManager.allow_hidden=True \
        --ServerApp.terminado_settings='{"shell_command":["/bin/bash"]}' \
        --IdentityProvider.token="${token}" \
        --ServerApp.allow_origin='*' \
        --ServerApp.preferred_dir=/workspace \
        > /workspace/logs/jupyter.log 2>&1 &
    JUPYTER_PID=$!

    sleep 2
    if ! kill -0 "${JUPYTER_PID}" 2>/dev/null; then
        cat /workspace/logs/jupyter.log
        exit 1
    fi
}

start_nginx

if [ -f /pre_start.sh ]; then
    echo "PRE-START: running"
    /pre_start.sh
fi

start_jupyter

if [ -f /post_start.sh ]; then
    echo "POST-START: running"
    /post_start.sh
fi

echo "Container is ready"
wait -n
