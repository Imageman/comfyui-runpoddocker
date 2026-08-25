from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from loguru import logger
from minio.error import S3Error


HERE = Path(__file__).resolve().parent
SERVERLESS_DIR = HERE.parent
sys.path.insert(0, str(SERVERLESS_DIR))

from s3_minio import S3MiniApp, load_cfg  # noqa: E402


CONFIG_PATH = HERE / "test_config.json"
S3_PREFIX = "tmp"
MIN_TIMEOUT_SEC = 3600
CLIENT_LOG_PATH = HERE / "test_runpod.log"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
REFERENCE_FIELDS = (
    "ref_images.",
    "ref_videos.",
    "ref_video_audios.",
    "ref_audios.",
)


def configure_logging(log_path: Path = CLIENT_LOG_PATH) -> None:
    logger.remove()
    logger.add(sys.stderr, colorize=True)
    logger.add(
        log_path,
        encoding="utf-8",
        colorize=False,
        rotation="5 MB",
        retention=1,
    )
    logger.info("Client log: {} (rotation: 5 MB, retained archives: 1)", log_path)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def resolve_local_path(raw: str, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} must be a non-empty path")
    path = Path(raw)
    if not path.is_absolute():
        path = HERE / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    return path


def make_task_id(configured: Any) -> str:
    if configured in (None, ""):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"comfy-{stamp}-{uuid.uuid4().hex[:6]}"
    if not isinstance(configured, str) or not TASK_ID_RE.fullmatch(configured):
        raise ValueError("task_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    return configured


def require_node(workflow: dict[str, Any], node_id: str, class_type: str) -> dict[str, Any]:
    node = workflow.get(node_id)
    if not isinstance(node, dict) or node.get("class_type") != class_type:
        raise ValueError(f"Workflow node {node_id} must be {class_type}")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError(f"Workflow node {node_id} has no inputs object")
    return node


def validate_config(config: dict[str, Any]) -> None:
    refs = config.get("references")
    if not isinstance(refs, dict):
        raise ValueError("references must be an object")
    for kind in ("images", "videos", "audios"):
        if not isinstance(refs.get(kind), list):
            raise ValueError(f"references.{kind} must be an array")
    if len(refs["images"]) > 9:
        raise ValueError("MiniMax H3 accepts at most 9 reference images")
    if len(refs["videos"]) > 3:
        raise ValueError("MiniMax H3 accepts at most 3 reference videos")
    if len(refs["audios"]) > 3:
        raise ValueError("MiniMax H3 accepts at most 3 standalone reference audios")
    if sum(len(refs[kind]) for kind in ("images", "videos", "audios")) > 12:
        raise ValueError("MiniMax H3 accepts at most 12 reference files in total")

    runpod_config = config.get("runpod")
    if not isinstance(runpod_config, dict):
        raise ValueError("runpod must be an object")
    if float(runpod_config.get("timeout_sec", 0)) < MIN_TIMEOUT_SEC:
        raise ValueError(f"runpod.timeout_sec must be at least {MIN_TIMEOUT_SEC}")
    if float(runpod_config.get("poll_interval_sec", 0)) <= 0:
        raise ValueError("runpod.poll_interval_sec must be positive")


def flat_remote_name(task_id: str, kind: str, index: int, local_path: Path) -> str:
    safe_basename = SAFE_NAME_RE.sub("_", local_path.name).strip("._")
    if not safe_basename:
        raise ValueError(f"Cannot derive a safe filename from {local_path.name!r}")
    return f"{task_id}_{kind}_{index + 1}_{safe_basename}"


def next_node_id(workflow: dict[str, Any]) -> str:
    numeric_ids = [int(node_id) for node_id in workflow if str(node_id).isdigit()]
    return str(max(numeric_ids, default=0) + 1)


def add_reference_loaders(
    workflow: dict[str, Any],
    config: dict[str, Any],
    task_id: str,
) -> list[tuple[Path, str]]:
    ref_node = require_node(workflow, "136", "MiniMaxH3ReferenceToVideo")
    ref_inputs = ref_node["inputs"]
    for field in list(ref_inputs):
        if field.startswith(REFERENCE_FIELDS):
            del ref_inputs[field]

    templates = {
        "images": copy.deepcopy(require_node(workflow, "172", "LoadImage")),
        "videos": copy.deepcopy(require_node(workflow, "147", "VHS_LoadVideoFFmpeg")),
        "audios": copy.deepcopy(require_node(workflow, "148", "LoadAudio")),
    }
    for template_id in ("172", "147", "148"):
        workflow.pop(template_id, None)

    uploads: list[tuple[Path, str]] = []
    references = config["references"]
    generation_height = int(config["generation"]["height"])
    for kind in ("images", "videos", "audios"):
        for index, raw_path in enumerate(references[kind]):
            local_path = resolve_local_path(raw_path, f"references.{kind}[{index}]")
            remote_name = flat_remote_name(task_id, kind[:-1], index, local_path)
            remote_key = f"{S3_PREFIX}/{remote_name}"
            uploads.append((local_path, remote_key))

            node_id = next_node_id(workflow)
            loader = copy.deepcopy(templates[kind])
            if kind == "images":
                loader["inputs"]["image"] = remote_name
                ref_inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]
            elif kind == "videos":
                loader["inputs"]["video"] = remote_name
                loader["inputs"]["custom_height"] = generation_height
                ref_inputs[f"ref_videos.ref_video_{index}"] = [node_id, 0]
                # TODO: Inspect each MP4 before building the workflow. Until that is
                # implemented, every video is assumed to contain an audio stream.
                ref_inputs[f"ref_video_audios.ref_video_audio_{index}"] = [node_id, 2]
            else:
                loader["inputs"]["audio"] = remote_name
                ref_inputs[f"ref_audios.ref_audio_{index}"] = [node_id, 0]
            workflow[node_id] = loader
    return uploads


