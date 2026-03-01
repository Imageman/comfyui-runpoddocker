#!/usr/bin/env bash
set -euo pipefail

MODELS_FILE="/models.txt"
WORKSPACE_DIR="/workspace"
INTERNAL_IO_PROBE_DIR="${INTERNAL_IO_PROBE_DIR:-/tmp}"
WORKSPACE_COMFYUI="/workspace/ComfyUI"
FLUX_TARGET="${WORKSPACE_COMFYUI}/models/diffusion_models/FLUX1"
FLUX_LINK="${WORKSPACE_COMFYUI}/models/checkpoints/flux"
WORKSPACE_IO_PROBE_MB="${WORKSPACE_IO_PROBE_MB:-64}"
WORKSPACE_IO_PROBE_BLOCK_KB="${WORKSPACE_IO_PROBE_BLOCK_KB:-1024}"

ensure_flux_symlink() {
    mkdir -p "$(dirname "${FLUX_LINK}")" "${FLUX_TARGET}"

    if [ -L "${FLUX_LINK}" ]; then
        current_target="$(readlink "${FLUX_LINK}")"
        if [ "${current_target}" = "${FLUX_TARGET}" ]; then
            return
        fi
        rm -f "${FLUX_LINK}"
    elif [ -e "${FLUX_LINK}" ]; then
        rm -rf "${FLUX_LINK}"
    fi

    ln -s "${FLUX_TARGET}" "${FLUX_LINK}"
}

run_io_probe() {
    local probe_name="$1"
    local probe_dir="$2"

    if ! command -v python3 >/dev/null 2>&1; then
        echo "${probe_name}: python3 is not installed, skipping"
        return 0
    fi

    if ! python3 - "${probe_name}" "${probe_dir}" "${WORKSPACE_IO_PROBE_MB}" "${WORKSPACE_IO_PROBE_BLOCK_KB}" <<'PY'
import ctypes
import errno
import os
import sys
import time

probe_name = sys.argv[1]
probe_dir = sys.argv[2]
total_mb = int(sys.argv[3])
block_kb = int(sys.argv[4])

if total_mb <= 0 or block_kb <= 0:
    print(f"{probe_name}: invalid probe size, skipping")
    raise SystemExit(0)

o_direct = getattr(os, "O_DIRECT", None)
if o_direct is None:
    print(f"{probe_name}: O_DIRECT is unavailable, skipping")
    raise SystemExit(0)

os.makedirs(probe_dir, exist_ok=True)

stat = os.statvfs(probe_dir)
alignment = max(4096, stat.f_bsize)
block_size = block_kb * 1024
total_bytes = total_mb * 1024 * 1024

if block_size % alignment != 0:
    block_size = ((block_size + alignment - 1) // alignment) * alignment
if total_bytes % block_size != 0:
    total_bytes = ((total_bytes + block_size - 1) // block_size) * block_size

probe_path = os.path.join(probe_dir, ".codex-workspace-io-probe.bin")
chunk_count = total_bytes // block_size
libc = ctypes.CDLL(None, use_errno=True)
buffer_ptr = ctypes.c_void_p()

def cleanup_file() -> None:
    try:
        os.unlink(probe_path)
    except FileNotFoundError:
        pass

def direct_error(prefix: str, err: OSError) -> None:
    if err.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF}:
        print(f"{probe_name}: {prefix} does not support O_DIRECT ({err.strerror}), skipping")
        raise SystemExit(0)
    raise err

ret = libc.posix_memalign(ctypes.byref(buffer_ptr), alignment, block_size)
if ret != 0:
    print(f"{probe_name}: posix_memalign failed with code {ret}, skipping")
    raise SystemExit(0)

try:
    ctypes.memset(buffer_ptr, 0x5A, block_size)

    try:
        write_fd = os.open(probe_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | o_direct, 0o600)
    except OSError as err:
        direct_error("write probe", err)

    try:
        started = time.perf_counter()
        for _ in range(chunk_count):
            written = libc.write(write_fd, buffer_ptr, block_size)
            if written != block_size:
                err_no = ctypes.get_errno()
                if written < 0:
                    direct_error("write probe", OSError(err_no, os.strerror(err_no)))
                raise OSError(err_no, f"short direct write: {written} of {block_size}")
        os.fsync(write_fd)
        write_seconds = time.perf_counter() - started
    finally:
        os.close(write_fd)

    try:
        read_fd = os.open(probe_path, os.O_RDONLY | o_direct)
    except OSError as err:
        direct_error("read probe", err)

    try:
        started = time.perf_counter()
        total_read = 0
        while total_read < total_bytes:
            read_now = libc.read(read_fd, buffer_ptr, block_size)
            if read_now == 0:
                break
            if read_now < 0:
                err_no = ctypes.get_errno()
                direct_error("read probe", OSError(err_no, os.strerror(err_no)))
            total_read += read_now
        read_seconds = time.perf_counter() - started
    finally:
        os.close(read_fd)

    write_mib_s = total_bytes / (1024 * 1024) / max(write_seconds, 1e-9)
    read_mib_s = total_read / (1024 * 1024) / max(read_seconds, 1e-9)
    print(
        f"{probe_name}: "
        f"path={probe_dir} size_mib={total_bytes // (1024 * 1024)} "
        f"block_kib={block_size // 1024} "
        f"write_mib_s={write_mib_s:.2f} "
        f"read_mib_s={read_mib_s:.2f}"
    )
finally:
    cleanup_file()
    libc.free(buffer_ptr)
PY
    then
        echo "${probe_name}: probe failed unexpectedly, continuing"
    fi
}

if ! command -v aria2c >/dev/null 2>&1; then
    echo "aria2c is not installed"
    exit 1
fi

ensure_flux_symlink
run_io_probe "WORKSPACE IO TEST" "${WORKSPACE_DIR}"
run_io_probe "INTERNAL IO TEST" "${INTERNAL_IO_PROBE_DIR}"

if [ ! -f "${MODELS_FILE}" ]; then
    echo "Models list not found: ${MODELS_FILE}"
    exit 0
fi

while IFS= read -r line || [ -n "${line}" ]; do
    line="${line#"${line%%[![:space:]]*}"}"

    if [ -z "${line}" ] || [[ "${line}" == \#* ]]; then
        continue
    fi

    read -r model_url destination_dir extra <<< "${line}"

    if [ -z "${model_url:-}" ] || [ -z "${destination_dir:-}" ]; then
        echo "Skipping malformed models.txt line: ${line}"
        continue
    fi

    if [ -n "${extra:-}" ]; then
        echo "Skipping malformed models.txt line with extra fields: ${line}"
        continue
    fi

    filename="$(basename "${model_url%%\?*}")"
    target_path="${destination_dir%/}/${filename}"

    mkdir -p "${destination_dir}"

    if [ -f "${target_path}" ]; then
        echo "Model already exists, skipping: ${target_path}"
        continue
    fi

    echo "Downloading model: ${model_url} -> ${target_path}"
    aria2c \
        --allow-overwrite=false \
        --auto-file-renaming=false \
        --continue=true \
        --dir="${destination_dir}" \
        --file-allocation=none \
        --max-connection-per-server=8 \
        --min-split-size=1M \
        --out="${filename}" \
        --split=8 \
        "${model_url}"
done < "${MODELS_FILE}"
