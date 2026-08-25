"""
s3_minio_app.py — мини-приложение для работы с RunPod S3 (S3-compatible) через MinIO SDK.

Основной функционал реализован классом S3MiniApp:
- upload(): загрузка локального файла/директории в S3 (multipart, ретраи; скорость логируется на DEBUG)
- download(): скачивание файла или префикса из S3 с докачкой (resume) в ./tmp/tmp_s3_*.part; скорость логируется на DEBUG
- check(): быстрый stat объекта (размер, etag, время модификации, content-type)
- delete(): удаление объекта
- delete_old(): удаление старых объектов по prefix и возрасту (в часах)
- list(): листинг по prefix
- status(): быстрые служебные сведения (endpoint/region/bucket, bucket_exists, 5 первых ключей)
- recursive_status(): расширенный статус (включая число объектов и суммарный объем)

Дополнительно:
- Авто-очистка старых part-файлов (./tmp/tmp_s3_*.part) при status(), recursive_status() и при запуске demo (--demo)

Зависимости:
    pip install minio tenacity python-dotenv loguru
"""

from __future__ import annotations

import argparse
import io
import json
import hashlib
import mimetypes
import os
import random
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import certifi
from dotenv import load_dotenv
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception
import urllib3

from minio import Minio
from minio.datatypes import Part, parse_list_objects
from minio.error import S3Error


# ---------------------------- Defaults in code (as requested) ----------------------------

DEFAULT_LOG_LEVEL: str = "DEBUG"
DEFAULT_PART_SIZE_MB: int = 16  # For typical 1–150 MB objects, 16 MiB is a good compromise.
MIN_PART_SIZE_MB: int = 5  # S3 multipart minimum part size.
MAX_PART_SIZE_MB: int = 5 * 1024  # 5 GiB S3 multipart maximum part size.
MAX_MULTIPART_PARTS: int = 10_000  # S3 hard limit.
TARGET_MAX_PARTS: int = 8_000  # Keep a safety margin under MAX_MULTIPART_PARTS.
DEFAULT_TMP_CLEANUP_HOURS: int = 24  # Remove resumable part files older than N hours.
DEFAULT_HTTP_TIMEOUT_SEC: int = 20  # Debug timeout: cap per-request network time.
RANGE_FALLBACK_LENGTH_BYTES: int = 1
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_PARALLEL_UPLOADS: int = 3
MAX_RETRY_COUNT: int = 10
MAX_RETRY_ATTEMPTS: int = MAX_RETRY_COUNT + 1
SMALL_FILE_THRESHOLD_BYTES: int = 64 * 1024 * 1024
SMALL_FILE_PART_SIZE_MB: int = 5
LARGE_FILE_PART_SIZE_MB: int = 8
ADAPTIVE_MIN_PART_SIZE_MB: int = 1
LIST_MAX_KEYS: Optional[int] = None  # None uses S3 default (typically 1000).
LIST_MAX_PAGES: int = 0  # 0 means no hard limit.
LIST_USE_API_V1: bool = False  # Use ListObjects V2 by default.
TMP_PART_PREFIX: str = "tmp_s3_"
TMP_DIR_NAME: str = "tmp"


# ---------------------------- Config ----------------------------

@dataclass(frozen=True)
class S3Cfg:
    """
    Runtime configuration for S3 client.

    Fields:
        endpoint: S3 endpoint host (no scheme), e.g. "s3api-us-ca-2.runpod.io"
        region: region string, e.g. "us-ca-2"
        bucket: bucket name, e.g. "n1ii97ynil"
        access_key: AWS_ACCESS_KEY_ID
        secret_key: AWS_SECRET_ACCESS_KEY
        secure: True for HTTPS, False for HTTP
        part_size_mb: base multipart part size for uploads, in MiB (auto-adjusted)

    Example:
        cfg = S3Cfg(
            endpoint="s3api-us-ca-2.runpod.io",
            region="us-ca-2",
            bucket="n1ii97ynil",
            access_key="AKIA...",
            secret_key="....",
            secure=True,
            part_size_mb=16,
        )
    """

    endpoint: str
    region: Optional[str]
    bucket: str
    access_key: str
    secret_key: str
    secure: bool
    part_size_mb: int


@dataclass(frozen=True)
class StatInfo:
    """
    Minimal stat info for object metadata (safe for fallback parsing).
    """

    size: int
    etag: Optional[str]
    last_modified: Optional[datetime]
    content_type: Optional[str]
    metadata: Dict[str, Any]
    version_id: Optional[str]


class RetryBudget:
    """Thread-safe retry budget shared by multipart operations."""

    def __init__(self, retries: int) -> None:
        """Initialize the budget with a maximum number of retries."""
        self._remaining = retries
        self._lock = Lock()

    @property
    def remaining(self) -> int:
        """Return the number of unused HTTP attempts."""
        with self._lock:
            return self._remaining

    def consume_retry(self) -> bool:
        """Consume one retry, returning False when the budget is exhausted."""
        with self._lock:
            if self._remaining <= 0:
                return False
            self._remaining -= 1
            return True


def _env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    """
    Return the first non-empty env var value among names.

    Example:
        os.environ["A"] = "x"
        v = _env_first("B", "A")  # -> "x"
    """
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v.strip()
    return default


def _parse_endpoint(raw: str) -> Tuple[str, bool]:
    """
    Parse endpoint and decide HTTPS flag.

    Accepts:
        - "https://s3api-us-ca-2.runpod.io"
        - "s3api-us-ca-2.runpod.io"
        - "http://host:9000"

    Returns:
        (endpoint_without_scheme, secure)

    Example:
        _parse_endpoint("https://x.y") -> ("x.y", True)
        _parse_endpoint("http://x.y:9000") -> ("x.y:9000", False)
        _parse_endpoint("x.y") -> ("x.y", True)
    """
    raw = raw.strip()
    if "://" in raw:
        u = urlparse(raw)
        secure = (u.scheme.lower() == "https")
        host = u.netloc
        if not host:
            raise ValueError(f"Bad endpoint url: {raw}")
        return host, secure
    return raw, True  # default https


def _load_env_limited(env_filename: str) -> Optional[Path]:
    """
    Load .env from current directory or its parent (one level up).
    """
    cwd = Path.cwd()
    first = cwd / env_filename
    if first.is_file():
        load_dotenv(dotenv_path=first)
        logger.info(f"init_store: loaded {env_filename} from {first.resolve()}")
        return first

    logger.info(
        "init_store: file "
        f"{env_filename} not found in current directory {cwd.resolve()}, "
        "searching one level up"
    )
    second = cwd.parent / env_filename
    if second.is_file():
        load_dotenv(dotenv_path=second)
        logger.info(f"init_store: loaded {env_filename} from {second.resolve()}")
        return second
    return None


