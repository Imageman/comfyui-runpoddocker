ARG BASE_IMAGE=nvidia/cuda:13.0.0-cudnn-devel-ubuntu22.04
FROM ${BASE_IMAGE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    SHELL=/bin/bash \
    TZ=Etc/UTC

WORKDIR /

ARG PYTHON_VERSION=3.13
COPY --chmod=755 cu130-sage23/build/install_system.sh /install_system.sh
RUN PYTHON_VERSION="${PYTHON_VERSION}" /install_system.sh && rm -f /install_system.sh

COPY cu130-sage23/build/constraints-cu130.txt /constraints-cu130.txt
COPY build/nodes.txt /nodes.txt
COPY build/models.txt /models.txt
COPY --chmod=755 scripts/download_models.sh /download_models.sh
COPY --chmod=755 cu130-sage23/build/install_comfyui.sh /install_comfyui.sh
COPY --chmod=755 cu130-sage23/build/install_custom_nodes.sh /install_custom_nodes.sh
COPY --chmod=755 build/install_app_manager.sh /install_app_manager.sh
COPY --chmod=755 build/install_civitai_model_downloader.sh /install_civitai_model_downloader.sh

ARG COMFYUI_VERSION=v0.33.1
ARG TORCH_VERSION=2.11.0+cu130
ARG TORCHVISION_VERSION=0.26.0+cu130
ARG TORCHAUDIO_VERSION=2.11.0+cu130
ARG INDEX_URL=https://download.pytorch.org/whl/cu130
ARG SAGE2_WHEEL_URL
ARG SAGE3_WHEEL_URL

RUN COMFYUI_VERSION="${COMFYUI_VERSION}" \
    TORCH_VERSION="${TORCH_VERSION}" \
    TORCHVISION_VERSION="${TORCHVISION_VERSION}" \
    TORCHAUDIO_VERSION="${TORCHAUDIO_VERSION}" \
    INDEX_URL="${INDEX_URL}" \
    SAGE2_WHEEL_URL="${SAGE2_WHEEL_URL}" \
    SAGE3_WHEEL_URL="${SAGE3_WHEEL_URL}" \
    /install_comfyui.sh

ARG TENSORRT_VERSION=10.16.1.11
RUN TENSORRT_VERSION="${TENSORRT_VERSION}" /install_custom_nodes.sh

# The local ComfyUI overlay is intentionally part of this image.
COPY ComfyUI/ /ComfyUI/
RUN find /ComfyUI -type f -name "*.sh" -exec chmod 755 {} +

ARG APP_MANAGER_VERSION=2.0.1
RUN APP_MANAGER_VERSION="${APP_MANAGER_VERSION}" /install_app_manager.sh
COPY app-manager/config.json /app-manager/public/config.json
COPY --chmod=755 app-manager/*.sh /app-manager/scripts/

ARG CIVITAI_DOWNLOADER_VERSION=3.0.0
RUN CIVITAI_DOWNLOADER_VERSION="${CIVITAI_DOWNLOADER_VERSION}" /install_civitai_model_downloader.sh

RUN rm -f /install_*.sh

COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY --chmod=755 scripts/pre_start.sh /pre_start.sh
COPY --chmod=755 scripts/start_comfyui.sh /start_comfyui.sh
COPY --chmod=755 cu130-sage23/scripts/fix_venv.sh /fix_venv.sh
COPY --chmod=755 cu130-sage23/scripts/start.sh /start.sh

ARG RELEASE=v0.33.1
ENV TEMPLATE_VERSION=${RELEASE} \
    VENV_PATH=/workspace/ComfyUI/venv

EXPOSE 3000 3001 8000 8888

CMD ["/start.sh"]
