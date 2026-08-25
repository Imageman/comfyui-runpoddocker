from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "serverless" / "test_example" / "test_runpod.py"
SPEC = importlib.util.spec_from_file_location("test_example_runpod", SCRIPT)
assert SPEC and SPEC.loader
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


def base_config() -> dict:
    return json.loads((client.HERE / "test_config.json").read_text(encoding="utf-8"))


def test_build_workflow_rewrites_parameters_and_references() -> None:
    config = base_config()
    task_id = "test-20260825"

    workflow, uploads = client.build_workflow(config, task_id)

    assert workflow["136"]["inputs"]["width"] == 1344
    assert workflow["138"]["inputs"]["value"].startswith("subject_definitions:\n")
    assert workflow["129"]["inputs"]["noise_seed"] == 12345
    assert workflow["143"]["inputs"]["value"] == 20
    assert workflow["178"]["inputs"]["filename_prefix"] == "minimax_output"
    assert workflow["150"]["inputs"]["tau_start"] == 1.9
    assert workflow["153"]["inputs"]["reuse_threshold"] == 0.15

    inputs = workflow["136"]["inputs"]
    image_id = inputs["ref_images.ref_image_0"][0]
    video_id = inputs["ref_videos.ref_video_0"][0]
    audio_id = inputs["ref_audios.ref_audio_0"][0]
    assert inputs["ref_video_audios.ref_video_audio_0"] == [video_id, 2]
    assert workflow[image_id]["inputs"]["image"].startswith(f"{task_id}_image_1_")
    assert workflow[video_id]["inputs"]["video"].startswith(f"{task_id}_video_1_")
    assert workflow[audio_id]["inputs"]["audio"].startswith(f"{task_id}_audio_1_")
    assert len(uploads) == 3
    assert all(key.startswith(f"tmp/{task_id}_") for _, key in uploads)
    assert len({Path(key).name for _, key in uploads}) == 3
    assert not {"147", "148", "172"}.intersection(workflow)


def test_empty_reference_arrays_remove_template_connections() -> None:
    config = base_config()
    config["references"] = {"images": [], "videos": [], "audios": []}

    workflow, uploads = client.build_workflow(config, "empty-refs")

    assert uploads == []
    assert not any(
        field.startswith(client.REFERENCE_FIELDS) for field in workflow["136"]["inputs"]
    )


def test_timeout_must_be_at_least_one_hour() -> None:
    config = base_config()
    config["runpod"]["timeout_sec"] = 3599

    with pytest.raises(ValueError, match="at least 3600"):
        client.validate_config(config)


def test_logging_uses_loguru_console_and_rotating_file(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(client.logger, "remove", lambda: calls.append(("remove", (), {})))
    monkeypatch.setattr(
        client.logger,
        "add",
        lambda *args, **kwargs: calls.append(("add", args, kwargs)),
    )
    monkeypatch.setattr(client.logger, "info", lambda *args, **kwargs: None)

    path = tmp_path / "client.log"
    client.configure_logging(path)

    assert calls[0][0] == "remove"
    console = calls[1]
    file_sink = calls[2]
    assert console[1] == (client.sys.stderr,)
    assert console[2]["colorize"] is True
    assert file_sink[1] == (path,)
    assert file_sink[2]["colorize"] is False
    assert file_sink[2]["rotation"] == "5 MB"
    assert file_sink[2]["retention"] == 1