def load_cfg(*, load_dotenv_file: bool = True) -> S3Cfg:
    """
    Load configuration from .env / environment variables.

    Required env:
        RUNPOD_S3_ENDPOINT: e.g. "https://s3api-us-ca-2.runpod.io"
        RUNPOD_S3_BUCKET:   e.g. "n1ii97ynil"
        AWS_ACCESS_KEY_ID
        AWS_SECRET_ACCESS_KEY

    Optional env:
        RUNPOD_S3_REGION:   e.g. "us-ca-2" (recommended)

    Example .env:
        RUNPOD_S3_ENDPOINT=https://s3api-us-ca-2.runpod.io
        RUNPOD_S3_REGION=us-ca-2
        RUNPOD_S3_BUCKET=n1ii97ynil
        AWS_ACCESS_KEY_ID=...
        AWS_SECRET_ACCESS_KEY=...
    """
    if load_dotenv_file:
        load_dotenv()

    endpoint_raw = _env_first("RUNPOD_S3_ENDPOINT", "S3_ENDPOINT", "AWS_S3_ENDPOINT")
    if not endpoint_raw:
        raise ValueError("Missing endpoint. Set RUNPOD_S3_ENDPOINT=https://s3api-us-ca-2.runpod.io")
    endpoint, secure = _parse_endpoint(endpoint_raw)

    region = _env_first("RUNPOD_S3_REGION", "S3_REGION", "AWS_REGION", default=None)

    bucket = _env_first("RUNPOD_S3_BUCKET", "S3_BUCKET")
    if not bucket:
        raise ValueError("Missing bucket. Set RUNPOD_S3_BUCKET=n1ii97ynil")

    access_key = _env_first("AWS_ACCESS_KEY_ID")
    secret_key = _env_first("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise ValueError("Missing AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env")

    return S3Cfg(
        endpoint=endpoint,
        region=region,
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
        part_size_mb=DEFAULT_PART_SIZE_MB,
    )


def get_client(cfg: S3Cfg, http_client: Optional[urllib3.PoolManager] = None) -> Minio:
    """
    Create MinIO client instance.

    Example:
        cfg = load_cfg()
        client = get_client(cfg)
    """
    http_client = http_client or urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=10.0, read=float(DEFAULT_HTTP_TIMEOUT_SEC)),
        maxsize=10,
        cert_reqs="CERT_REQUIRED",
        ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where(),
        retries=urllib3.Retry(
            total=5,
            backoff_factor=0.2,
            status_forcelist=[500, 502, 503, 504],
        ),
    )
    return Minio(
        cfg.endpoint,
        access_key=cfg.access_key,
        secret_key=cfg.secret_key,
        secure=cfg.secure,
        region=cfg.region,
        http_client=http_client,
    )


# ---------------------------- Retry helpers ----------------------------

def _is_retryable_s3_error(e: S3Error) -> bool:
    """
    Detect transient S3 errors and HTTP responses worth retrying.

    HTTP status matching is intentional because S3-compatible gateways may return
    502/503 responses with a non-standard or missing XML error code.

    Example:
        try:
            ...
        except S3Error as e:
            if _is_retryable_s3_error(e):
                ...
    """
    if _s3_status_code(e) in RETRYABLE_HTTP_STATUS_CODES:
        return True

    code = (e.code or "").lower()
    return code in {
        "slowdown",
        "requesttimeout",
        "internalerror",
        "serviceunavailable",
        "throttling",
        "temporarilyunavailable",
    }


def _s3_status_code(exc: S3Error) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status", "status_code"):
            value = getattr(response, attr, None)
            if isinstance(value, int):
                return value
    return None


def _is_access_denied_error(exc: S3Error) -> bool:
    """Return True when an S3 error represents an access-denied response."""
    code = str(getattr(exc, "code", "") or "").lower()
    return _s3_status_code(exc) == 403 or code in {"accessdenied", "forbidden", "403"}


def _mask_secret(value: Optional[str]) -> str:
    if value is None:
        return "<empty>"
    text = str(value)
    if text == "":
        return "<empty>"
    if len(text) <= 10:
        return text
    return f"{text[:10]}..."


def _log_auth_context_if_needed(exc: BaseException) -> None:
    if not isinstance(exc, S3Error):
        return
    code = (exc.code or "").lower()
    status = _s3_status_code(exc)
    if status != 401 and code not in {
        "accessdenied",
        "invalidaccesskeyid",
        "signaturedoesnotmatch",
        "authorizationheaderinvalid",
        "invalidtoken",
    }:
        return
    context = {
        "AWS_ACCESS_KEY_ID": _mask_secret(os.getenv("AWS_ACCESS_KEY_ID")),
        "AWS_SECRET_ACCESS_KEY": _mask_secret(os.getenv("AWS_SECRET_ACCESS_KEY")),
        "RUNPOD_S3_BUCKET": os.getenv("RUNPOD_S3_BUCKET") or "<empty>",
        "RUNPOD_S3_ENDPOINT": os.getenv("RUNPOD_S3_ENDPOINT") or "<empty>",
        "RUNPOD_S3_REGION": os.getenv("RUNPOD_S3_REGION") or "<empty>",
    }
    logger.error(f"S3 auth error context: status={status} code={code} env={context}")


def _retry_predicate(exc: BaseException) -> bool:
    """
    Tenacity predicate for retries.

    Retries:
        - transient S3Error codes (slowdown, timeout, etc.)
        - network-ish local exceptions

    Example:
        # used internally by @resilient decorator
        pass
    """
    _log_auth_context_if_needed(exc)
    if isinstance(exc, S3Error):
        return _is_retryable_s3_error(exc)
    return isinstance(
        exc,
        (
            OSError,
            TimeoutError,
            ConnectionError,
            urllib3.exceptions.MaxRetryError,
            urllib3.exceptions.ReadTimeoutError,
            urllib3.exceptions.ConnectTimeoutError,
            urllib3.exceptions.ProtocolError,
        ),
    )


def resilient(fn):
    """
    Decorator: exponential backoff + jitter, one attempt plus up to 10 retries.

    Example:
        @resilient
        def my_op(...): ...
    """
    return retry(
        reraise=True,
        stop=stop_after_attempt(MAX_RETRY_ATTEMPTS),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        retry=retry_if_exception(_retry_predicate),
    )(fn)


def _mbps(num_bytes: int, seconds: float) -> float:
    """
    Convert bytes/time to MiB/s.

    Example:
        _mbps(10485760, 2.0) -> ~5.0
    """
    if seconds <= 0:
        return 0.0
    return (num_bytes / (1024 * 1024)) / seconds


def _ceil_div(num: int, den: int) -> int:
    """
    Integer ceil division.
    """
    if den <= 0:
        return 0
    return (num + den - 1) // den


def sha256_file(path: Path) -> str:
    """
    Compute SHA-256 of a local file.

    Example:
        h = sha256_file(Path("demo.txt"))
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _script_dir() -> Path:
    """
    Return directory of this script; fallback to current working directory.

    Example:
        d = _script_dir()
    """
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


def key_basename(key: str) -> str:
    """
    Return basename from S3 key.

    Example:
        key_basename("tmp/in/tmp_video.mp4") -> "tmp_video.mp4"
    """
    raw = key
    key = key.replace("\\", "/").strip("/")
    if not key:
        return raw
    return key.rsplit("/", 1)[-1]


def _normalize_prefix(prefix: str) -> str:
    """
    Normalize S3 key prefix to a consistent "directory" form.
    """
    norm = prefix.replace("\\", "/").strip().lstrip("/")
    if norm and not norm.endswith("/"):
        norm += "/"
    return norm


def _parse_http_datetime(value: Optional[str]) -> Optional[datetime]:
    """
    Parse HTTP date header value into datetime, tolerant to GMT/UTC.
    """
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def cleanup_tmp_parts(
    tmp_dir: Path,
    *,
    prefix: str = TMP_PART_PREFIX,
    suffix: str = ".part",
    older_than_hours: int = DEFAULT_TMP_CLEANUP_HOURS,
) -> Dict[str, Any]:
    """
    Remove stale resumable download part files.

    Args:
        tmp_dir: directory where part files are stored
        prefix: filename prefix to match (default: "tmp_s3_")
        suffix: filename suffix to match (default: ".part")
        older_than_hours: delete files older than this age

    Returns:
        dict with cleanup statistics

    Example:
        stats = cleanup_tmp_parts(Path("./tmp"), older_than_hours=24)
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    cutoff = now - older_than_hours * 3600

    removed = 0
    kept = 0
    errors: List[str] = []

    for p in tmp_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        try:
            mtime = p.stat().st_mtime
            if mtime < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
            else:
                kept += 1
        except Exception as e:
            errors.append(f"{p}: {e}")

    return {
        "tmp_dir": str(tmp_dir),
        "prefix": prefix,
        "suffix": suffix,
        "older_than_hours": older_than_hours,
        "removed": removed,
        "kept": kept,
        "errors": errors,
    }


