# MiniMax H3 RunPod smoke test

`test_runpod.py` performs the complete test cycle:

1. Loads `s3.env` and `runpod.env` from the parent `serverless` directory.
2. Uploads every configured reference to flat `tmp/` S3 keys.
3. Builds an API-format ComfyUI workflow and submits it asynchronously to
   `https://api.runpod.ai/v2/<DEFAULT_ENDPOINT_ID>/run`.
4. Polls `tmp/<task_id>.json` directly through S3 for at least one hour.
5. Downloads the report, task log, and every report-declared output into
   `out/<task_id>/`.

Run from this directory or the repository root:

```powershell
python serverless/test_example/test_runpod.py
```

All test options are in `test_config.json`; the script has no CLI options.
Leave `task_id` empty to create a timestamp-based ID. Paths in the config are
resolved relative to this directory. `prompt.txt` is loaded as UTF-8 and may
contain arbitrary line breaks.

`generation.length` is the number of output video frames, not a duration in
seconds. MiniMax H3 generates at a fixed 24 fps, so the approximate duration is
`length / 24` seconds. Valid frame counts follow the model grid
`length = 17 * n + 5` (equivalently, `length % 17 == 5`). ComfyUI rounds an
unsupported value upward to the next valid frame count. For example, the
current `length: 56` is valid and produces approximately `56 / 24 = 2.33`
seconds; `124` frames produce approximately 5.17 seconds. The ComfyUI node
documents an approximately 124-362 frame trained range; longer generation is
allowed by the node but is marked as untested in the ComfyUI source.

## Parameter source repositories

The config names intentionally match the inputs of their ComfyUI nodes. Use
these upstream repositories and source files when changing the values:

- [Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI) is the core backend.
  [`nodes_minimax_h3.py`](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_minimax_h3.py)
  defines `MiniMaxH3ReferenceToVideo`, including `width`, `height`, `length`,
  prompt tags, and the `ref_images`, `ref_videos`, `ref_video_audios`, and
  `ref_audios` inputs.
- [`nodes_easycache.py`](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_easycache.py)
  in the same repository defines `easy_cache.reuse_threshold`,
  `start_percent`, `end_percent`, and `verbose`.
- [Saganaki22/ComfyUI-sol-attn](https://github.com/Saganaki22/ComfyUI-sol-attn)
  defines `MiniMaxH3ScheduledSolAttentionPatch` and every option under
  `sol_attention`, including the tau schedule, dense gate, integer attention,
  sink conditioning, and dense block selection.
- [kijai/ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes) provides
  `PathchSageAttentionKJ`; its implementation is in
  [`model_optimization_nodes.py`](https://github.com/kijai/ComfyUI-KJNodes/blob/main/nodes/model_optimization_nodes.py).
- [Kosinkadink/ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite)
  provides `VHS_LoadVideoFFmpeg` and `VHS_VideoCombine`, which load the video
  references and write the final MP4.
- [runpod/runpod-python](https://github.com/runpod/runpod-python) is the RunPod
  Serverless SDK used by the worker, and
  [minio/minio-py](https://github.com/minio/minio-py) is the S3-compatible SDK
  used by `s3_minio.py`.

`runpod.timeout_sec` cannot be lower than 3600 seconds and defaults to 7200.
The Serverless endpoint's own execution timeout must also be configured to at
least the expected inference duration; the client-side timeout cannot prevent
RunPod from terminating a job earlier.

References are supplied as the three arrays `images`, `videos`, and `audios`.
The script creates the corresponding ComfyUI loader nodes and numbered
`ref_*` inputs dynamically. Each video is currently assumed to have an audio
stream: loader output 0 is connected as `ref_videos.ref_video_N`, and output 2
as `ref_video_audios.ref_video_audio_N`. A future version must inspect each
input MP4 and create the audio connection only when an audio stream is actually
present. Until then, use videos that contain audio.

Uploaded input S3 objects are intentionally preserved. The client also keeps
the S3 result report as the durable completion marker and does not rely on
RunPod job-status retention. A failed workflow is still downloaded and then
causes the script to exit with code 1.

Console output is also written by Loguru to `test_runpod.log` beside the batch
file. Loguru rotates the file at 5 MB and retains one rotated log version.

Do not commit `s3.env` or `runpod.env`; both are excluded from the Docker image
and Git by the parent project configuration.
