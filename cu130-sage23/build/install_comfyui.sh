#!/usr/bin/env bash
set -euo pipefail

: "${COMFYUI_VERSION:?COMFYUI_VERSION is required}"
: "${TORCH_VERSION:?TORCH_VERSION is required}"
: "${TORCHVISION_VERSION:?TORCHVISION_VERSION is required}"
: "${TORCHAUDIO_VERSION:?TORCHAUDIO_VERSION is required}"
: "${INDEX_URL:?INDEX_URL is required}"
: "${SAGE2_WHEEL_URL:?SAGE2_WHEEL_URL is required}"
: "${SAGE3_WHEEL_URL:?SAGE3_WHEEL_URL is required}"

git clone https://github.com/comfyanonymous/ComfyUI.git /ComfyUI
cd /ComfyUI
git checkout "${COMFYUI_VERSION}"

python3.13 -m venv venv
source venv/bin/activate

python -m pip install --no-cache-dir --upgrade pip wheel "setuptools<82"
python -m pip install --no-cache-dir \
    "torch==${TORCH_VERSION}" \
    "torchvision==${TORCHVISION_VERSION}" \
    "torchaudio==${TORCHAUDIO_VERSION}" \
    --index-url "${INDEX_URL}"

python -m pip install --no-cache-dir -r requirements.txt -c /constraints-cu130.txt
python -m pip install --no-cache-dir accelerate ipykernel -c /constraints-cu130.txt

# Astral wheels are pinned by immutable artifact URL and SHA256 fragment.
python -m pip install --no-cache-dir --no-deps "${SAGE2_WHEEL_URL}"
python -m pip install --no-cache-dir --no-deps "${SAGE3_WHEEL_URL}"

git clone https://github.com/ltdrdata/ComfyUI-Manager.git custom_nodes/ComfyUI-Manager
python -m pip install --no-cache-dir -r custom_nodes/ComfyUI-Manager/requirements.txt -c /constraints-cu130.txt

python -m ipykernel install --prefix=/usr/local --name comfyui --display-name "ComfyUI (Python 3.13)"
python -m pip install --no-cache-dir "numpy>=2.0,<2.8" "setuptools<82"
python -m pip cache purge

deactivate