# ---------------------------- Main class ----------------------------

class S3MiniApp:
    """
    Main app wrapper that stores configuration and the MinIO client.

    Example:
        cfg = load_cfg()
        app = S3MiniApp(cfg)
        app.upload(Path("a.bin"), "tmp/a.bin")
        app.download("tmp/a.bin", Path("out.bin"))
        app.delete("tmp/a.bin")
    """

    def __init__(self, cfg: S3Cfg, http_client: Optional[urllib3.PoolManager] = None) -> None:
        """
        Initialize app.

        Args:
            cfg: S3Cfg

        Example:
            app = S3MiniApp(load_cfg())
        """
        self.cfg = cfg
        self.client = get_client(cfg, http_client=http_client)

        self.base_dir = _script_dir()
        self.tmp_dir = self.base_dir / TMP_DIR_NAME
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def _choose_part_size_mb(self, size_bytes: int) -> int:
        """
        Pick multipart part size based on object size to avoid huge part counts.
        """
        heuristic_mb = (
            SMALL_FILE_PART_SIZE_MB
            if size_bytes <= SMALL_FILE_THRESHOLD_BYTES
            else LARGE_FILE_PART_SIZE_MB
        )
        base_mb = max(min(self.cfg.part_size_mb, heuristic_mb), MIN_PART_SIZE_MB)
        if size_bytes <= 0:
            return base_mb

        bytes_per_part = _ceil_div(size_bytes, TARGET_MAX_PARTS)
        required_mb = _ceil_div(bytes_per_part, 1024 * 1024)
        part_mb = max(base_mb, required_mb)
        part_mb = min(max(part_mb, MIN_PART_SIZE_MB), MAX_PART_SIZE_MB)
        return int(part_mb)

    @staticmethod
    def _is_timeout_exception(exc: BaseException) -> bool:
        """Return True for transport or S3 timeout failures."""
        if isinstance(
            exc,
            (
                TimeoutError,
                urllib3.exceptions.ReadTimeoutError,
                urllib3.exceptions.ConnectTimeoutError,
            ),
        ):
            return True
        if isinstance(exc, S3Error):
            code = str(getattr(exc, "code", "") or "").lower()
            if code in {"requesttimeout", "requesttimedout", "timeout"}:
                return True
            return _s3_status_code(exc) in {408, 504}
        return "timeout" in str(exc).lower() or "timed out" in str(exc).lower()

    @staticmethod
    def _next_part_size_bytes(part_size_bytes: int) -> Optional[int]:
        """Return the next smaller adaptive multipart size, if one exists."""
        minimum = ADAPTIVE_MIN_PART_SIZE_MB * 1024 * 1024
        current_mb = part_size_bytes // (1024 * 1024)
        next_mb = max(ADAPTIVE_MIN_PART_SIZE_MB, current_mb // 2)
        if next_mb >= current_mb:
            return None
        return max(minimum, next_mb * 1024 * 1024)

    def _iter_list_objects_safe(
        self,
        prefix: str,
        *,
        recursive: bool,
        max_keys: Optional[int],
        max_pages: int,
        use_api_v1: bool = False,
    ):
        """
        Safer list iterator with explicit page limits and empty-page guard.
        """
        normalized = _normalize_prefix(prefix)
        delimiter = None if recursive else "/"
        continuation_token: Optional[str] = None
        seen_tokens: set[str] = set()
        page = 0

        while True:
            page += 1
            if max_pages and page > max_pages:
                logger.warning(
                    f"LIST_SAFE: stop after max_pages={max_pages} "
                    f"prefix={normalized} api_v1={use_api_v1}"
                )
                break

            query: Dict[str, str] = {
                "delimiter": delimiter or "",
                "max-keys": str(max_keys or 1000),
                "prefix": normalized or "",
                "encoding-type": "url",
            }
            if use_api_v1:
                if continuation_token:
                    query["marker"] = continuation_token
            else:
                query["list-type"] = "2"
                if continuation_token:
                    query["continuation-token"] = continuation_token

            try:
                response = self.client._execute(
                    "GET",
                    self.cfg.bucket,
                    query_params=query,
                )
            except S3Error as exc:
                _log_auth_context_if_needed(exc)
                raise
            objects, is_truncated, next_token, _ = parse_list_objects(response)

            if not objects:
                logger.warning(
                    f"LIST_SAFE: empty page prefix={normalized} is_truncated={is_truncated} "
                    f"token={next_token} api_v1={use_api_v1}"
                )
                if not is_truncated:
                    break
                if not next_token or next_token == continuation_token:
                    logger.warning(
                        f"LIST_SAFE: stop due to no progress prefix={normalized} "
                        f"token={next_token} api_v1={use_api_v1}"
                    )
                    break

            for obj in objects:
                yield obj

            if not is_truncated:
                break

            if next_token:
                if next_token in seen_tokens:
                    logger.warning(
                        f"LIST_SAFE: repeated continuation token prefix={normalized} "
                        f"token={next_token} api_v1={use_api_v1}"
                    )
                    break
                seen_tokens.add(next_token)
            continuation_token = next_token

    def _iter_list_objects_manual_recursive(
        self,
        prefix: str,
        *,
        max_keys: Optional[int],
        max_pages: int,
        use_api_v1: bool = False,
    ):
        """
        Emulate recursive listing via delimiter walk (no recursive list calls).
        """
        normalized = _normalize_prefix(prefix)
        queue = [normalized]
        seen_prefixes = {normalized}

        while queue:
            current = queue.pop(0)
            for obj in self._iter_list_objects_safe(
                current,
                recursive=False,
                max_keys=max_keys,
                max_pages=max_pages,
                use_api_v1=use_api_v1,
            ):
                if obj.is_dir:
                    if obj.object_name and obj.object_name not in seen_prefixes:
                        seen_prefixes.add(obj.object_name)
                        queue.append(obj.object_name)
                else:
                    yield obj

    def _iter_list_objects(
        self,
        prefix: str,
        *,
        recursive: bool,
        max_keys: Optional[int],
        max_pages: int,
        use_api_v1: bool = False,
    ):
        """
        Unified iterator: recursive listings are emulated via delimiter walk.
        """
        if recursive:
            yield from self._iter_list_objects_manual_recursive(
                prefix,
                max_keys=max_keys,
                max_pages=max_pages,
                use_api_v1=use_api_v1,
            )
            return

        yield from self._iter_list_objects_safe(
            prefix,
            recursive=False,
            max_keys=max_keys,
            max_pages=max_pages,
            use_api_v1=use_api_v1,
        )

    def _upload_part_with_retry(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        data: bytes,
        content_type: str,
        budget: RetryBudget,
    ) -> str:
        """Upload one multipart part with retries bound to its upload ID and number."""
        attempt = 0
        while True:
            attempt += 1
            try:
                etag = self.client._upload_part(
                    self.cfg.bucket,
                    key,
                    data,
                    {"Content-Type": content_type},
                    upload_id,
                    part_number,
                )
                logger.debug(
                    f"UPLOAD_PART succeeded: s3://{self.cfg.bucket}/{key} "
                    f"upload_id={upload_id} part={part_number} attempt={attempt} "
                    f"bytes={len(data)} budget_remaining={budget.remaining}"
                )
                return etag
            except Exception as exc:
                if isinstance(exc, S3Error):
                    _log_auth_context_if_needed(exc)
                if not _retry_predicate(exc):
                    raise
                logger.warning(
                    f"UPLOAD_PART failed: s3://{self.cfg.bucket}/{key} "
                    f"upload_id={upload_id} part={part_number} attempt={attempt} "
                    f"budget_remaining={budget.remaining}: {exc}"
                )
                if self._is_timeout_exception(exc):
                    if not budget.consume_retry():
                        raise
                    raise
                if not budget.consume_retry():
                    raise
                delay = min(8.0, 0.5 * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0))
                time.sleep(delay)

    def _complete_multipart_with_retry(
        self,
        key: str,
        upload_id: str,
        parts: List[Part],
        budget: RetryBudget,
    ) -> Any:
        """Complete one multipart upload with a separately retried request."""
        attempt = 0
        while True:
            attempt += 1
            try:
                result = self.client._complete_multipart_upload(
                    self.cfg.bucket,
                    key,
                    upload_id,
                    parts,
                )
                logger.info(
                    f"COMPLETE_MULTIPART succeeded: s3://{self.cfg.bucket}/{key} "
                    f"upload_id={upload_id} parts={len(parts)} attempt={attempt} "
                    f"budget_remaining={budget.remaining}"
                )
                return result
            except Exception as exc:
                if isinstance(exc, S3Error):
                    _log_auth_context_if_needed(exc)
                if not _retry_predicate(exc) or not budget.consume_retry():
                    raise
                logger.warning(
                    f"COMPLETE_MULTIPART failed: s3://{self.cfg.bucket}/{key} "
                    f"upload_id={upload_id} attempt={attempt} "
                    f"budget_remaining={budget.remaining}: {exc}"
                )
                delay = min(8.0, 0.5 * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0))
                time.sleep(delay)

    def _upload_file_multipart(
        self,
        local_path: Path,
        key: str,
        size: int,
        part_size_bytes: int,
        part_count: int,
        content_type: str,
        budget: RetryBudget,
    ) -> Any:
        """Upload a file with explicit per-part and completion retries."""
        upload_id = self.client._create_multipart_upload(
            self.cfg.bucket,
            key,
            {"Content-Type": content_type},
        )
        logger.info(
            f"CREATE_MULTIPART succeeded: s3://{self.cfg.bucket}/{key} "
            f"upload_id={upload_id} parts={part_count} workers={DEFAULT_PARALLEL_UPLOADS}"
        )

        def upload_part(part_number: int) -> Part:
            """Read and upload one part in a bounded worker."""
            offset = (part_number - 1) * part_size_bytes
            expected_size = min(part_size_bytes, size - offset)
            with local_path.open("rb") as file_handle:
                file_handle.seek(offset)
                data = file_handle.read(expected_size)
            if len(data) != expected_size:
                raise IOError(
                    f"Could not read full part from {local_path.resolve()}: "
                    f"part={part_number}, expected={expected_size}, got={len(data)}"
                )
            etag = self._upload_part_with_retry(
                key,
                upload_id,
                part_number,
                data,
                content_type,
                budget,
            )
            return Part(part_number, etag)

        try:
            parts: List[Part] = []
            with ThreadPoolExecutor(max_workers=DEFAULT_PARALLEL_UPLOADS) as executor:
                futures = {
                    executor.submit(upload_part, part_number): part_number
                    for part_number in range(1, part_count + 1)
                }
                for future in as_completed(futures):
                    parts.append(future.result())
            parts.sort(key=lambda part: part.part_number)
            return self._complete_multipart_with_retry(key, upload_id, parts, budget)
        except Exception:
            try:
                self.client._abort_multipart_upload(self.cfg.bucket, key, upload_id)
                logger.warning(
                    f"ABORT_MULTIPART completed: s3://{self.cfg.bucket}/{key} "
                    f"upload_id={upload_id}"
                )
            except Exception as abort_error:
                logger.error(
                    f"ABORT_MULTIPART failed: s3://{self.cfg.bucket}/{key} "
                    f"upload_id={upload_id}: {abort_error}"
                )
            raise

    def _upload_file(self, local_path: Path, key: str) -> Dict[str, Any]:
        """
        Upload a single local file to S3 (multipart when applicable).
        """
        size = local_path.stat().st_size
        ctype, _ = mimetypes.guess_type(str(local_path))
        ctype = ctype or "application/octet-stream"

        part_size_mb = self._choose_part_size_mb(size)
        part_size_bytes = part_size_mb * 1024 * 1024
        part_count = _ceil_div(size, part_size_bytes)
        if part_count > MAX_MULTIPART_PARTS:
            raise ValueError(
                f"Multipart upload would require {part_count} parts, exceeding "
                f"the S3 limit of {MAX_MULTIPART_PARTS}: {local_path.resolve()}"
            )

        logger.info(
            f"UPLOAD: {local_path} -> s3://{self.cfg.bucket}/{key} "
            f"(size={size}, part_size_mb={part_size_mb}, parts~={part_count})"
        )
        t0 = time.perf_counter()
        try:
            if part_count <= 1:
                res = self.client.fput_object(
                    self.cfg.bucket,
                    key,
                    str(local_path),
                    content_type=ctype,
                    part_size=part_size_bytes,
                )
            else:
                budget = RetryBudget(MAX_RETRY_COUNT)
                while True:
                    try:
                        res = self._upload_file_multipart(
                            local_path,
                            key,
                            size,
                            part_size_bytes,
                            part_count,
                            ctype,
                            budget,
                        )
                        break
                    except Exception as exc:
                        smaller_part_size = self._next_part_size_bytes(part_size_bytes)
                        if not self._is_timeout_exception(exc) or smaller_part_size is None:
                            raise
                        if budget.remaining <= 0:
                            logger.error(
                                f"UPLOAD adaptive retry budget exhausted: s3://{self.cfg.bucket}/{key}"
                            )
                            raise
                        old_part_size_mb = part_size_bytes // (1024 * 1024)
                        part_size_bytes = smaller_part_size
                        part_size_mb = part_size_bytes // (1024 * 1024)
                        part_count = _ceil_div(size, part_size_bytes)
                        if part_count > MAX_MULTIPART_PARTS:
                            raise ValueError(
                                f"Adaptive multipart size would require {part_count} parts, "
                                f"exceeding the S3 limit of {MAX_MULTIPART_PARTS}: "
                                f"{local_path.resolve()}"
                            )
                        logger.warning(
                            f"UPLOAD timeout: reducing part size for s3://{self.cfg.bucket}/{key} "
                            f"from {old_part_size_mb} MiB to {part_size_mb} MiB; "
                            f"parts={part_count}, retries_remaining={budget.remaining}"
                        )
        except S3Error as exc:
            _log_auth_context_if_needed(exc)
            raise
        dt = time.perf_counter() - t0

        logger.debug(
            f"UPLOAD done: bytes={size} time={dt:.3f}s speed={_mbps(size, dt):.2f} MiB/s "
            f"etag={getattr(res, 'etag', None)}"
        )
        return {
            "bucket": self.cfg.bucket,
            "key": key,
            "etag": getattr(res, "etag", None),
            "bytes": size,
            "seconds": round(dt, 4),
            "mbps": round(_mbps(size, dt), 3),
            "part_size_mb": part_size_mb,
            "parts": part_count,
        }

    def _part_path_for(self, key: str, dest_path: Path) -> Path:
        """
        Return path for resumable *.part file stored in ./tmp рядом со скриптом.
        Имя начинается с tmp_s3_ и содержит короткий стабильный хеш (чтобы не было коллизий).

        Example:
            part = self._part_path_for("tmp/a.bin", Path("./out/a.bin"))
            # -> ./tmp/tmp_s3_a.bin_<hash>.part
        """
        # стабильный идентификатор докачки: зависит от bucket + key + dest
        # (чтобы разные назначения не конфликтовали)
        dest_abs = str(dest_path.resolve())
        ident_src = f"{self.cfg.bucket}|{key}|{dest_abs}".encode("utf-8", errors="ignore")
        short = hashlib.sha1(ident_src).hexdigest()[:12]  # noqa: S324 (не для крипты)
        base = dest_path.name.replace(" ", "_")
        return self.tmp_dir / f"{TMP_PART_PREFIX}{base}_{short}.part"

    def cleanup_parts(self, older_than_hours: int = DEFAULT_TMP_CLEANUP_HOURS) -> Dict[str, Any]:
        """
        Cleanup old part files in ./tmp.

        Example:
            app.cleanup_parts(older_than_hours=24)
        """
        return cleanup_tmp_parts(self.tmp_dir, older_than_hours=older_than_hours)

    def _stat_object_fallback(self, key: str) -> StatInfo:
        """
        Fallback stat using raw HEAD response headers.
        """
        response = None
        try:
            response = self.client._execute("HEAD", self.cfg.bucket, key)
        except S3Error as exc:
            _log_auth_context_if_needed(exc)
            raise
        try:
            headers = response.headers
            last_modified = _parse_http_datetime(headers.get("last-modified"))
            etag = headers.get("etag", "")
            etag = etag.replace('"', "") if etag else None
            size = int(headers.get("content-length", "0"))
            content_type = headers.get("content-type")
            version_id = headers.get("x-amz-version-id")
            return StatInfo(
                size=size,
                etag=etag,
                last_modified=last_modified,
                content_type=content_type,
                metadata=dict(headers),
                version_id=version_id,
            )
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def _stat_from_range_get(self, key: str, stat_error: BaseException) -> StatInfo:
        """Build object metadata from a one-byte ranged GET when HEAD is denied."""
        response = None
        logger.warning(
            f"STAT GET fallback: s3://{self.cfg.bucket}/{key}; "
            f"HEAD/stat error={stat_error}"
        )
        try:
            response = self.client.get_object(
                self.cfg.bucket,
                key,
                offset=0,
                length=RANGE_FALLBACK_LENGTH_BYTES,
            )
            response.read(RANGE_FALLBACK_LENGTH_BYTES)
            headers = response.headers
            content_range = headers.get("content-range", "")
            total_size = None
            if "/" in content_range:
                candidate = content_range.rsplit("/", maxsplit=1)[-1].strip()
                if candidate.isdigit():
                    total_size = int(candidate)
            if total_size is None:
                total_size = int(headers.get("content-length", "0"))
            etag = headers.get("etag", "")
            etag = etag.replace('"', "") if etag else None
            return StatInfo(
                size=total_size,
                etag=etag,
                last_modified=_parse_http_datetime(headers.get("last-modified")),
                content_type=headers.get("content-type"),
                metadata=dict(headers),
                version_id=headers.get("x-amz-version-id"),
            )
        except S3Error as exc:
            _log_auth_context_if_needed(exc)
            raise
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def _stat_object(self, key: str) -> StatInfo:
        """
        Stat object with UTC/GMT-tolerant parsing fallback.
        """
        try:
            st = self.client.stat_object(self.cfg.bucket, key)
            return StatInfo(
                size=int(st.size),
                etag=getattr(st, "etag", None),
                last_modified=getattr(st, "last_modified", None),
                content_type=getattr(st, "content_type", None),
                metadata=dict(getattr(st, "metadata", {}) or {}),
                version_id=getattr(st, "version_id", None),
            )
        except ValueError as exc:
            logger.debug(
                f"STAT parser failed for s3://{self.cfg.bucket}/{key}; "
                f"using raw HEAD fallback: {exc}"
            )
            try:
                return self._stat_object_fallback(key)
            except S3Error as fallback_error:
                if _is_access_denied_error(fallback_error):
                    return self._stat_from_range_get(key, fallback_error)
                raise
        except S3Error as exc:
            if _is_access_denied_error(exc):
                return self._stat_from_range_get(key, exc)
            _log_auth_context_if_needed(exc)
            raise

    def _get_object_exists_without_stat(self, key: str, stat_error: BaseException) -> bool:
        """
        Check object existence with a one-byte GET after HEAD/stat is denied.
        """
        response = None
        logger.warning(
            f"EXISTS stat failed for s3://{self.cfg.bucket}/{key}; "
            f"trying GET fallback: {stat_error}"
        )
        try:
            response = self.client.get_object(self.cfg.bucket, key, offset=0, length=1)
            response.read(1)
            return True
        except S3Error as exc:
            code = str(getattr(exc, "code", "") or "").lower()
            message = str(exc).lower()
            if code in {"nosuchkey", "nosuchobject", "notfound"}:
                return False
            if code == "invalidargument" and "object not found" in message:
                return False
            if code == "invalidrange":
                return True
            _log_auth_context_if_needed(exc)
            raise
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    @resilient
    def exists(self, key: str) -> bool:
        """
        Return True when an object exists and is readable.
        """
        try:
            self._stat_object(key)
            return True
        except S3Error as exc:
            code = str(getattr(exc, "code", "") or "").lower()
            status = getattr(getattr(exc, "response", None), "status", None)
            if code in {"nosuchkey", "nosuchobject", "notfound"} or status == 404:
                return False
            if code == "accessdenied" or status == 403:
                return self._get_object_exists_without_stat(key, exc)
            _log_auth_context_if_needed(exc)
            raise

    def upload(self, local_path: Path, key: str) -> Dict[str, Any]:
        """
        Upload a local file or directory to S3 (multipart when applicable).

        Notes:
            - default multipart size is 5 MiB for files up to 64 MiB and 8 MiB for larger files;
              timeout adaptation may lower it to 1 MiB for this S3-compatible endpoint.
            - multipart uploads retry each part independently and retry completion;
              timeout failures restart with a smaller part size within one shared budget of
              10 retries plus the initial request; resumable upload across process restarts
              is not guaranteed.
            - Existing object with the same key is overwritten by upload.

        Example:
            app.upload(Path("./video.mp4"), "tmp/in/video.mp4")
        """
        if local_path.is_dir():
            return self.upload_dir(local_path, key, recursive=True)
        if not local_path.is_file():
            raise FileNotFoundError(str(local_path))
        return self._upload_file(local_path, key)

    def upload_dir(
        self,
        local_dir: Path,
        key_prefix: str,
        *,
        recursive: bool = True,
    ) -> Dict[str, Any]:
        """
        Upload directory contents to S3 using key_prefix as a "folder".
        """
        if not local_dir.is_dir():
            raise NotADirectoryError(str(local_dir))

        prefix = _normalize_prefix(key_prefix)
        logger.info(
            f"UPLOAD_DIR: {local_dir} -> s3://{self.cfg.bucket}/{prefix} "
            f"(recursive={recursive})"
        )

        files = local_dir.rglob("*") if recursive else local_dir.glob("*")
        uploaded = 0
        skipped = 0
        uploaded_bytes = 0
        for path in files:
            if not path.is_file():
                continue
            rel = path.relative_to(local_dir).as_posix()
            remote_key = f"{prefix}{rel}" if prefix else rel
            info = self._upload_file(path, remote_key)
            if info.get("skipped"):
                skipped += 1
            else:
                uploaded += 1
                uploaded_bytes += int(info.get("bytes", 0))

        return {
            "bucket": self.cfg.bucket,
            "prefix": prefix,
            "local_dir": str(local_dir),
            "uploaded": uploaded,
            "skipped": skipped,
            "total": uploaded + skipped,
            "bytes": uploaded_bytes,
            "recursive": recursive,
        }

    def _resumable_download_impl(
        self,
        key: str,
        dest_path: Path,
        *,
        st: Optional[StatInfo] = None,
    ) -> Dict[str, Any]:
        """
        Download with resume support using a shared ./tmp folder near the script.

        Strategy:
            - partial file stored at ./tmp/tmp_s3_<destname>_<hash>.part
            - resume via current part size -> S3 range offset
            - after full download: move to final destination (no *.part next to dest)

        Example:
            app.download("tmp/in/video.mp4", Path("./out/video.mp4"))
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = self._part_path_for(key, dest_path)

        st = st or self._stat_object(key)
        total = int(st.size)

        offset = part_path.stat().st_size if part_path.exists() else 0
        if offset > total:
            part_path.unlink(missing_ok=True)
            offset = 0

        mode = "ab" if offset > 0 else "wb"
        logger.info(
            f"DOWNLOAD: s3://{self.cfg.bucket}/{key} -> {dest_path} "
            f"(resume_offset={offset}, part={part_path})"
        )

        t0 = time.perf_counter()
        try:
            resp = self.client.get_object(self.cfg.bucket, key, offset=offset)
        except S3Error as exc:
            _log_auth_context_if_needed(exc)
            raise
        try:
            with open(part_path, mode) as f:
                for chunk in resp.stream(1024 * 1024):
                    f.write(chunk)
        finally:
            resp.close()
            resp.release_conn()

        got = part_path.stat().st_size
        dt = time.perf_counter() - t0

        if got != total:
            # Trigger retry via @resilient wrapper
            raise ConnectionError(f"Incomplete download: got={got} total={total}")

        # финализация без *.part рядом с dest: переносим готовый файл
        # shutil.move корректно работает и при разных ФС (сделает copy+delete)
        shutil.move(str(part_path), str(dest_path))

        logger.debug(
            f"DOWNLOAD done: bytes={total} time={dt:.3f}s speed={_mbps(total, dt):.2f} MiB/s"
        )
        return {
            "bucket": self.cfg.bucket,
            "key": key,
            "dest": str(dest_path),
            "bytes": total,
            "seconds": round(dt, 4),
            "mbps": round(_mbps(total, dt), 3),
            "etag": st.etag,
            "last_modified": st.last_modified.isoformat() if st.last_modified else None,
            "content_type": st.content_type,
        }

    def _direct_download_without_stat(
        self,
        key: str,
        dest_path: Path,
        *,
        stat_error: Optional[BaseException] = None,
    ) -> Dict[str, Any]:
        """
        Download without a preliminary HEAD/stat request.

        Some S3-compatible providers may deny object metadata requests while allowing
        object GET. This path intentionally skips resume and size-match checks because
        the total size is not known before GET.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = self._part_path_for(key, dest_path)
        part_path.unlink(missing_ok=True)

        logger.warning(
            f"DOWNLOAD direct fallback: s3://{self.cfg.bucket}/{key} -> {dest_path} "
            f"(no stat/head, part={part_path}, stat_error={stat_error})"
        )

        t0 = time.perf_counter()
        try:
            resp = self.client.get_object(self.cfg.bucket, key)
        except S3Error as exc:
            _log_auth_context_if_needed(exc)
            raise

        try:
            expected_size: Optional[int] = None
            headers = getattr(resp, "headers", None)
            if headers is not None:
                raw_len = headers.get("content-length")
                if raw_len:
                    try:
                        expected_size = int(raw_len)
                    except ValueError:
                        expected_size = None

            with open(part_path, "wb") as f:
                for chunk in resp.stream(1024 * 1024):
                    f.write(chunk)
        finally:
            resp.close()
            resp.release_conn()

        got = part_path.stat().st_size
        dt = time.perf_counter() - t0
        if expected_size is not None and got != expected_size:
            raise ConnectionError(f"Incomplete direct download: got={got} total={expected_size}")

        shutil.move(str(part_path), str(dest_path))

        logger.debug(
            f"DOWNLOAD direct fallback done: bytes={got} time={dt:.3f}s "
            f"speed={_mbps(got, dt):.2f} MiB/s"
        )
        return {
            "bucket": self.cfg.bucket,
            "key": key,
            "dest": str(dest_path),
            "bytes": got,
            "seconds": round(dt, 4),
            "mbps": round(_mbps(got, dt), 3),
            "etag": None,
            "last_modified": None,
            "content_type": None,
            "stat_fallback": True,
            "reason": "stat_failed",
            "stat_error": str(stat_error) if stat_error else None,
        }

    @resilient
    def download(self, key: str, dest_path: Path) -> Dict[str, Any]:
        """
        Download object to local path with resume and retries.

        Notes:
            - If local file exists with the same size, download is skipped.

        Example:
            app.download("tmp/demo/abc.txt", Path("./abc.txt"))
        """
        if dest_path.exists() and dest_path.is_dir():
            dest_path = dest_path / key_basename(key)
        try:
            st = self._stat_object(key)
        except S3Error as exc:
            logger.warning(
                f"DOWNLOAD stat failed for s3://{self.cfg.bucket}/{key}; "
                f"trying direct GET fallback: {exc}"
            )
            return self._direct_download_without_stat(key, dest_path, stat_error=exc)
        if dest_path.exists() and dest_path.is_file():
            local_size = dest_path.stat().st_size
            if local_size == int(st.size):
                logger.info(
                    f"DOWNLOAD skip: s3://{self.cfg.bucket}/{key} -> {dest_path} "
                    f"(size match: {local_size} bytes)"
                )
                return {
                    "bucket": self.cfg.bucket,
                    "key": key,
                    "dest": str(dest_path),
                    "bytes": int(st.size),
                    "etag": st.etag,
                    "last_modified": st.last_modified.isoformat() if st.last_modified else None,
                    "content_type": st.content_type,
                    "skipped": True,
                    "reason": "size_match",
                }
        return self._resumable_download_impl(key, dest_path, st=st)

    def download_prefix(
        self,
        prefix: str,
        dest_dir: Path,
        *,
        recursive: bool = True,
    ) -> Dict[str, Any]:
        """
        Download all objects under prefix into a local directory.
        """
        normalized = _normalize_prefix(prefix)
        dest_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"DOWNLOAD_DIR: s3://{self.cfg.bucket}/{normalized} -> {dest_dir} "
            f"(recursive={recursive})"
        )

        downloaded = 0
        skipped = 0
        downloaded_bytes = 0
        t0 = time.perf_counter()
        first_key: Optional[str] = None
        if recursive:
            logger.info(
                f"DOWNLOAD_DIR: manual recursive listing api_v1={LIST_USE_API_V1}"
            )
        objects_iter = self._iter_list_objects(
            normalized,
            recursive=recursive,
            max_keys=LIST_MAX_KEYS,
            max_pages=LIST_MAX_PAGES,
            use_api_v1=LIST_USE_API_V1,
        )
        for obj in objects_iter:
            key = obj.object_name
            if first_key is None:
                first_key = key
                dt = time.perf_counter() - t0
                logger.info(
                    f"DOWNLOAD_DIR: first object after {dt:.2f}s key={key} size={obj.size}"
                )
            if normalized:
                rel = key[len(normalized):]
            else:
                rel = key
            if not rel or rel.endswith("/"):
                continue
            dest_path = dest_dir / Path(rel)
            info = self.download(key, dest_path)
            if info.get("skipped"):
                skipped += 1
            else:
                downloaded += 1
                downloaded_bytes += int(info.get("bytes", 0))
                logger.debug(
                    f"DOWNLOAD_DIR: downloaded key={key} -> {dest_path} bytes={info.get('bytes')}"
                )
        if first_key is None:
            dt = time.perf_counter() - t0
            logger.warning(
                f"DOWNLOAD_DIR: no objects found for prefix={normalized} after {dt:.2f}s"
            )

        return {
            "bucket": self.cfg.bucket,
            "prefix": normalized,
            "dest_dir": str(dest_dir),
            "downloaded": downloaded,
            "skipped": skipped,
            "total": downloaded + skipped,
            "bytes": downloaded_bytes,
            "recursive": recursive,
        }

    @resilient
    def check(self, key: str) -> Dict[str, Any]:
        """
        Stat object (fast).

        Example:
            info = app.check("tmp/demo/abc.txt")
        """
        st = self._stat_object(key)
        return {
            "bucket": self.cfg.bucket,
            "key": key,
            "size": st.size,
            "etag": st.etag,
            "last_modified": st.last_modified.isoformat() if st.last_modified else None,
            "content_type": st.content_type,
            "metadata": dict(st.metadata or {}),
            "version_id": st.version_id,
        }

    @resilient
    def delete(self, key: str) -> Dict[str, Any]:
        """
        Delete object.

        Example:
            app.delete("tmp/demo/abc.txt")
        """
        logger.info(f"DELETE: s3://{self.cfg.bucket}/{key}")
        self.client.remove_object(self.cfg.bucket, key)
        return {"bucket": self.cfg.bucket, "key": key, "deleted": True}

    @resilient
    def put_json(self, obj: Any, key: str) -> Dict[str, Any]:
        """
        Serialize object as JSON and upload to S3 by key.

        Example:
            app.put_json({"a": 1}, "tmp/data.json")
        """
        try:
            payload = json.dumps(obj).encode("utf-8")
        except Exception as exc:
            logger.error(f"put_json serialize error for key={key}: {exc}")
            raise

        logger.info(f"PUT_JSON: s3://{self.cfg.bucket}/{key}")
        t0 = time.perf_counter()
        bio = io.BytesIO(payload)
        try:
            res = self.client.put_object(
                self.cfg.bucket,
                key,
                bio,
                length=len(payload),
                content_type="application/json; charset=utf-8",
            )
        except S3Error as exc:
            _log_auth_context_if_needed(exc)
            raise
        dt = time.perf_counter() - t0

        return {
            "bucket": self.cfg.bucket,
            "key": key,
            "etag": getattr(res, "etag", None),
            "bytes": len(payload),
            "seconds": round(dt, 4),
            "mbps": round(_mbps(len(payload), dt), 3),
        }

    @resilient
    def get_json(self, key: str) -> Any:
        """
        Download JSON object from S3 by key and deserialize.

        Example:
            obj = app.get_json("tmp/data.json")
        """
        logger.info(f"GET_JSON: s3://{self.cfg.bucket}/{key}")
        try:
            resp = self.client.get_object(self.cfg.bucket, key)
        except S3Error as exc:
            _log_auth_context_if_needed(exc)
            raise
        try:
            raw = resp.read()
        finally:
            resp.close()
            resp.release_conn()

        try:
            text = raw.decode("utf-8")
        except Exception as exc:
            logger.error(f"get_json decode error for key={key}: {exc}")
            raise

        try:
            return json.loads(text)
        except Exception as exc:
            logger.error(f"get_json parse error for key={key}: {exc}")
            raise

    @resilient
    def delete_old(self, prefix: str, older_than_hours: int) -> Dict[str, Any]:
        """
        Delete objects older than N hours under prefix.

        Example:
            app.delete_old(prefix="tmp/", older_than_hours=24)
        """
        if older_than_hours < 0:
            raise ValueError("older_than_hours must be >= 0")

        logger.info(
            f"DELETE_OLD: s3://{self.cfg.bucket}/{prefix} older_than_hours={older_than_hours}"
        )
        delta = timedelta(hours=older_than_hours)
        checked = 0
        deleted = 0
        skipped = 0

        for obj in self._iter_list_objects(
            prefix,
            recursive=True,
            max_keys=LIST_MAX_KEYS,
            max_pages=LIST_MAX_PAGES,
            use_api_v1=LIST_USE_API_V1,
        ):
            checked += 1
            last_modified = getattr(obj, "last_modified", None)
            if not last_modified:
                skipped += 1
                continue

            if last_modified.tzinfo is None:
                cutoff = datetime.utcnow() - delta
            else:
                cutoff = datetime.now(tz=last_modified.tzinfo) - delta

            if last_modified <= cutoff:
                self.client.remove_object(self.cfg.bucket, obj.object_name)
                deleted += 1

        return {
            "bucket": self.cfg.bucket,
            "prefix": prefix,
            "older_than_hours": older_than_hours,
            "checked": checked,
            "deleted": deleted,
            "skipped": skipped,
        }

    def list(self, prefix: str = "", recursive: bool = True, limit: int = 200) -> List[Dict[str, Any]]:
        """
        List objects under prefix.

        Example:
            items = app.list(prefix="tmp/demo/", limit=50)
        """
        logger.info(f"LIST: s3://{self.cfg.bucket}/{prefix} recursive={recursive} limit={limit}")
        out: List[Dict[str, Any]] = []
        for obj in self._iter_list_objects(
            prefix,
            recursive=recursive,
            max_keys=LIST_MAX_KEYS,
            max_pages=LIST_MAX_PAGES,
            use_api_v1=LIST_USE_API_V1,
        ):
            out.append(
                {
                    "key": obj.object_name,
                    "size": obj.size,
                    "etag": getattr(obj, "etag", None),
                    "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
                }
            )
            if len(out) >= limit:
                break
        return out

    def status(self) -> Dict[str, Any]:
        """
        Fast status dictionary (no bandwidth tests).

        Also performs quick cleanup of stale part files in ./tmp.

        Example:
            s = app.status()
        """
        cleanup = self.cleanup_parts(older_than_hours=DEFAULT_TMP_CLEANUP_HOURS)

        t0 = time.perf_counter()
        bucket_ok = self.client.bucket_exists(self.cfg.bucket)
        sample = self.list(prefix="", recursive=True, limit=5) if bucket_ok else []
        dt = time.perf_counter() - t0

        return {
            "endpoint": self.cfg.endpoint,
            "secure": self.cfg.secure,
            "region": self.cfg.region,
            "bucket": self.cfg.bucket,
            "bucket_exists": bucket_ok,
            "sample_objects": sample,
            "part_size_mb": self.cfg.part_size_mb,
            "python": sys.version.split()[0],
            "elapsed_sec": round(dt, 4),
            "tmp_cleanup": cleanup,
        }

    def recursive_status(self) -> Dict[str, Any]:
        """
        Extended status dictionary with full bucket scan.

        Includes object count and total size. Also cleans old part files.

        Example:
            s = app.recursive_status()
        """
        cleanup = self.cleanup_parts(older_than_hours=DEFAULT_TMP_CLEANUP_HOURS)

        t0 = time.perf_counter()
        bucket_ok = self.client.bucket_exists(self.cfg.bucket)
        sample: List[Dict[str, Any]] = []
        object_count = 0
        objects_bytes = 0
        if bucket_ok:
            for obj in self._iter_list_objects(
                "",
                recursive=True,
                max_keys=LIST_MAX_KEYS,
                max_pages=LIST_MAX_PAGES,
                use_api_v1=LIST_USE_API_V1,
            ):
                if len(sample) < 5:
                    sample.append(
                        {
                            "key": obj.object_name,
                            "size": obj.size,
                            "etag": getattr(obj, "etag", None),
                            "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
                        }
                    )
                object_count += 1
                objects_bytes += int(getattr(obj, "size", 0) or 0)
        dt = time.perf_counter() - t0

        return {
            "endpoint": self.cfg.endpoint,
            "secure": self.cfg.secure,
            "region": self.cfg.region,
            "bucket": self.cfg.bucket,
            "bucket_exists": bucket_ok,
            "sample_objects": sample,
            "object_count": object_count,
            "objects_bytes": objects_bytes,
            "part_size_mb": self.cfg.part_size_mb,
            "python": sys.version.split()[0],
            "elapsed_sec": round(dt, 4),
            "tmp_cleanup": cleanup,
        }