def build_workflow(config: dict[str, Any], task_id: str) -> tuple[dict[str, Any], list[tuple[Path, str]]]:
    workflow_path = resolve_local_path(config["workflow_file"], "workflow_file")
    prompt_path = resolve_local_path(config["prompt_file"], "prompt_file")
    workflow = load_json(workflow_path)

    generation = config["generation"]
    ref_node = require_node(workflow, "136", "MiniMaxH3ReferenceToVideo")
    ref_node["inputs"].update(
        width=int(generation["width"]),
        height=int(generation["height"]),
        length=int(generation["length"]),
    )
    require_node(workflow, "138", "PrimitiveStringMultiline")["inputs"]["value"] = (
        prompt_path.read_text(encoding="utf-8")
    )
    require_node(workflow, "129", "RandomNoise")["inputs"]["noise_seed"] = int(generation["seed"])
    require_node(workflow, "143", "PrimitiveInt")["inputs"]["value"] = int(generation["steps"])
    require_node(workflow, "178", "VHS_VideoCombine")["inputs"]["filename_prefix"] = str(
        generation["output_filename_prefix"]
    )

    require_node(workflow, "150", "MiniMaxH3ScheduledSolAttentionPatch")["inputs"].update(
        config["sol_attention"]
    )
    require_node(workflow, "153", "EasyCache")["inputs"].update(config["easy_cache"])
    uploads = add_reference_loaders(workflow, config, task_id)
    return workflow, uploads


def load_test_environment() -> None:
    for path in (SERVERLESS_DIR / "s3.env", SERVERLESS_DIR / "runpod.env"):
        if not path.is_file():
            raise FileNotFoundError(f"Missing test environment file: {path}")
        load_dotenv(path, override=True)


