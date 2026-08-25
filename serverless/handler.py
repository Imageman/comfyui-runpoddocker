from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import runpod
from loguru import logger

import capi
from s3_minio import S3MiniApp, load_cfg


COMFY_ROOT = Path(os.getenv("COMFY_ROOT", "/ComfyUI"))
COMFY_INPUT = COMFY_ROOT / "input"
COMFY_OUTPUT = COMFY_ROOT / "output"
TASK_TMP_ROOT = Path(os.getenv("TASK_TMP_ROOT", "/tmp/comfyui-serverless"))
CONTAINER_LOG = Path(os.getenv("CONTAINER_LOG", "/tmp/comfyui-serverless.log"))
COMFY_URL = os.getenv("COMFY_URL", "127.0.0.1:8188")
S3_PREFIX = os.getenv("S3_PREFIX", "tmp").strip("/")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_handler_lock = threading.Lock()
_first_task = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def s3_key(name: str) -> str:
    return f"{S3_PREFIX}/{name}" if S3_PREFIX else name


def validate_request(job: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
    payload = job.get("input")
    if not isinstance(payload, dict):
        raise ValueError("input must be an object")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        raise ValueError("workflow must be a non-empty ComfyUI API-format object")
    keys = payload.get("input_s3_keys", [])
    if not isinstance(keys, list) or not all(isinstance(key, str) and key for key in keys):
        raise ValueError("input_s3_keys must be an array of non-empty strings")
    basenames = [Path(key.replace("\\", "/")).name for key in keys]
    if any(not name or name in {".", ".."} for name in basenames):
        raise ValueError("every input S3 key must have a filename")
    if len(set(basenames)) != len(basenames):
        raise ValueError("input S3 keys must have unique basenames")
    return task_id, workflow, keys


def make_comfy_client(task_id: str) -> capi.ConnectionManager:
    return capi.ConnectionManager(
        server_url=COMFY_URL,
        open_button_token=os.getenv("OPEN_BUTTON_TOKEN", ""),
        use_https=False,
        use_wss=False,
        job_name=task_id,
    )


def wait_for_comfyui(timeout_sec: float = 300.0) -> None:
    deadline = time.monotonic() + timeout_sec
    client = make_comfy_client("startup")
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            stats = client.get_system_stats()
            logger.info("ComfyUI is ready: {}", str(stats)[:500])
            return
        except BaseException as exc:
            last_error = exc
            time.sleep(1.0)
    raise TimeoutError(f"ComfyUI did not become ready within {timeout_sec}s") from last_error


def unique_output_name(task_id: str, original: str, used: set[str]) -> str:
    base = Path(original).name
    candidate = f"{task_id}_{base}"
    index = 2
    while candidate in used:
        candidate = f"{task_id}_{index}_{base}"
        index += 1
    used.add(candidate)
    return candidate


def collect_outputs(
    client: capi.ConnectionManager,
    history: dict[str, Any],
    task_dir: Path,
    task_id: str,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    used: set[str] = set()
    for node_id, node_output in history.get("outputs", {}).items():
        for kind in ("images", "gifs", "audio"):
            for descriptor in node_output.get(kind, []):
                if descriptor.get("type") != "output" or not descriptor.get("filename"):
                    continue
                original = Path(str(descriptor["filename"])).name
                name = unique_output_name(task_id, original, used)
                local_path = task_dir / name
                data = client.get_image(
                    descriptor["filename"],
                    descriptor.get("subfolder", ""),
                    descriptor["type"],
                )
                local_path.write_bytes(data)
                outputs.append(
                    {
                        "filename": name,
                        "original_filename": original,
                        "s3_key": s3_key(name),
                        "bytes": len(data),
                        "content_type": mimetypes.guess_type(original)[0] or "application/octet-stream",
                        "node_id": str(node_id),
                        "kind": kind,
                        "local_path": local_path,
                        "descriptor": descriptor,
                        "uploaded": False,
                    }
                )
    return outputs


def history_errors(history: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    status = history.get("status")
    if isinstance(status, dict) and status.get("status_str") not in (None, "success"):
        errors.append(f"ComfyUI status: {status.get('status_str')}")
    node_errors = history.get("node_errors")
    if node_errors:
        errors.append(f"ComfyUI node_errors: {json.dumps(node_errors, ensure_ascii=False)[:4000]}")
    return errors


def read_log_slice(offset: int, destination: Path) -> None:
    with CONTAINER_LOG.open("rb") as source, destination.open("wb") as target:
        source.seek(offset)
        shutil.copyfileobj(source, target)


def remove_comfy_output(descriptor: dict[str, Any]) -> None:
    subfolder = str(descriptor.get("subfolder", ""))
    candidate = (COMFY_OUTPUT / subfolder / Path(str(descriptor["filename"])).name).resolve()
    output_root = COMFY_OUTPUT.resolve()
    if candidate != output_root and output_root in candidate.parents:
        candidate.unlink(missing_ok=True)


def process_job(job: dict[str, Any], store: S3MiniApp, log_offset: int) -> dict[str, Any]:
    started = time.monotonic()
    started_at = utc_now()
    task_id = "invalid-task"
    input_keys: list[str] = []
    downloaded_inputs: list[Path] = []
    output_records: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    prompt_id: str | None = None
    failure: BaseException | None = None
    task_dir: Path | None = None
    client: capi.ConnectionManager | None = None

    try:
        task_id, workflow, input_keys = validate_request(job)
        task_dir = TASK_TMP_ROOT / task_id
        task_dir.mkdir(parents=True, exist_ok=False)
        COMFY_INPUT.mkdir(parents=True, exist_ok=True)
        for key in input_keys:
            destination = COMFY_INPUT / Path(key.replace("\\", "/")).name
            store.download(key, destination)
            downloaded_inputs.append(destination)

        client = make_comfy_client(task_id)
        client.open_websocket_connection()
        response = client.queue_prompt(workflow)
        prompt_id = response["prompt_id"]
        client.track_progress(workflow, prompt_id)
        history = client.get_history(prompt_id)[prompt_id]
        errors.extend(history_errors(history))
        if errors:
            raise RuntimeError("; ".join(errors))

        output_records = collect_outputs(client, history, task_dir, task_id)
        if not output_records:
            warnings.append("ComfyUI history contains no downloadable images, gifs, or audio outputs")
        for record in output_records:
            store.upload(record["local_path"], record["s3_key"])
            record["uploaded"] = True
    except BaseException as exc:
        failure = exc
        if not errors or str(exc) not in errors:
            errors.append(f"{type(exc).__name__}: {exc}")
        logger.exception("Task {} failed", task_id)
    finally:
        if client is not None:
            client.close_ws()

    ended_at = utc_now()
    duration_sec = round(time.monotonic() - started, 3)
    if task_dir is None:
        task_dir = TASK_TMP_ROOT / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
    log_name = f"{task_id}.log"
    log_path = task_dir / log_name
    logger.info("Task {} finished status={} duration_sec={}", task_id, "failed" if failure else "completed", duration_sec)
    read_log_slice(log_offset, log_path)
    log_key = s3_key(log_name)

    report_outputs = [
        {
            key: value
            for key, value in record.items()
            if key not in {"local_path", "descriptor", "uploaded"}
        }
        for record in output_records
        if record["uploaded"]
    ]
    report = {
        "task_id": task_id,
        "status": "failed" if failure else "completed",
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_sec": duration_sec,
        "prompt_id": prompt_id,
        "input_s3_keys": input_keys,
        "outputs": report_outputs,
        "log_s3_key": log_key,
        "warnings": warnings,
        "errors": errors,
    }
    report_key = s3_key(f"{task_id}.json")

    try:
        try:
            store.upload(log_path, log_key)
        except Exception as exc:
            report["log_s3_key"] = None
            warnings.append(f"Log upload failed: {type(exc).__name__}: {exc}")
            logger.exception("Failed to upload task log {}", log_key)
        store.put_json(report, report_key)
    finally:
        for path in downloaded_inputs:
            path.unlink(missing_ok=True)
        for record in output_records:
            try:
                remove_comfy_output(record["descriptor"])
            except Exception as exc:
                logger.warning("Failed to remove ComfyUI output {}: {}", record["descriptor"], exc)
        shutil.rmtree(task_dir, ignore_errors=True)

    if failure is not None:
        raise RuntimeError(f"Task {task_id} failed; report: {report_key}") from failure
    return {"task_id": task_id, "status": "completed", "report_s3_key": report_key}


def handler(job: dict[str, Any]) -> dict[str, Any]:
    global _first_task
    with _handler_lock:
        log_offset = 0 if _first_task else CONTAINER_LOG.stat().st_size
        _first_task = False
        store = S3MiniApp(load_cfg(load_dotenv_file=False))
        return process_job(job, store, log_offset)


if __name__ == "__main__":
    TASK_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    wait_for_comfyui(float(os.getenv("COMFY_STARTUP_TIMEOUT_SEC", "300")))
    runpod.serverless.start({"handler": handler, "concurrency_modifier": lambda _current: 1})