store: Optional["S3MiniApp"] = None


def init_store(env_filename: str = ".env") -> None:
    """
    Initialize global store from .env in CWD or its parent.
    """
    global store
    if store is not None:
        logger.warning("init_store: already initialized, overwriting store")

    _load_env_limited(env_filename)
    cfg = load_cfg(load_dotenv_file=False)
    store = S3MiniApp(cfg)


# ---------------------------- Extra demo helper (kept as function) ----------------------------

def demo_roundtrip(app: S3MiniApp) -> None:
    """
    Demo: upload small text -> check -> download -> checksum -> delete.
    Before running, cleans stale part files.

    Example:
        python s3_minio_app.py --demo
    """
    cleanup = app.cleanup_parts(older_than_hours=DEFAULT_TMP_CLEANUP_HOURS)
    logger.debug({"tmp_cleanup": cleanup})

    tmp_dir = Path("./tmp_s3_demo")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    payload = f"hello-runpod-s3 {time.time()} {uuid.uuid4()}".encode("utf-8")
    src = tmp_dir / "demo.txt"
    src.write_bytes(payload)

    key = f"tmp/demo/{uuid.uuid4()}.txt"
    dst = tmp_dir / "downloaded.txt"

    up = app.upload(src, key)
    st = app.check(key)
    dn = app.download(key, dst)

    ok = (sha256_file(src) == sha256_file(dst))
    logger.info(f"CHECKSUM ok={ok}")
    if not ok:
        raise RuntimeError("Downloaded file checksum mismatch")

    app.delete(key)
    logger.info({"upload": up, "stat": st, "download": dn, "deleted_key": key})


