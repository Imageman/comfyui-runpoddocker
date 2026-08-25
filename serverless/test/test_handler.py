import json
import sys
from pathlib import Path

import pytest


SERVERLESS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVERLESS_DIR))

import handler as worker  # noqa: E402


class FakeStore:
    def __init__(self):
        self.events = []
        self.reports = {}

    def download(self, key, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"downloaded:{key}".encode())
        self.events.append(("download", key))

    def upload(self, local_path, key):
        self.events.append(("upload", key, Path(local_path).read_bytes()))
        return {"key": key}

    def put_json(self, value, key):
        self.events.append(("json", key))
        self.reports[key] = json.loads(json.dumps(value))
        return {"key": key}


class LogUploadFailingStore(FakeStore):
    def upload(self, local_path, key):
        if key.endswith(".log"):
            self.events.append(("upload_failed", key))
            raise OSError("log storage unavailable")
        return super().upload(local_path, key)


class FakeClient:
    def __init__(self, *, fail=False):
        self.fail = fail

    def open_websocket_connection(self):
        return None

    def queue_prompt(self, _workflow):
        return {"prompt_id": "prompt-1"}

    def track_progress(self, _workflow, _prompt_id):
        if self.fail:
            raise RuntimeError("workflow exploded")

    def get_history(self, _prompt_id):
        return {
            "prompt-1": {
                "status": {"status_str": "success"},
                "outputs": {
                    "9": {
                        "images": [
                            {"filename": "result.png", "subfolder": "", "type": "output"}
                        ],
                        "gifs": [
                            {"filename": "result.mp4", "subfolder": "", "type": "output"}
                        ],
                    }
                },
            }
        }

    def get_image(self, filename, _subfolder, _folder_type):
        return f"bytes:{filename}".encode()

    def close_ws(self):
        return None


@pytest.fixture
def isolated_worker(tmp_path, monkeypatch):
    comfy = tmp_path / "ComfyUI"
    log = tmp_path / "container.log"
    log.write_text("comfy startup\n", encoding="utf-8")
    monkeypatch.setattr(worker, "COMFY_ROOT", comfy)
    monkeypatch.setattr(worker, "COMFY_INPUT", comfy / "input")
    monkeypatch.setattr(worker, "COMFY_OUTPUT", comfy / "output")
    monkeypatch.setattr(worker, "TASK_TMP_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(worker, "CONTAINER_LOG", log)
    return tmp_path


def test_validate_request_rejects_duplicate_input_basenames():
    job = {
        "input": {
            "task_id": "abc123",
            "workflow": {"1": {}},
            "input_s3_keys": ["tmp/a/input.png", "tmp/b/input.png"],
        }
    }
    with pytest.raises(ValueError, match="unique basenames"):
        worker.validate_request(job)


def test_completed_task_uploads_outputs_log_then_report(isolated_worker, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(worker, "make_comfy_client", lambda _task_id: FakeClient())
    result = worker.process_job(
        {
            "input": {
                "task_id": "abc123",
                "workflow": {"1": {"class_type": "LoadImage"}},
                "input_s3_keys": ["tmp/input.png"],
            }
        },
        store,
        0,
    )

    assert result == {
        "task_id": "abc123",
        "status": "completed",
        "report_s3_key": "tmp/abc123.json",
    }
    assert [event[:2] for event in store.events] == [
        ("download", "tmp/input.png"),
        ("upload", "tmp/abc123_result.png"),
        ("upload", "tmp/abc123_result.mp4"),
        ("upload", "tmp/abc123.log"),
        ("json", "tmp/abc123.json"),
    ]
    report = store.reports["tmp/abc123.json"]
    assert report["status"] == "completed"
    assert [item["s3_key"] for item in report["outputs"]] == [
        "tmp/abc123_result.png",
        "tmp/abc123_result.mp4",
    ]
    assert not (worker.COMFY_INPUT / "input.png").exists()


def test_failed_task_uploads_log_and_final_report_then_raises(isolated_worker, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(worker, "make_comfy_client", lambda _task_id: FakeClient(fail=True))

    with pytest.raises(RuntimeError, match="report: tmp/failed1.json"):
        worker.process_job(
            {
                "input": {
                    "task_id": "failed1",
                    "workflow": {"1": {"class_type": "Broken"}},
                    "input_s3_keys": [],
                }
            },
            store,
            0,
        )

    assert [event[:2] for event in store.events] == [
        ("upload", "tmp/failed1.log"),
        ("json", "tmp/failed1.json"),
    ]
    report = store.reports["tmp/failed1.json"]
    assert report["status"] == "failed"
    assert "workflow exploded" in report["errors"][0]


def test_log_upload_failure_does_not_block_final_report(isolated_worker, monkeypatch):
    store = LogUploadFailingStore()
    monkeypatch.setattr(worker, "make_comfy_client", lambda _task_id: FakeClient())

    result = worker.process_job(
        {
            "input": {
                "task_id": "logfail1",
                "workflow": {"1": {"class_type": "Example"}},
                "input_s3_keys": [],
            }
        },
        store,
        0,
    )

    assert result["report_s3_key"] == "tmp/logfail1.json"
    assert ("upload_failed", "tmp/logfail1.log") in store.events
    assert ("json", "tmp/logfail1.json") in store.events
    report = store.reports["tmp/logfail1.json"]
    assert report["status"] == "completed"
    assert report["log_s3_key"] is None
    assert "log storage unavailable" in report["warnings"][0]
