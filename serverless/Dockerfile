ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root

COPY serverless/requirements.txt /serverless/requirements.txt
RUN /ComfyUI/venv/bin/python -m pip install --no-cache-dir -r /serverless/requirements.txt

# Optional target-specific binary dependency. The cu130 base already contains
# SageAttention3; serverless-cu128 supplies the matching cp312/cu128/torch2.11
# wheel through its Bake target.
ARG SAGE3_WHEEL_URL=""
RUN if [ -n "${SAGE3_WHEEL_URL}" ]; then \
        /ComfyUI/venv/bin/python -m pip install \
            --no-cache-dir --no-deps "${SAGE3_WHEEL_URL}"; \
    fi

COPY serverless/capi.py serverless/s3_minio.py serverless/handler.py /serverless/
COPY --chmod=755 serverless/start_serverless.sh /serverless/start_serverless.sh

# A named context is used so a Windows junction can safely provide the overlay.
COPY --from=comfyui_overlay / /ComfyUI/
RUN find /ComfyUI -type f -name "*.sh" -exec chmod 755 {} +

ENV COMFY_ROOT=/ComfyUI \
    COMFY_URL=127.0.0.1:8188 \
    COMFY_PORT=8188 \
    CONTAINER_LOG=/tmp/comfyui-serverless.log \
    TASK_TMP_ROOT=/tmp/comfyui-serverless \
    S3_PREFIX=tmp

CMD ["/serverless/start_serverless.sh"]