# ---------------------------- CLI ----------------------------

def build_parser() -> argparse.ArgumentParser:
    """
    Build CLI parser with detailed descriptions and examples.
    """
    p = argparse.ArgumentParser(
        prog="s3_minio_app",
        description=(
            "RunPod S3 (S3-compatible) mini tool via MinIO SDK.\n\n"
            "Examples:\n"
            "  python s3_minio_app.py --status\n"
            "  python s3_minio_app.py --recursive-status\n"
            "  python s3_minio_app.py --list --prefix tmp/\n"
            "  python s3_minio_app.py --upload ./local.bin --key tmp/local.bin\n"
            "  python s3_minio_app.py --upload ./local_dir --key tmp/local_dir/\n"
            "  python s3_minio_app.py --download ./out.bin --key tmp/local.bin\n"
            "  python s3_minio_app.py --download ./out_dir --key tmp/local_dir/\n"
            "  python s3_minio_app.py --check --key tmp/local.bin\n"
            "  python s3_minio_app.py --delete --key tmp/local.bin\n"
            "  python s3_minio_app.py --demo\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    p.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Run a tiny roundtrip demo: upload small text, stat, download (resume into ./tmp), "
            "verify sha256, delete.\n"
            "Example:\n"
            "  --demo"
        ),
    )
    p.add_argument(
        "--status",
        action="store_true",
        help=(
            "Print fast status dictionary (no bandwidth tests). Also cleans old ./tmp/tmp_s3_*.part.\n"
            "Example:\n"
            "  --status"
        ),
    )
    p.add_argument(
        "--recursive-status",
        action="store_true",
        help=(
            "Print extended status with full bucket scan (object count and total size). "
            "Also cleans old ./tmp/tmp_s3_*.part.\n"
            "Example:\n"
            "  --recursive-status"
        ),
    )
    p.add_argument(
        "--list",
        action="store_true",
        help=(
            "List objects in bucket (limited to first 200 items). Use --prefix to limit listing.\n"
            "Example:\n"
            "  --list --prefix tmp/demo/"
        ),
    )
    p.add_argument(
        "--prefix",
        default="",
        help=(
            "Prefix for --list (acts like a folder). Empty means root.\n"
            "Examples:\n"
            "  --prefix tmp/\n"
            "  --prefix tmp/demo/"
        ),
    )
    p.add_argument(
        "--upload",
        default="",
        help=(
            "Local file or directory path to upload (directories are recursive).\n"
            "Example:\n"
            "  --upload ./video.mp4 --key tmp/in/video.mp4\n"
            "  --upload ./videos --key tmp/in/videos/"
        ),
    )
    p.add_argument(
        "--download",
        default="",
        help=(
            "Destination local path for download. Supports resume into ./tmp via tmp_s3_*.part.\n"
            "For directory download use --key with trailing slash and a local directory path.\n"
            "Example:\n"
            "  --download ./out/video.mp4 --key tmp/in/video.mp4\n"
            "  --download ./out_dir --key tmp/in/videos/"
        ),
    )
    p.add_argument(
        "--key",
        default="",
        help=(
            "S3 object key or prefix for upload/download/check/delete.\n"
            "Example keys:\n"
            "  tmp/demo/abc.txt\n"
            "  tmp/in/video.mp4\n"
            "  tmp/in/videos/"
        ),
    )
    p.add_argument(
        "--check",
        action="store_true",
        help=(
            "Stat object by --key (fast metadata check: size/etag/last_modified).\n"
            "Example:\n"
            "  --check --key tmp/demo/abc.txt"
        ),
    )
    p.add_argument(
        "--delete",
        action="store_true",
        help=(
            "Delete object by --key.\n"
            "Example:\n"
            "  --delete --key tmp/demo/abc.txt"
        ),
    )
    return p


