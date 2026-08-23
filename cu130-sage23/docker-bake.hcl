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

variable "RELEASE_SUFFIX" {
    default = ""
}

group "default" {
    targets = ["cu130-py313-sage23"]
}

target "cu130-py313-sage23" {
    context = "."
    dockerfile = "cu130-sage23/Dockerfile"
    tags = ["${REGISTRY}/${REGISTRY_USER}/${APP}:cu130-py313-sage23-${RELEASE}${RELEASE_SUFFIX}"]
    args = {
        RELEASE              = "${RELEASE}"
        BASE_IMAGE           = "nvidia/cuda:13.0.0-cudnn-devel-ubuntu22.04"
        PYTHON_VERSION       = "3.13"
        COMFYUI_VERSION      = "${RELEASE}"
        INDEX_URL            = "https://download.pytorch.org/whl/cu130"
        TORCH_VERSION        = "2.11.0+cu130"
        TORCHVISION_VERSION  = "0.26.0+cu130"
        TORCHAUDIO_VERSION   = "2.11.0+cu130"
        SAGE2_WHEEL_URL      = "https://wheels.astral.sh/artifacts/355aa4b5ca09527ca40b5bc18a411abbf7124de9951271f2f0724c73bc682e92/sageattention-2.2.0%2Bcu.13.0.torch.2.11-cp313-cp313-manylinux_2_24_x86_64.whl#sha256=355aa4b5ca09527ca40b5bc18a411abbf7124de9951271f2f0724c73bc682e92"
        SAGE3_WHEEL_URL      = "https://wheels.astral.sh/artifacts/ac756773b5dbe9d39ee2f42f7eae21c9f75002e7cc54060127c613e9116c6c5b/sageattn3-2.2.0%2Bcu.13.0.torch.2.11-cp313-cp313-manylinux_2_24_x86_64.whl#sha256=ac756773b5dbe9d39ee2f42f7eae21c9f75002e7cc54060127c613e9116c6c5b"
        TENSORRT_VERSION     = "10.16.1.11"
        APP_MANAGER_VERSION  = "2.0.1"
        CIVITAI_DOWNLOADER_VERSION = "3.0.0"
    }
    platforms = ["linux/amd64"]
}