def submit_job(endpoint_id: str, api_key: str, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    url = f"https://api.runpod.ai/v2/{endpoint_id}/run"
    response = requests.post(
        url,
        headers={
            "accept": "application/json",
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        json=payload,
        timeout=timeout_sec,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError(f"Unexpected RunPod response: {result!r}")
    return result


def is_missing_object(exc: BaseException) -> bool:
    if not isinstance(exc, S3Error):
        return False
    code = str(getattr(exc, "code", "") or "").lower()
    status = getattr(getattr(exc, "response", None), "status", None)
    return code in {"nosuchkey", "nosuchobject", "notfound"} or status == 404


def wait_for_report(store: S3MiniApp, key: str, timeout_sec: float, poll_interval_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            report = store.get_json(key)
            if not isinstance(report, dict):
                raise RuntimeError(f"S3 report {key} is not a JSON object")
            return report
        except S3Error as exc:
            if not is_missing_object(exc):
                raise
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Report {key} did not appear within {timeout_sec:.0f} seconds")
        time.sleep(min(poll_interval_sec, max(0.0, deadline - time.monotonic())))


def download_fresh(store: S3MiniApp, key: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    store.download(key, destination)


def download_report_artifacts(
    store: S3MiniApp,
    report: dict[str, Any],
    report_key: str,
    output_dir: Path,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / Path(report_key).name
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    keys: list[str] = []
    for output in report.get("outputs", []):
        if isinstance(output, dict) and isinstance(output.get("s3_key"), str):
            keys.append(output["s3_key"])
    if isinstance(report.get("log_s3_key"), str):
        keys.append(report["log_s3_key"])

    downloaded = [report_path]
    seen: set[str] = set()
    errors: list[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        destination = output_dir / Path(key.replace("\\", "/")).name
        try:
            download_fresh(store, key, destination)
            downloaded.append(destination)
        except Exception as exc:
            errors.append(f"{key}: {type(exc).__name__}: {exc}")
    if errors:
        raise RuntimeError("Some report artifacts could not be downloaded: " + "; ".join(errors))
    return downloaded


def main() -> int:
    config = load_json(CONFIG_PATH)
    validate_config(config)
    load_test_environment()

    endpoint_id = os.getenv("DEFAULT_ENDPOINT_ID", "").strip()
    api_key = os.getenv("RUNPOD_API_KEY", "").strip()
    if not endpoint_id or not api_key:
        raise ValueError("DEFAULT_ENDPOINT_ID and RUNPOD_API_KEY are required in runpod.env")

    task_id = make_task_id(config.get("task_id"))
    workflow, uploads = build_workflow(config, task_id)
    store = S3MiniApp(load_cfg(load_dotenv_file=False))

    logger.info("task_id: {}", task_id)
    input_s3_keys: list[str] = []
    for local_path, remote_key in uploads:
        logger.info("Uploading input: {} -> {}", local_path.name, remote_key)
        store.upload(local_path, remote_key)
        input_s3_keys.append(remote_key)

    payload = {
        "input": {
            "task_id": task_id,
            "workflow": workflow,
            "input_s3_keys": input_s3_keys,
        }
    }
    runpod_config = config["runpod"]
    response = submit_job(
        endpoint_id,
        api_key,
        payload,
        float(runpod_config.get("submit_timeout_sec", 120)),
    )
    logger.info("RunPod job: {} status={}", response["id"], response.get("status", "unknown"))

    output_root = Path(config.get("output_dir", "out"))
    if not output_root.is_absolute():
        output_root = HERE / output_root
    task_output = output_root / task_id
    task_output.mkdir(parents=True, exist_ok=True)
    (task_output / "runpod_submit.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    report_key = f"{S3_PREFIX}/{task_id}.json"
    report = wait_for_report(
        store,
        report_key,
        float(runpod_config["timeout_sec"]),
        float(runpod_config["poll_interval_sec"]),
    )
    downloaded = download_report_artifacts(store, report, report_key, task_output)
    logger.info("Report status: {}", report.get("status", "unknown"))
    logger.info("Downloaded {} artifact(s) to {}", len(downloaded), task_output)
    return 0 if report.get("status") == "completed" else 1


if __name__ == "__main__":
    configure_logging()
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted; uploaded S3 input objects were intentionally preserved.")
        raise SystemExit(130)
    except Exception as exc:
        logger.exception("{}: {}", type(exc).__name__, exc)
        raise SystemExit(1)