def main() -> int:
    """
    CLI entry point.

    Logging:
        - DEFAULT_LOG_LEVEL controls verbosity (DEBUG prints upload/download speeds)
        - writes DEBUG logs to s3_minio_app.log

    Example:
        python s3_minio_app.py --demo
    """
    logger.remove()
    logger.add(sys.stdout, level=DEFAULT_LOG_LEVEL)
    logger.add("s3_minio_app.log", rotation="5 MB", retention=5, level="DEBUG")

    cfg = load_cfg()
    app = S3MiniApp(cfg)

    p = build_parser()
    args = p.parse_args()

    did_any = False

    if args.status:
        logger.info(app.status())
        did_any = True

    if args.recursive_status:
        logger.info(app.recursive_status())
        did_any = True

    if args.list:
        items = app.list(prefix=args.prefix, recursive=True, limit=200)
        logger.info({"count": len(items), "items": items})
        did_any = True

    if args.upload:
        if not args.key:
            logger.error("For --upload you must also set --key")
            return 2
        app.upload(Path(args.upload), args.key)
        did_any = True

    if args.check:
        if not args.key:
            logger.error("For --check you must set --key")
            return 2
        logger.info(app.check(args.key))
        did_any = True

    if args.download:
        if not args.key:
            logger.error("For --download you must set --key and --download DEST")
            return 2
        download_raw = args.download
        dest_path = Path(download_raw)
        download_dir_hint = download_raw.endswith(("/", "\\")) or (
            dest_path.exists() and dest_path.is_dir()
        )
        if args.key.endswith("/"):
            app.download_prefix(args.key, dest_path, recursive=True)
        elif download_dir_hint:
            try:
                app.check(args.key)
            except S3Error:
                app.download_prefix(args.key, dest_path, recursive=True)
            else:
                app.download(args.key, dest_path / key_basename(args.key))
        else:
            app.download(args.key, dest_path)
        did_any = True

    if args.delete:
        if not args.key:
            logger.error("For --delete you must set --key")
            return 2
        app.delete(args.key)
        did_any = True

    if args.demo:
        demo_roundtrip(app)
        did_any = True

    if not did_any:
        p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
