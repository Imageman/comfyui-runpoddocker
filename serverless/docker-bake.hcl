variable "REGISTRY" {
    default = "docker.io"
}

variable "REGISTRY_USER" {
    default = "realizedfantasy"
}

variable "APP" {
    default = "comfyui-runpoddocker"
}

variable "RELEASE" {
    default = "v0.33.1"
}

variable "SERVERLESS_SUFFIX" {
    default = "1"
}

variable "SAGE3_CU128_WHEEL_URL" {
    default = "https://github.com/Comfy-Org/wheels/releases/download/sageattn3-latest/sageattn3-1.0.0%2Bcu128torch2.11-cp312-cp312-manylinux_2_34_x86_64.manylinux_2_35_x86_64.whl"
}

group "default" {
    targets = ["serverless-cu128", "serverless-cu130"]
}

target "serverless-common" {
    context = "."
    dockerfile = "serverless/Dockerfile"
    contexts = {
        comfyui_overlay = "serverless/ComfyUI"
    }
    platforms = ["linux/amd64"]
}

target "serverless-cu128" {
    inherits = ["serverless-common"]
    tags = ["${REGISTRY}/${REGISTRY_USER}/${APP}:cu128-py312-${RELEASE}-serverless${SERVERLESS_SUFFIX}"]
    args = {
        BASE_IMAGE      = "${REGISTRY}/${REGISTRY_USER}/${APP}:cu128-py312-${RELEASE}"
        SAGE3_WHEEL_URL = "${SAGE3_CU128_WHEEL_URL}"
    }
}

target "serverless-cu130" {
    inherits = ["serverless-common"]
    tags = ["${REGISTRY}/${REGISTRY_USER}/${APP}:cu130-py313-sage23-${RELEASE}-serverless${SERVERLESS_SUFFIX}"]
    args = {
        BASE_IMAGE = "${REGISTRY}/${REGISTRY_USER}/${APP}:cu130-py313-sage23-${RELEASE}"
    }
}
