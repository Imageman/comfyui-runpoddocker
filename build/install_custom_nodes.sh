#!/usr/bin/env bash
set -euo pipefail

NODES_FILE="/nodes.txt"
CONSTRAINTS_FILE="/constraints.txt"
CUSTOM_NODES_DIR="/ComfyUI/custom_nodes"
VENV_PYTHON="/ComfyUI/venv/bin/python"

if [ ! -f "${NODES_FILE}" ]; then
    echo "Custom nodes list not found: ${NODES_FILE}"
    exit 0
fi

mkdir -p "${CUSTOM_NODES_DIR}"

echo "Preinstalling TensorRT for CUDA 12 before custom nodes"
"${VENV_PYTHON}" -m pip install --no-cache-dir "tensorrt-cu12==10.4.0" -c "${CONSTRAINTS_FILE}"

while IFS= read -r line || [ -n "${line}" ]; do
    # Normalize CRLF-authored files before parsing fields on Linux.
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"

    if [ -z "${line}" ] || [[ "${line}" == \#* ]]; then
        continue
    fi

    read -r repo_url folder_name extra <<< "${line}"
    repo_url="${repo_url%$'\r'}"
    folder_name="${folder_name%$'\r'}"
    extra="${extra%$'\r'}"

    if [ -z "${repo_url:-}" ] || [ -z "${folder_name:-}" ]; then
        echo "Skipping malformed nodes.txt line: ${line}"
        continue
    fi

    if [ -n "${extra:-}" ]; then
        echo "Skipping malformed nodes.txt line with extra fields: ${line}"
        continue
    fi

    node_dir="${CUSTOM_NODES_DIR}/${folder_name}"

    echo "Installing custom node: ${repo_url} -> ${node_dir}"
    git clone --depth 1 "${repo_url}" "${node_dir}"

    requirements_file="${node_dir}/requirements.txt"
    if [ -f "${requirements_file}" ]; then
        echo "Installing Python requirements for ${folder_name}"
        "${VENV_PYTHON}" -m pip install --no-cache-dir -r "${requirements_file}" -c "${CONSTRAINTS_FILE}"
    fi
done < "${NODES_FILE}"

"${VENV_PYTHON}" -m pip uninstall -y onnxruntime onnxruntime-gpu
"${VENV_PYTHON}" -m pip install onnx onnxruntime-gpu --extra-index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/
