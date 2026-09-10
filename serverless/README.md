# ComfyUI RunPod Serverless worker

Это serverless Runpod реализация Minimax H3 video ref2video.

The worker derives from the existing `cu128-py312` and
`cu130-py313-sage23` images. It runs only the local ComfyUI backend and the
RunPod handler. The `ComfyUI` directory is supplied as a BuildKit named context
because it is a Windows junction.


## Request

Submit an API-format ComfyUI workflow. Input files must already be referenced
by their flat basenames relative to `ComfyUI/input`.

```json
{
  "input": {
    "task_id": "abc123",
    "workflow": {"1": {"inputs": {}, "class_type": "ExampleNode"}},
    "input_s3_keys": ["tmp/source.png", "tmp/audio.wav"]
  }
}
```

`task_id` is required and supplied by the client. Input S3 keys must have
unique basenames.

## S3 contract

The worker reads these production environment variables:

- `RUNPOD_S3_ENDPOINT`
- `RUNPOD_S3_REGION`
- `RUNPOD_S3_BUCKET`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

It downloads inputs, executes the workflow, and uploads artifacts in this
order:

1. `tmp/<task_id>_<output basename>` for every output reported by ComfyUI as
   `images`, `gifs`, or `audio`.
2. `tmp/<task_id>.log` with the log range for that task. The first task also
   includes container and ComfyUI initialization. If this upload fails, report
   publication still proceeds with `log_s3_key: null` and a warning.
3. `tmp/<task_id>.json`, always last, containing `completed` or `failed`, all
   successfully uploaded output keys, timings, warnings, and errors.

After the JSON report is stored, a failed workflow raises an exception so the
RunPod job is marked `FAILED`. Successful RunPod output is intentionally small:

```json
{
  "task_id": "abc123",
  "status": "completed",
  "report_s3_key": "tmp/abc123.json"
}
```

Local input/output/task files are removed after publication. Per-worker
concurrency is fixed at one.

## Interactive debugging

ComfyUI listens on `0.0.0.0:8188`. If HTTP port `8188` is exposed in the
RunPod Pod configuration, the ComfyUI web interface can be opened and used
interactively for debugging. The handler continues to connect locally through
`127.0.0.1:8188`.

ComfyUI has no authentication configured in this image. Expose this port only
for controlled diagnostic sessions; it is not intended as a public production
endpoint.

## Build and push

`build_serverless.bat` directly pushes both images. Edit its
`SERVERLESS_SUFFIX` value, or override it with the first argument:

```bat
build_serverless.bat 2
```

To build and push only the CUDA 12.8 serverless image, use the separate helper:

```bat
build_serverless_cu128.bat 2
```

The CUDA 12.8 target installs the Comfy-Org SageAttention3 wheel built for
Linux x86_64, Python 3.12, CUDA 12.8, and PyTorch 2.11. The CUDA 13 target does
not reinstall it because its base image already contains SageAttention3.

With the default suffix `1`, it publishes:

- `realizedfantasy/comfyui-runpoddocker:cu128-py312-v0.33.1-serverless1`
- `realizedfantasy/comfyui-runpoddocker:cu130-py313-sage23-v0.33.1-serverless1`

The real `.env` test files are excluded from the build and must never be
published in the image.
