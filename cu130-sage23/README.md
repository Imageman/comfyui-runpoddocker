# CUDA 13 / Python 3.13 / SageAttention 2+3 image

This directory is an isolated Docker build path. It does not modify or replace
the existing CUDA 12.4 and CUDA 12.8 Dockerfiles, constraints, or bake targets.

Matrix:

- NVIDIA CUDA 13.0.0 + cuDNN development image on Ubuntu 22.04
- Python 3.13
- PyTorch 2.11.0 + cu130
- SageAttention 2 and SageAttention 3 from SHA256-pinned Astral wheels
- JupyterLab on port 8888
- ComfyUI on port 3001, with nginx proxy on port 3000
- Application Manager on port 8000

Build and push from the repository root without loading the image into the local
Docker image store:

```bash
docker buildx bake -f cu130-sage23/docker-bake.hcl cu130-py313-sage23 --push
```

On Windows, the root-level helper performs the same direct push by default:

```bat
build_cu130.bat
```

The resulting tag is:

```text
realizedfantasy/comfyui-runpoddocker:cu130-py313-sage23-v0.33.1
```

Jupyter accepts either `JUPYTER_LAB_PASSWORD` (compatible with the older image)
or `JUPYTER_PASSWORD`. If neither is set, Jupyter starts without a token and
prints a warning.

The build intentionally reuses only immutable project inputs from the root
context (`ComfyUI/`, node/model lists, nginx config, Application Manager assets,
and the existing pre-start/download scripts). All CUDA 13-specific installation,
constraints, entrypoint, and venv repair logic lives in this directory.
