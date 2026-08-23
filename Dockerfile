ARG BASE_IMAGE=ashleykza/runpod-base:2.6.0-python3.12-cuda12.8.1-torch2.11.0
FROM ${BASE_IMAGE}

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends aria2 && rm -rf /var/lib/apt/lists/*

# Copy the build scripts
WORKDIR /
COPY --chmod=755 build/* ./

# Install ComfyUI
ARG TORCH_VERSION
ARG XFORMERS_VERSION
ARG INDEX_URL
ARG COMFYUI_VERSION
ARG CONSTRAINTS_FILENAME=constraints.txt
RUN /install_comfyui.sh
RUN CONSTRAINTS_FILENAME="${CONSTRAINTS_FILENAME}" /install_custom_nodes.sh
COPY ComfyUI/ /ComfyUI/
# enable run any .sh files in folder (for download large files)
RUN find /ComfyUI -type f -name "*.sh" -exec chmod 755 {} +

# Install Application Manager
ARG APP_MANAGER_VERSION
RUN /install_app_manager.sh
COPY app-manager/config.json /app-manager/public/config.json
COPY --chmod=755 app-manager/*.sh /app-manager/scripts/

# Install CivitAI Model Downloader
ARG CIVITAI_DOWNLOADER_VERSION
RUN /install_civitai_model_downloader.sh

# Cleanup installation scripts
RUN rm -f /install_*.sh

# Remove existing SSH host keys
RUN rm -f /etc/ssh/ssh_host_*

# NGINX Proxy
COPY nginx/nginx.conf /etc/nginx/nginx.conf

# Set template version
ARG RELEASE
ENV TEMPLATE_VERSION=${RELEASE}

# Set the main venv path
ARG VENV_PATH
ENV VENV_PATH=${VENV_PATH}

# Copy the scripts
WORKDIR /
COPY --chmod=755 scripts/* ./

# Start the container
SHELL ["/bin/bash", "--login", "-c"]
CMD [ "/start.sh" ]
