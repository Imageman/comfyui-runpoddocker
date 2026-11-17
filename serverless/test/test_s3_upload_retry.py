from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from minio.error import InvalidResponseError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s3_minio


def test_multipart_retries_non_xml_520_on_all_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_minio.time, "sleep", lambda _: None)
    path = tmp_path / "output.mp4"
    path.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    calls = {"create": 0, "part": 0, "complete": 0}

    def fail_once(name, result):
        calls[name] += 1
        if calls[name] == 1:
            raise InvalidResponseError(520, "text/plain", "error code: 520")
        return result

    class Client:
        def _create_multipart_upload(self, *args):
            return fail_once("create", "upload-id")

        def _upload_part(self, *args):
            return fail_once("part", "etag")

        def _complete_multipart_upload(self, *args):
            return fail_once("complete", SimpleNamespace(etag="done"))

        def _abort_multipart_upload(self, *args):
            pytest.fail("successful retry should not abort")

    app = object.__new__(s3_minio.S3MiniApp)
    app.cfg = SimpleNamespace(bucket="bucket", part_size_mb=5)
    app.client = Client()
    assert app.upload(path, "output.mp4")["etag"] == "done"
    assert calls == {"create": 2, "part": 3, "complete": 2}


def test_single_upload_stops_after_ten_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_minio.time, "sleep", lambda _: None)
    path = tmp_path / "small.txt"
    path.write_text("data")
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise InvalidResponseError(520, "text/plain", "error code: 520")

    app = object.__new__(s3_minio.S3MiniApp)
    app.cfg = SimpleNamespace(bucket="bucket", part_size_mb=5)
    app.client = SimpleNamespace(fput_object=fail)
    with pytest.raises(InvalidResponseError):
        app.upload(path, "small.txt")
    assert len(calls) == 11
