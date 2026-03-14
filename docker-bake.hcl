# Реестр контейнеров для публикации итогового образа.
variable "REGISTRY" {
    default = "docker.io"
}

# Имя пользователя или организации в реестре.
variable "REGISTRY_USER" {
    default = "realizedfantasy"
}

# Имя репозитория итогового образа.
variable "APP" {
    default = "comfyui-runpoddocker"
}

# Версия ComfyUI, которая checkout'ится во время сборки.
variable "RELEASE" {
    default = "v0.17.1"
}
# старая версия на vast.ai v0.5.1

# Дополнительный суффикс для тегов образов.
variable "RELEASE_SUFFIX" {
    default = ""
}

# Репозиторий базового образа, используемого в FROM.
variable "BASE_IMAGE_REPOSITORY" {
    default = "ashleykza/runpod-base"
}

# Версия базового образа.
variable "BASE_IMAGE_VERSION" {
    default = "2.4.15"
}

# Версия Application Manager, устанавливаемая в образ.
variable "APP_MANAGER_VERSION" {
    default = "1.3.1"
}

# Версия CivitAI Downloader, устанавливаемая в образ.
variable "CIVITAI_DOWNLOADER_VERSION" {
    default = "3.0.0"
}

group "default" {
    targets = ["cu124-py312"]
}

group "all" {
    targets = [
        "cu124-py311",
        "cu124-py312",
        "cu128-py311",
        "cu128-py312"
    ]
}

target "cu124-py311" {
    dockerfile = "Dockerfile"
    tags = ["${REGISTRY}/${REGISTRY_USER}/${APP}:cu124-py311-${RELEASE}${RELEASE_SUFFIX}"]
    args = {
        RELEASE                    = "${RELEASE}"
        BASE_IMAGE                 = "${BASE_IMAGE_REPOSITORY}:${BASE_IMAGE_VERSION}-python3.11-cuda12.4.1-torch2.6.0"
        INDEX_URL                  = "https://download.pytorch.org/whl/cu124"
        TORCH_VERSION              = "2.6.0+cu124"
        XFORMERS_VERSION           = "0.0.29.post3"
        COMFYUI_VERSION            = "${RELEASE}"
        APP_MANAGER_VERSION        = "${APP_MANAGER_VERSION}"
        CIVITAI_DOWNLOADER_VERSION = "${CIVITAI_DOWNLOADER_VERSION}"
    }
    platforms = ["linux/amd64"]
}

target "cu124-py312" {
    dockerfile = "Dockerfile"
    tags = ["${REGISTRY}/${REGISTRY_USER}/${APP}:cu124-py312-${RELEASE}${RELEASE_SUFFIX}"]
    args = {
        RELEASE                    = "${RELEASE}"
        BASE_IMAGE                 = "${BASE_IMAGE_REPOSITORY}:${BASE_IMAGE_VERSION}-python3.12-cuda12.4.1-torch2.6.0"
        INDEX_URL                  = "https://download.pytorch.org/whl/cu124"
        TORCH_VERSION              = "2.6.0+cu124"
        XFORMERS_VERSION           = "0.0.29.post3"
        COMFYUI_VERSION            = "${RELEASE}"
        APP_MANAGER_VERSION        = "${APP_MANAGER_VERSION}"
        CIVITAI_DOWNLOADER_VERSION = "${CIVITAI_DOWNLOADER_VERSION}"
    }
    platforms = ["linux/amd64"]
}

target "cu128-py311" {
    dockerfile = "Dockerfile"
    tags = ["${REGISTRY}/${REGISTRY_USER}/${APP}:cu128-py311-${RELEASE}${RELEASE_SUFFIX}"]
    args = {
        RELEASE                    = "${RELEASE}"
        BASE_IMAGE                 = "${BASE_IMAGE_REPOSITORY}:${BASE_IMAGE_VERSION}-python3.11-cuda12.8.1-torch2.10.0"
        INDEX_URL                  = "https://download.pytorch.org/whl/cu128"
        TORCH_VERSION              = "2.10.0+cu128"
        COMFYUI_VERSION            = "${RELEASE}"
        APP_MANAGER_VERSION        = "${APP_MANAGER_VERSION}"
        CIVITAI_DOWNLOADER_VERSION = "${CIVITAI_DOWNLOADER_VERSION}"
    }
    platforms = ["linux/amd64"]
}

target "cu128-py312" {
    dockerfile = "Dockerfile"
    tags = ["${REGISTRY}/${REGISTRY_USER}/${APP}:cu128-py312-${RELEASE}${RELEASE_SUFFIX}"]
    args = {
        RELEASE                    = "${RELEASE}"
        BASE_IMAGE                 = "${BASE_IMAGE_REPOSITORY}:${BASE_IMAGE_VERSION}-python3.12-cuda12.8.1-torch2.10.0"
        INDEX_URL                  = "https://download.pytorch.org/whl/cu128"
        TORCH_VERSION              = "2.10.0+cu128"
        COMFYUI_VERSION            = "${RELEASE}"
        APP_MANAGER_VERSION        = "${APP_MANAGER_VERSION}"
        CIVITAI_DOWNLOADER_VERSION = "${CIVITAI_DOWNLOADER_VERSION}"
    }
    platforms = ["linux/amd64"]
}
