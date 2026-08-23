#!/usr/bin/env bash
set -euo pipefail

NODES_FILE="/nodes.txt"
CONSTRAINTS_FILE="/constraints-cu130.txt"
CUSTOM_NODES_DIR="/ComfyUI/custom_nodes"
VENV_PYTHON="/ComfyUI/venv/bin/python"
TENSORRT_VERSION="${TENSORRT_VERSION:-10.16.1.11}"

if [ ! -f "${NODES_FILE}" ]; then
    echo "Custom nodes list not found: ${NODES_FILE}"
    exit 0
fi

mkdir -p "${CUSTOM_NODES_DIR}"

# CUDA 13 package. The pinned RIFE node still requests tensorrt==10.4.0,
# so its requirements are handled explicitly below instead of installing
# that CUDA 12-era package.
"${VENV_PYTHON}" -m pip install --no-cache-dir \
    "tensorrt-cu13==${TENSORRT_VERSION}"

while IFS= read -r line || [ -n "${line}" ]; do
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

    if [ -z "${repo_url:-}" ] || [ -z "${folder_name:-}" ] || [ -n "${extra:-}" ]; then
        echo "Skipping malformed nodes.txt line: ${line}"
        continue
    fi

    node_dir="${CUSTOM_NODES_DIR}/${folder_name}"
    echo "Installing custom node: ${repo_url} -> ${node_dir}"
    git clone --depth 1 "${repo_url}" "${node_dir}"

    if [ "${folder_name}" = "ComfyUI-Rife-Tensorrt" ]; then
        echo "Pinning ${folder_name} to e971cfb71ac88ff1be6abd5bb8b4f04fdf110a88"
        cd "${node_dir}"
        git fetch --depth 1 origin e971cfb71ac88ff1be6abd5bb8b4f04fdf110a88
        git checkout e971cfb71ac88ff1be6abd5bb8b4f04fdf110a88
        test "$(git rev-parse HEAD)" = "e971cfb71ac88ff1be6abd5bb8b4f04fdf110a88"

        "${VENV_PYTHON}" -m pip install --no-cache-dir \
            einops colored polygraphy cuda-python \
            -c "${CONSTRAINTS_FILE}"
        continue
    fi

    requirements_file="${node_dir}/requirements.txt"
    if [ -f "${requirements_file}" ]; then
        "${VENV_PYTHON}" -m pip install --no-cache-dir \
            -r "${requirements_file}" \
            -c "${CONSTRAINTS_FILE}"
    fi
done < "${NODES_FILE}"

"${VENV_PYTHON}" -m pip uninstall -y onnxruntime onnxruntime-gpu || true
"${VENV_PYTHON}" -m pip install --no-cache-dir onnx onnxruntime-gpu -c "${CONSTRAINTS_FILE}"
"${VENV_PYTHON}" -m pip install --no-cache-dir "setuptools<82"
if ! "${VENV_PYTHON}" -m pip check; then
    echo "WARNING: pip check reported custom-node metadata conflicts"
fi
