"""Per-invocation Prime Minion lifecycle owner.

The parent Hermes process owns the session lease and the result decision.  This
module owns exactly one short-lived worker process, its private mount/PID
namespace, the relay, the embedded Prime RPC process, and cleanup.  The module
intentionally uses only the Python standard library so the bootstrap path can
fail closed before any provider-backed process is started.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import ctypes.util
import errno
import fcntl
import json
import os
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1 << 20
MAX_RESULT_BYTES = 2 << 20
MAX_RPC_LINE_BYTES = 4 << 20
MAX_DIAGNOSTIC_BYTES = 64 << 10
MAX_READY_BYTES = 64 << 10
MAX_TASK_BYTES = MAX_REQUEST_BYTES // 2
EVIDENCE_PREFIX = "PRIME_MINION_EVIDENCE "
MAX_RPC_EVENTS = 256
MAX_RPC_RETAINED_BYTES = 8 << 20
TMPFS_SIZE = "64M"
CAPABILITY_PROFILE = "linux-user-mount-pid-v1"

WORKER_SUCCESS = 0
WORKER_FAILURE = 1
WORKER_PROTOCOL_FAILURE = 2
WORKER_UNSUPPORTED = 3

# Linux mount constants.  Keeping these local avoids importing a platform
# package in the worker's minimal startup path.
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REC = 1 << 14
MS_PRIVATE = 1 << 18
MNT_DETACH = 2
TMPFS_MAGIC = 0x01021994


class ProtocolError(ValueError):
    """A bounded lifecycle stream violated its wire contract."""


class UnsupportedLifecycleHost(RuntimeError):
    """The host does not expose the required namespace/mount primitives."""


class ControlState(str, Enum):
    ALIVE = "alive"
    INTENTIONAL_STOP = "intentional_stop"
    PARENT_LOST = "parent_lost"


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolError(f"JSON value is not encodable: {exc}") from exc


def encode_frame(value: Any, maximum: int) -> bytes:
    """Encode one four-byte big-endian length-prefixed JSON frame."""

    if maximum <= 0:
        raise ValueError("maximum must be positive")
    payload = _json_bytes(value)
    if len(payload) > maximum:
        raise ProtocolError(f"frame payload exceeds maximum of {maximum} bytes")
    return struct.pack(">I", len(payload)) + payload


def decode_frame(data: bytes, maximum: int) -> dict[str, Any]:
    """Decode exactly one bounded JSON object frame."""

    if not isinstance(data, bytes) or len(data) < 4:
        raise ProtocolError("frame is truncated")
    length = struct.unpack(">I", data[:4])[0]
    if length > maximum:
        raise ProtocolError(f"frame payload exceeds maximum of {maximum} bytes")
    if len(data) != length + 4:
        raise ProtocolError("frame has trailing or missing bytes")
    try:
        text = data[4:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("frame is not valid UTF-8") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("frame contains malformed JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("frame JSON value must be an object")
    return value


def _read_exact_fd(fd: int, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        try:
            chunk = os.read(fd, remaining)
        except InterruptedError:
            continue
        if not chunk:
            raise ProtocolError("frame is truncated before EOF")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame_fd(fd: int, maximum: int) -> dict[str, Any]:
    """Read one frame from a blocking descriptor and require EOF afterwards."""

    header = _read_exact_fd(fd, 4)
    length = struct.unpack(">I", header)[0]
    if length > maximum:
        # Drain the sender before returning so the peer cannot deadlock on a
        # bounded pipe, but retain no untrusted body.
        while True:
            try:
                chunk = os.read(fd, 65_536)
            except InterruptedError:
                continue
            if not chunk:
                break
        raise ProtocolError(f"frame payload exceeds maximum of {maximum} bytes")
    body = _read_exact_fd(fd, length)
    try:
        extra = os.read(fd, 1)
    except InterruptedError:
        extra = os.read(fd, 1)
    if extra:
        while True:
            try:
                chunk = os.read(fd, 65_536)
            except InterruptedError:
                continue
            if not chunk:
                break
        raise ProtocolError("multiple or trailing request frames")
    return decode_frame(header + body, maximum)


async def read_frame_stream(reader: asyncio.StreamReader, maximum: int) -> dict[str, Any]:
    """Read one frame from an asyncio stream and continue draining to EOF."""

    error: ProtocolError | None = None
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError as exc:
        error = ProtocolError("result frame is missing or truncated")
        body = exc.partial
        while await reader.read(65_536):
            pass
        raise error
    length = struct.unpack(">I", header)[0]
    if length > maximum:
        while await reader.read(65_536):
            pass
        raise ProtocolError(f"frame payload exceeds maximum of {maximum} bytes")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        while await reader.read(65_536):
            pass
        raise ProtocolError("result frame is truncated")
    trailing = bytearray()
    while True:
        chunk = await reader.read(65_536)
        if not chunk:
            break
        if len(trailing) < 256:
            trailing.extend(chunk[: 256 - len(trailing)])
    if trailing:
        raise ProtocolError("multiple or trailing result frames")
    return decode_frame(header + body, maximum)


class TailBuffer:
    """Retain only the last bounded bytes while continuing to drain."""

    def __init__(self, maximum: int = MAX_DIAGNOSTIC_BYTES) -> None:
        self.maximum = maximum
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def append(self, value: bytes) -> None:
        if not value:
            return
        if len(value) >= self.maximum:
            self._chunks.clear()
            self._chunks.append(value[-self.maximum :])
            self._size = min(len(value), self.maximum)
            return
        self._chunks.append(value)
        self._size += len(value)
        while self._size > self.maximum and self._chunks:
            self._size -= len(self._chunks.popleft())

    def bytes(self) -> bytes:
        return b"".join(self._chunks)[-self.maximum :]

    def text(self) -> str:
        return self.bytes().decode("utf-8", errors="replace")


async def drain_stream(reader: asyncio.StreamReader | None, tail: TailBuffer) -> None:
    if reader is None:
        return
    while True:
        chunk = await reader.read(65_536)
        if not chunk:
            return
        tail.append(chunk)


async def read_bounded_line(
    reader: asyncio.StreamReader,
    *,
    maximum: int,
    timeout: float,
) -> tuple[bytes, bytes]:
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError("bounded line read timed out")
        chunk = await asyncio.wait_for(reader.read(min(4096, maximum + 2)), timeout=remaining)
        if not chunk:
            if not buffer:
                return b"", b""
            raise ProtocolError("bounded line ended without a newline")
        buffer.extend(chunk)
        newline = buffer.find(b"\n")
        if newline >= 0:
            if newline > maximum:
                raise ProtocolError("bounded line exceeds its maximum")
            return bytes(buffer[: newline + 1]), bytes(buffer[newline + 1 :])
        if len(buffer) > maximum:
            raise ProtocolError("bounded line exceeds its maximum")


def classify_control_bytes(value: bytes) -> ControlState:
    if value == b"":
        return ControlState.PARENT_LOST
    if value == b"S":
        return ControlState.INTENTIONAL_STOP
    raise ProtocolError("control pipe requires exactly one S byte or EOF")


@dataclass(frozen=True)
class CleanupVerdict:
    clean: bool
    mount_absent: bool
    children_reaped: bool
    protocol_failure: bool = False
    parent_lost: bool = False
    cancellation: bool = False
    diagnostics_drained: bool = True
    error: str | None = None


def accept_success_after_cleanup(
    *,
    provisional_result: Mapping[str, Any] | None,
    cleanup: CleanupVerdict,
    parent_lost: bool,
) -> bool:
    """The deliberately strict success predicate used by tests and the worker."""

    return bool(
        not parent_lost
        and provisional_result is not None
        and provisional_result.get("status") == "completed"
        and cleanup.clean
        and cleanup.mount_absent
        and cleanup.children_reaped
        and not cleanup.protocol_failure
        and not cleanup.cancellation
        and cleanup.error is None
    )


ANCHOR_RECEIPT_VERSION = 1
MAX_ANCHOR_RECEIPT_BYTES = 4096


def _stat_mode(mode: int) -> int:
    return stat.S_IMODE(mode)


def _normalized_anchor_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _directory_identity(info: os.stat_result, label: str) -> dict[str, int]:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsupportedLifecycleHost(f"{label} is not a directory")
    if info.st_uid != os.getuid():
        raise UnsupportedLifecycleHost(f"{label} has the wrong owner")
    if _stat_mode(info.st_mode) != 0o700:
        raise UnsupportedLifecycleHost(f"{label} is not mode 0700")
    return {
        "dev": int(info.st_dev),
        "ino": int(info.st_ino),
        "uid": int(info.st_uid),
        "mode": _stat_mode(info.st_mode),
    }


def anchor_receipt_path(path: Path) -> Path:
    path = _normalized_anchor_path(path)
    return path.parent / f".{path.name}.identity.json"


def _read_anchor_receipt(parent_fd: int, path: Path) -> dict[str, Any]:
    receipt_name = anchor_receipt_path(path).name
    flags = os.O_RDONLY | os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        raise UnsupportedLifecycleHost("O_NOFOLLOW is unavailable")
    try:
        fd = os.open(receipt_name, flags | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise UnsupportedLifecycleHost("fixed invocation anchor identity receipt is missing") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or _stat_mode(info.st_mode) != 0o600:
            raise UnsupportedLifecycleHost("fixed invocation anchor identity receipt is invalid")
        if info.st_size <= 0 or info.st_size > MAX_ANCHOR_RECEIPT_BYTES:
            raise UnsupportedLifecycleHost("fixed invocation anchor identity receipt size is invalid")
        data = bytearray()
        while len(data) <= MAX_ANCHOR_RECEIPT_BYTES:
            chunk = os.read(fd, min(1024, MAX_ANCHOR_RECEIPT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != info.st_size:
            raise UnsupportedLifecycleHost("fixed invocation anchor identity receipt changed while reading")
    finally:
        os.close(fd)
    try:
        receipt = json.loads(bytes(data))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsupportedLifecycleHost("fixed invocation anchor identity receipt is malformed") from exc
    required = {"schema_version", "anchor_path", "parent_identity", "anchor_identity"}
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise UnsupportedLifecycleHost("fixed invocation anchor identity receipt shape is invalid")
    if receipt["schema_version"] != ANCHOR_RECEIPT_VERSION or receipt["anchor_path"] != str(path):
        raise UnsupportedLifecycleHost("fixed invocation anchor identity receipt binding is invalid")
    for identity_field in ("parent_identity", "anchor_identity"):
        identity = receipt[identity_field]
        if not isinstance(identity, dict) or set(identity) != {"dev", "ino", "uid", "mode"}:
            raise UnsupportedLifecycleHost("fixed invocation anchor identity receipt shape is invalid")
        if any(type(identity[key]) is not int for key in identity):
            raise UnsupportedLifecycleHost("fixed invocation anchor identity receipt values are invalid")
    return receipt


def open_provisioned_anchor(path: Path) -> tuple[int, dict[str, int]]:
    """Open an installation-provisioned anchor without mutating its route."""

    path = _normalized_anchor_path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        raise UnsupportedLifecycleHost("O_NOFOLLOW is unavailable")
    try:
        parent_fd = os.open(path.parent, flags | os.O_NOFOLLOW)
    except OSError as exc:
        raise UnsupportedLifecycleHost("fixed invocation anchor parent is unavailable") from exc
    anchor_fd = -1
    try:
        parent_identity = _directory_identity(os.fstat(parent_fd), "fixed invocation anchor parent")
        current_parent = _directory_identity(os.lstat(path.parent), "fixed invocation anchor parent pathname")
        if current_parent != parent_identity:
            raise UnsupportedLifecycleHost("fixed invocation anchor parent identity changed")
        receipt = _read_anchor_receipt(parent_fd, path)
        if receipt["parent_identity"] != parent_identity:
            raise UnsupportedLifecycleHost("fixed invocation anchor parent identity is unrecognized")
        try:
            anchor_fd = os.open(path.name, flags | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            raise UnsupportedLifecycleHost("fixed invocation anchor is unavailable") from exc
        identity = anchor_identity(anchor_fd)
        if receipt["anchor_identity"] != identity:
            raise UnsupportedLifecycleHost("fixed invocation anchor identity is unrecognized")
        verify_anchor_path(path, anchor_fd, identity)
        return anchor_fd, identity
    except Exception:
        if anchor_fd >= 0:
            os.close(anchor_fd)
        raise
    finally:
        os.close(parent_fd)


def verify_fixed_anchor(path: Path) -> dict[str, int]:
    fd, identity = open_provisioned_anchor(path)
    os.close(fd)
    return identity


def provision_fixed_anchor(path: Path) -> dict[str, int]:
    """Explicit install/test setup; normal invocation admission never calls this."""

    path = _normalized_anchor_path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        raise UnsupportedLifecycleHost("O_NOFOLLOW is unavailable")
    try:
        parent_fd = os.open(path.parent, flags | os.O_NOFOLLOW)
    except OSError as exc:
        raise UnsupportedLifecycleHost("fixed invocation anchor parent must be pre-created") from exc
    try:
        parent_identity = _directory_identity(os.fstat(parent_fd), "fixed invocation anchor parent")
        if _directory_identity(os.lstat(path.parent), "fixed invocation anchor parent pathname") != parent_identity:
            raise UnsupportedLifecycleHost("fixed invocation anchor parent identity changed")
        receipt_name = anchor_receipt_path(path).name
        anchor_exists = False
        receipt_exists = False
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            anchor_exists = True
        except FileNotFoundError:
            pass
        try:
            os.stat(receipt_name, dir_fd=parent_fd, follow_symlinks=False)
            receipt_exists = True
        except FileNotFoundError:
            pass
        if anchor_exists or receipt_exists:
            if not (anchor_exists and receipt_exists):
                raise UnsupportedLifecycleHost("fixed invocation anchor provisioning is incomplete")
            return verify_fixed_anchor(path)
        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        anchor_fd = os.open(path.name, flags | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            identity = anchor_identity(anchor_fd)
        finally:
            os.close(anchor_fd)
        receipt = {
            "schema_version": ANCHOR_RECEIPT_VERSION,
            "anchor_path": str(path),
            "parent_identity": parent_identity,
            "anchor_identity": identity,
        }
        payload = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        receipt_fd = os.open(
            receipt_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(receipt_fd, view)
                if written <= 0:
                    raise OSError("anchor receipt write made no progress")
                view = view[written:]
            os.fsync(receipt_fd)
        finally:
            os.close(receipt_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return verify_fixed_anchor(path)


def is_persistent_anchor(path: Path) -> bool:
    try:
        verify_fixed_anchor(path)
    except (OSError, UnsupportedLifecycleHost):
        return False
    return True


def assert_no_recursive_anchor_cleanup() -> None:
    """Marker helper: invocation cleanup has no path-recursive anchor operation."""

    return None


def open_anchor(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        raise UnsupportedLifecycleHost("O_NOFOLLOW is unavailable")
    fd = os.open(path, flags | os.O_NOFOLLOW)
    try:
        anchor_identity(fd)
    except Exception:
        os.close(fd)
        raise
    return fd


def anchor_identity(fd: int) -> dict[str, int]:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsupportedLifecycleHost("mount anchor FD is not a directory")
    if info.st_uid != os.getuid():
        raise UnsupportedLifecycleHost("mount anchor FD has the wrong owner")
    if _stat_mode(info.st_mode) != 0o700:
        raise UnsupportedLifecycleHost("mount anchor FD is not mode 0700")
    return {
        "dev": int(info.st_dev),
        "ino": int(info.st_ino),
        "uid": int(info.st_uid),
        "mode": _stat_mode(info.st_mode),
    }


def verify_anchor_path(path: Path, fd: int, expected: Mapping[str, int]) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsupportedLifecycleHost("fixed invocation anchor pathname is not a directory")
    current = {
        "dev": int(info.st_dev),
        "ino": int(info.st_ino),
        "uid": int(info.st_uid),
        "mode": _stat_mode(info.st_mode),
    }
    if current != dict(expected):
        raise UnsupportedLifecycleHost("fixed invocation anchor identity changed")
    if anchor_identity(fd) != current:
        raise UnsupportedLifecycleHost("mount anchor FD/path identity mismatch")


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def _mount_call(source: bytes | None, target: bytes, filesystem: bytes | None, flags: int, data: bytes | None) -> None:
    libc = _libc()
    mount = libc.mount
    mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
    mount.restype = ctypes.c_int
    if mount(source, target, filesystem, flags, data) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target.decode(errors="replace"))


def _umount_call(target: bytes, flags: int) -> None:
    libc = _libc()
    umount2 = libc.umount2
    umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
    umount2.restype = ctypes.c_int
    if umount2(target, flags) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target.decode(errors="replace"))


def mount_tmpfs(anchor_fd: int) -> int:
    """Make propagation private and mount bounded tmpfs through pre-mount A."""

    _mount_call(None, b"/", None, MS_REC | MS_PRIVATE, None)
    helper = shutil.which("mount")
    if helper is None:
        raise UnsupportedLifecycleHost("mount helper is unavailable")
    target = f"/proc/self/fd/{anchor_fd}"
    completed = subprocess.run(
        [
            helper,
            "-t",
            "tmpfs",
            "-o",
            f"size={TMPFS_SIZE},mode=0700,nosuid,nodev,noexec",
            "tmpfs",
            target,
        ],
        pass_fds=(anchor_fd,),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise UnsupportedLifecycleHost(f"descriptor-bound tmpfs mount failed: {detail[-1000:]}")
    mounted_path = os.readlink(f"/proc/self/fd/{anchor_fd}")
    if mounted_path.endswith(" (deleted)"):
        raise UnsupportedLifecycleHost("mount anchor lost its pathname before mount receipt")
    record = mountinfo_for_path(Path(mounted_path))
    if record is None or _statfs_type(mounted_path) != TMPFS_MAGIC:
        raise UnsupportedLifecycleHost("worker-created tmpfs mount receipt is unavailable")
    return record[0]


def _decode_mountinfo_path(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def mountinfo_for_path(path: Path) -> tuple[int, str] | None:
    target = str(Path(path).resolve())
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return None
    best: tuple[int, str] | None = None
    for line in text.splitlines():
        fields = line.split(" - ", 1)
        if len(fields) != 2:
            continue
        left = fields[0].split()
        if len(left) < 6:
            continue
        mountpoint = _decode_mountinfo_path(left[4])
        if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
            try:
                mount_id = int(left[0])
            except ValueError:
                continue
            if best is None or len(mountpoint) > len(best[1]):
                best = (mount_id, mountpoint)
    return best


def _statfs_type(path: str) -> int:
    class StatFs(ctypes.Structure):
        _fields_ = [("f_type", ctypes.c_long), ("rest", ctypes.c_byte * 256)]

    libc = _libc()
    statfs = libc.statfs
    statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFs)]
    statfs.restype = ctypes.c_int
    result = StatFs()
    if statfs(path.encode(), ctypes.byref(result)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), path)
    return int(result.f_type) & 0xFFFFFFFFFFFFFFFF


def _fd_mount_id(fd: int) -> int:
    try:
        lines = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise UnsupportedLifecycleHost("runtime FD mount identity is unavailable") from exc
    for line in lines:
        if line.startswith("mnt_id:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError as exc:
                raise UnsupportedLifecycleHost("runtime FD mount identity is malformed") from exc
    raise UnsupportedLifecycleHost("runtime FD mount identity is missing")


def open_verified_runtime_fd(anchor_path: Path, expected_mount_id: int) -> tuple[int, int]:
    """Open post-mount R and prove it is the newly-mounted tmpfs."""

    runtime_fd = os.open(anchor_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if _statfs_type(f"/proc/self/fd/{runtime_fd}") != TMPFS_MAGIC:
            raise UnsupportedLifecycleHost("post-mount runtime FD is not tmpfs")
        observed_mount_id = _fd_mount_id(runtime_fd)
        if observed_mount_id != expected_mount_id:
            raise UnsupportedLifecycleHost("post-mount runtime FD does not reference the worker-created mount")
        return runtime_fd, observed_mount_id
    except Exception:
        os.close(runtime_fd)
        raise


def _mount_id_present(mount_id: int) -> bool:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    return any(line.split(maxsplit=1)[0] == str(mount_id) for line in lines if line)


def detach_tmpfs(runtime_fd: int, mount_id: int) -> bool:
    """Detach the mounted tmpfs through post-mount R and verify its mount ID."""

    helper = shutil.which("umount")
    if helper is None:
        raise UnsupportedLifecycleHost("umount helper is unavailable")
    completed = subprocess.run(
        [helper, "-l", f"/proc/self/fd/{runtime_fd}"],
        pass_fds=(runtime_fd,),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise UnsupportedLifecycleHost(f"descriptor-bound tmpfs detach failed: {detail[-1000:]}")
    return not _mount_id_present(mount_id)


def _proc_start_time(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2 :].split()
    # The suffix starts at field 3; field 22 is suffix index 19.
    return fields[19] if len(fields) > 19 else None


def process_identity(pid: int) -> dict[str, str | int] | None:
    start = _proc_start_time(pid)
    if start is None:
        return None
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return {"pid": pid, "proc_start_time": start, "boot_id": boot}


def remap_runtime_environment(
    env: Mapping[str, str], *, worker_runtime_fd: int, child_runtime_fd: int
) -> dict[str, str]:
    """Rebind private-runtime paths from worker R to the child's R duplicate."""

    worker_root = f"/proc/self/fd/{worker_runtime_fd}"
    child_root = f"/proc/self/fd/{child_runtime_fd}"
    remapped: dict[str, str] = {}
    for key, value in env.items():
        if value == worker_root or value.startswith(worker_root + "/"):
            remapped[key] = child_root + value[len(worker_root) :]
        else:
            remapped[key] = value
    return remapped


def _namespace_pids() -> list[int]:
    result: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return result
    for name in names:
        if name.isdigit():
            result.append(int(name))
    return result


def check_capability_profile(anchor: Path) -> dict[str, Any]:
    """Run ordered disposable no-provider probes for the lifecycle profile."""

    def unsupported(name: str, cause: Exception | None = None) -> UnsupportedLifecycleHost:
        error = UnsupportedLifecycleHost(
            f"unsupported lifecycle host: {CAPABILITY_PROFILE} prerequisite {name} failed"
        )
        if cause is not None:
            error.__cause__ = cause
        return error

    if not sys.platform.startswith("linux"):
        raise unsupported("linux")
    unshare = shutil.which("unshare")
    mount_helper = shutil.which("mount")
    umount_helper = shutil.which("umount")
    if unshare is None:
        raise unsupported("unshare")
    if mount_helper is None or umount_helper is None:
        raise unsupported("mount_helpers")
    if not hasattr(os, "O_NOFOLLOW"):
        raise unsupported("nofollow")
    anchor = _normalized_anchor_path(anchor)
    fd, identity = open_provisioned_anchor(anchor)
    verify_anchor_path(anchor, fd, identity)
    sample = Path(tempfile.gettempdir()) / f".prime-minion-capability-{os.getpid()}-{time.time_ns()}"
    sample_fd = -1
    try:
        sample_fd = os.open(sample, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        os.write(sample_fd, b"same-user-access")
        os.close(sample_fd)
        sample_fd = -1
        probe = Path(__file__).resolve().parent / "scripts" / "probe_lifecycle_capability.py"
        if not probe.is_file():
            raise unsupported("probe_script")
        completed = subprocess.run(
            [
                unshare,
                "--user",
                "--map-root-user",
                "--mount",
                "--pid",
                "--fork",
                "--kill-child=SIGKILL",
                "--mount-proc",
                sys.executable,
                str(probe),
                str(fd),
                str(anchor),
                str(sample),
                mount_helper,
                umount_helper,
                unshare,
            ],
            pass_fds=(fd,),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except UnsupportedLifecycleHost:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise unsupported("unshare", exc)
    finally:
        if sample_fd >= 0:
            os.close(sample_fd)
        sample.unlink(missing_ok=True)
        os.close(fd)
    if completed.returncode != 0:
        category = "unshare"
        for line in completed.stderr.splitlines():
            if line.startswith("FAIL:"):
                candidate = line.split(":", 2)[1]
                if candidate.replace("_", "").isalnum():
                    category = candidate
                break
        raise unsupported(category)
    try:
        receipt = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise unsupported("probe_receipt", exc)
    if not isinstance(receipt, dict) or receipt.get("status") != "pass":
        raise unsupported("probe_receipt")
    if not is_persistent_anchor(anchor):
        raise unsupported("anchor_persistence")
    return {
        "profile": CAPABILITY_PROFILE,
        "unshare": unshare,
        "anchor": str(anchor),
        "anchor_identity": identity,
        "status": "pass",
    }


def _validate_exact_keys(value: Mapping[str, Any], allowed: set[str], required: set[str], name: str) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ProtocolError(f"{name} contains unknown keys: {sorted(unknown)}")
    if missing:
        raise ProtocolError(f"{name} is missing keys: {sorted(missing)}")


@dataclass
class _Child:
    name: str
    process: asyncio.subprocess.Process
    stdout_tail: TailBuffer = field(default_factory=TailBuffer)
    stderr_tail: TailBuffer = field(default_factory=TailBuffer)
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    returncode: int | None = None


class _RPCStreamFailure(RuntimeError):
    pass


class _PrimeRPC:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.events: asyncio.Queue[dict[str, Any] | None | Exception] = asyncio.Queue(maxsize=MAX_RPC_EVENTS + 1)
        self.reader_task = asyncio.create_task(self._read_events())
        self.failure: Exception | None = None
        self.event_count = 0
        self.retained_bytes = 0

    def _fail(self, message: str) -> None:
        if self.failure is not None:
            return
        self.failure = _RPCStreamFailure(message)
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.events.put_nowait(self.failure)

    async def _read_events(self) -> None:
        stdout = self.process.stdout
        if stdout is None:
            self._fail("Prime RPC stdout is unavailable")
            return
        buffer = bytearray()
        discarding = False
        try:
            while True:
                chunk = await stdout.read(65_536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        if len(buffer) > MAX_RPC_LINE_BYTES:
                            self._fail("Prime RPC line exceeds 4 MiB")
                            buffer.clear()
                            discarding = True
                        break
                    line = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    if discarding:
                        discarding = False
                        continue
                    if len(line) > MAX_RPC_LINE_BYTES:
                        self._fail("Prime RPC line exceeds 4 MiB")
                        continue
                    if not line or self.failure is not None:
                        continue
                    try:
                        value = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._fail("Prime RPC emitted malformed JSON")
                        continue
                    if not isinstance(value, dict):
                        self._fail("Prime RPC emitted a non-object record")
                        continue
                    self.event_count += 1
                    self.retained_bytes += len(line)
                    if self.event_count > MAX_RPC_EVENTS:
                        self._fail("Prime RPC event count exceeds 256 records")
                        continue
                    if self.retained_bytes > MAX_RPC_RETAINED_BYTES:
                        self._fail("Prime RPC retained output exceeds 8 MiB")
                        continue
                    self.events.put_nowait(value)
            if buffer:
                self._fail("Prime RPC ended with an unterminated line")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(str(exc))
        finally:
            if self.failure is None:
                self.events.put_nowait(None)

    async def send(self, value: dict[str, Any]) -> None:
        stdin = self.process.stdin
        if stdin is None:
            raise _RPCStreamFailure("Prime RPC stdin is unavailable")
        payload = _json_bytes(value) + b"\n"
        if len(payload) - 1 > MAX_RPC_LINE_BYTES:
            raise _RPCStreamFailure("Prime RPC request line exceeds 4 MiB")
        stdin.write(payload)
        await stdin.drain()

    async def next_event(self, timeout: float = 60.0) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        try:
            event = await asyncio.wait_for(self.events.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise _RPCStreamFailure("Prime RPC response timed out") from exc
        if event is None:
            raise self.failure or _RPCStreamFailure("Prime RPC closed before a response")
        if isinstance(event, Exception):
            raise event
        return event

    async def command(
        self,
        *,
        request_id: str,
        command: dict[str, Any],
        expected_command: str,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        await self.send({"id": request_id, **command})
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.01, deadline - time.monotonic())
            event = await self.next_event(remaining)
            if event.get("type") != "response" or event.get("id") != request_id:
                continue
            if event.get("command") != expected_command:
                raise _RPCStreamFailure("Prime RPC returned a mismatched command response")
            if event.get("success") is not True:
                raise _RPCStreamFailure(str(event.get("error") or f"Prime Agent rejected {expected_command}"))
            return event

    async def abort(self) -> None:
        try:
            await self.send({"id": "hermes-minion-abort", "type": "abort"})
        except Exception:
            return


class InvocationWorker:
    """PID1 lifecycle owner.  All state here is invocation-local."""

    def __init__(
        self,
        *,
        control_fd: int,
        anchor_fd: int,
        require_pid1: bool = True,
        relay_command: Sequence[str] | None = None,
        prime_command: Sequence[str] | None = None,
    ) -> None:
        self.control_fd = control_fd
        self.anchor_fd = anchor_fd
        self.require_pid1 = require_pid1
        self.relay_command = list(relay_command) if relay_command is not None else None
        self.prime_command = list(prime_command) if prime_command is not None else None
        self.anchor_path: Path | None = None
        self.anchor_identity: dict[str, int] | None = None
        self.runtime_fd: int | None = None
        self.mount_id: int | None = None
        self.mount_active = False
        self.children: dict[str, _Child] = {}
        self.rpc: _PrimeRPC | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.stop_state: ControlState | None = None
        self.stop_reason: str | None = None
        self.protocol_failure = False
        self.parent_lost = False
        self.cancelled = False
        self._control_registered = False
        self._control_closed = False
        self._started = time.monotonic()
        self._provisional: dict[str, Any] | None = None
        self._diagnostics = TailBuffer()

    def _validate_startup_fds(self) -> None:
        self.anchor_identity = anchor_identity(self.anchor_fd)
        flags = fcntl.fcntl(self.control_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.control_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        mode = os.fstat(self.control_fd).st_mode
        if not stat.S_ISFIFO(mode):
            raise ProtocolError("control FD is not a pipe")

    def _install_handlers(self) -> None:
        if self.loop is None:
            raise RuntimeError("worker event loop is unavailable")
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                self.loop.add_signal_handler(sig, self._signal_stop, sig)
            except (NotImplementedError, RuntimeError) as exc:
                raise UnsupportedLifecycleHost(f"signal handler setup failed for {sig}") from exc
        try:
            self.loop.add_signal_handler(signal.SIGCHLD, self._sigchld_hint)
        except (NotImplementedError, RuntimeError) as exc:
            raise UnsupportedLifecycleHost("SIGCHLD handler setup failed") from exc
        self.loop.add_reader(self.control_fd, self._control_ready)
        self._control_registered = True

    def _signal_stop(self, sig: signal.Signals) -> None:
        self.cancelled = True
        self._request_stop(f"received {sig.name}", None)

    def _sigchld_hint(self) -> None:
        # asyncio owns direct-child wait handles. Reap only descendants that
        # have already been reparented to namespace PID1.
        self._reap_orphans()

    def _request_stop(self, reason: str, state: ControlState | None) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        if state is not None and self.stop_state is None:
            self.stop_state = state
        if state == ControlState.PARENT_LOST:
            self.parent_lost = True
        if state == ControlState.INTENTIONAL_STOP:
            self.cancelled = True
        if self.stop_event is not None:
            self.stop_event.set()

    def _control_ready(self) -> None:
        if self._control_closed:
            return
        try:
            value = os.read(self.control_fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            self.protocol_failure = True
            self._request_stop(f"control read failed: {exc}", None)
            return
        if self.stop_state == ControlState.INTENTIONAL_STOP:
            if value:
                self.protocol_failure = True
                self._request_stop("control protocol received bytes after intentional stop", None)
            else:
                self._close_control_reader()
            return
        try:
            state = classify_control_bytes(value)
        except ProtocolError as exc:
            self.protocol_failure = True
            self._request_stop(str(exc), None)
            return
        self._request_stop("Hermes parent closed control" if state == ControlState.PARENT_LOST else "intentional stop", state)
        if state == ControlState.PARENT_LOST:
            self._close_control_reader()

    def _close_control_reader(self) -> None:
        if self._control_closed:
            return
        if self._control_registered and self.loop is not None:
            try:
                self.loop.remove_reader(self.control_fd)
            except Exception:
                pass
            self._control_registered = False
        try:
            os.close(self.control_fd)
        except OSError:
            pass
        self._control_closed = True

    def _read_pending_control(self) -> None:
        try:
            value = os.read(self.control_fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            raise ProtocolError(f"control read failed: {exc}") from exc
        if not value:
            self.parent_lost = True
            self.stop_state = ControlState.PARENT_LOST
            self.stop_reason = "Hermes parent control EOF"
            return
        state = classify_control_bytes(value)
        self._request_stop("Hermes parent control EOF" if state == ControlState.PARENT_LOST else "intentional stop", state)

    def _runtime_path(self, relative: str) -> str:
        if self.runtime_fd is None:
            raise RuntimeError("runtime FD is not open")
        if relative not in {"agent-home", "tmp"}:
            raise ValueError("runtime path must be a fixed private directory")
        return f"/proc/self/fd/{self.runtime_fd}/{relative}"

    def _emit_evidence(self, stage: str, **fields: Any) -> None:
        record = {"protocol_version": PROTOCOL_VERSION, "stage": stage, **fields}
        payload = EVIDENCE_PREFIX.encode("ascii") + _json_bytes(record) + b"\n"
        if len(payload) > MAX_READY_BYTES:
            raise ProtocolError("startup evidence record exceeds 64 KiB")
        os.write(2, payload)

    async def _read_request(self) -> dict[str, Any]:
        request = await asyncio.to_thread(read_frame_fd, 0, MAX_REQUEST_BYTES)
        allowed = {
            "protocol_version",
            "operation",
            "task",
            "workdir",
            "runtime",
            "route",
            "timeout_seconds",
            "session_mode",
            "session_directory",
            "resume_path",
            "relay_script",
            "extension",
            "synthetic_bearer",
            "anchor_path",
            "anchor_identity",
        }
        required = {
            "protocol_version",
            "operation",
            "task",
            "workdir",
            "runtime",
            "route",
            "timeout_seconds",
            "session_mode",
            "session_directory",
            "resume_path",
            "relay_script",
            "extension",
            "synthetic_bearer",
            "anchor_path",
            "anchor_identity",
        }
        _validate_exact_keys(request, allowed, required, "invocation request")
        if request["protocol_version"] != PROTOCOL_VERSION:
            raise ProtocolError("unsupported invocation protocol version")
        if request["operation"] not in {"delegate", "route_probe"}:
            raise ProtocolError("unsupported invocation operation")
        task = request["task"]
        if not isinstance(task, str) or not task or len(task.encode("utf-8")) > MAX_TASK_BYTES:
            raise ProtocolError("task is empty or exceeds the bounded request limit")
        for key in ("workdir", "runtime", "relay_script", "extension", "anchor_path"):
            if not isinstance(request[key], str) or not os.path.isabs(request[key]):
                raise ProtocolError(f"{key} must be an absolute path")
        if not isinstance(request["synthetic_bearer"], str) or not request["synthetic_bearer"]:
            raise ProtocolError("synthetic bearer is missing")
        route = request["route"]
        if not isinstance(route, dict):
            raise ProtocolError("route must be an object")
        _validate_exact_keys(route, {"provider", "model", "reasoning_effort"}, {"provider", "model", "reasoning_effort"}, "route")
        if request["session_mode"] not in {"ephemeral", "resumable"}:
            raise ProtocolError("invalid session mode")
        for key in ("session_directory", "resume_path"):
            value = request[key]
            if value is not None and (not isinstance(value, str) or not os.path.isabs(value)):
                raise ProtocolError(f"{key} must be null or an absolute path")
        identity = request["anchor_identity"]
        if not isinstance(identity, dict):
            raise ProtocolError("anchor identity is missing")
        local_identity = anchor_identity(self.anchor_fd)
        for key in ("dev", "ino", "mode"):
            if local_identity[key] != identity.get(key):
                raise ProtocolError("mount anchor identity changed across namespace handoff")
        # The host-side uid is intentionally retained in the request for
        # admission evidence.  Inside --map-root-user the same inode is
        # represented as uid 0, so use the worker-local identity for the
        # pathname check rather than comparing unmapped uid values.
        verify_anchor_path(Path(request["anchor_path"]), self.anchor_fd, local_identity)
        return request

    @staticmethod
    def _validate_command(value: Any, name: str) -> None:
        if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
            raise ProtocolError(f"{name} must be a non-empty argv list")

    async def _start_child(
        self,
        *,
        name: str,
        command: Sequence[str],
        env: Mapping[str, str],
        cwd: Path,
    ) -> _Child:
        if self.runtime_fd is None:
            raise RuntimeError("runtime FD is unavailable")
        unshare = shutil.which("unshare")
        if unshare is None:
            raise UnsupportedLifecycleHost("unshare is unavailable for nested child")
        child_runtime_fd = os.dup(self.runtime_fd)
        os.set_inheritable(child_runtime_fd, True)
        child_env = remap_runtime_environment(
            env,
            worker_runtime_fd=self.runtime_fd,
            child_runtime_fd=child_runtime_fd,
        )
        nested = [
            unshare,
            "--user",
            "--map-root-user",
            "--fork",
            "--kill-child=SIGKILL",
            *command,
        ]
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *nested,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
                cwd=str(cwd),
                close_fds=True,
                pass_fds=(child_runtime_fd,),
                limit=MAX_RPC_LINE_BYTES + 1,
            )
        )
        spawn_cancelled = False
        try:
            while not spawn_task.done():
                try:
                    await asyncio.shield(spawn_task)
                except asyncio.CancelledError:
                    spawn_cancelled = True
            process = spawn_task.result()
        finally:
            os.close(child_runtime_fd)
        child = _Child(name=name, process=process)
        child.stderr_task = asyncio.create_task(drain_stream(process.stderr, child.stderr_tail))
        self.children[name] = child
        if spawn_cancelled:
            raise asyncio.CancelledError
        return child

    async def _execute_operation(self, request: Mapping[str, Any]) -> dict[str, Any]:
        relay, proxy_url = await self._start_relay(request)
        del relay
        self._emit_evidence("relay_ready", listener=proxy_url)
        return await self._run_prime(request, proxy_url)

    @staticmethod
    def _allowlisted_environment(*, relay: bool) -> dict[str, str]:
        allowed = {"LANG", "PATH", "PYTHONIOENCODING", "TZ", "VIRTUAL_ENV"}
        if relay:
            allowed.update(
                {
                    "HERMES_HOME",
                    "HOME",
                    "PYTHONPATH",
                    "REQUESTS_CA_BUNDLE",
                    "SSL_CERT_DIR",
                    "SSL_CERT_FILE",
                }
            )
        env = {
            key: value
            for key, value in os.environ.items()
            if key in allowed or key.startswith("LC_")
        }
        env.update({"DO_NOT_TRACK": "1", "PRIME_AGENT_TELEMETRY": "0"})
        return env

    @staticmethod
    def _route_effective(state: Mapping[str, Any]) -> dict[str, str]:
        model = state.get("model")
        if not isinstance(model, dict):
            raise ProtocolError("Prime state has no model")
        provider = model.get("provider")
        model_id = model.get("id")
        thinking = state.get("thinkingLevel")
        effort_map = {"off": "none", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
        effort = effort_map.get(str(thinking))
        if not all(isinstance(value, str) for value in (provider, model_id, effort)):
            raise ProtocolError("Prime state has an invalid effective route")
        return {"provider": provider, "model": model_id, "reasoning_effort": effort}

    @staticmethod
    def _prime_effort(effort: str) -> str:
        mapping = {"none": "off", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
        try:
            return mapping[effort]
        except KeyError as exc:
            raise ProtocolError("unsupported reasoning effort") from exc

    async def _start_relay(self, request: Mapping[str, Any]) -> tuple[_Child, str]:
        workdir = Path(str(request["workdir"]))
        if self.relay_command is not None:
            command = list(self.relay_command)
        else:
            command = [sys.executable, str(request["relay_script"]), "--parent-pid", str(os.getpid())]
        env = self._allowlisted_environment(relay=True)
        env["PRIME_AGENT_CODING_AGENT_DIR"] = self._runtime_path("agent-home")
        child = await self._start_child(name="relay", command=command, env=env, cwd=workdir)
        if child.process.stdin is not None:
            child.process.stdin.write((str(request["synthetic_bearer"]) + "\n").encode("utf-8"))
            await child.process.stdin.drain()
            child.process.stdin.close()
        stdout = child.process.stdout
        if stdout is None:
            raise ProtocolError("relay stdout is unavailable")
        try:
            raw, remainder = await read_bounded_line(stdout, maximum=MAX_READY_BYTES, timeout=30.0)
        except Exception:
            child.stdout_task = asyncio.create_task(drain_stream(stdout, child.stdout_tail))
            raise
        if not raw:
            raise ProtocolError("relay exited before readiness")
        try:
            ready = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("relay readiness is malformed") from exc
        if not isinstance(ready, dict) or ready.get("ready") is not True:
            raise ProtocolError(str(ready.get("error") if isinstance(ready, dict) else "relay is not ready"))
        host = ready.get("host")
        port = ready.get("port")
        if host != "127.0.0.1" or not isinstance(port, int) or not 0 < port < 65_536:
            raise ProtocolError("relay readiness violated loopback contract")
        child.stdout_tail.append(remainder)
        child.stdout_task = asyncio.create_task(drain_stream(stdout, child.stdout_tail))
        return child, f"http://127.0.0.1:{port}/v1"

    def _prime_command(self, request: Mapping[str, Any]) -> list[str]:
        if self.prime_command is not None:
            return list(self.prime_command)
        runtime = Path(str(request["runtime"]))
        node = shutil.which("node")
        tsx_package = runtime / "node_modules" / "tsx" / "package.json"
        embedded = Path(__file__).resolve().parent / "embedded_rpc.mjs"
        if node is None or not tsx_package.is_file() or not embedded.is_file():
            raise UnsupportedLifecycleHost("embedded Prime RPC runtime is missing")
        route = request["route"]
        command = [
            node,
            "--import",
            "tsx",
            str(embedded),
            "--mode",
            "rpc",
            "--no-extensions",
            "--provider",
            str(route["provider"]),
            "--model",
            str(route["model"]),
            "--thinking",
            self._prime_effort(str(route["reasoning_effort"])),
            "--cwd",
            str(request["workdir"]),
        ]
        if request["session_mode"] == "ephemeral":
            command.append("--no-session")
        else:
            command.extend(["--session-dir", str(request["session_directory"])])
            if request.get("resume_path") is not None:
                command.extend(["--resume", str(request["resume_path"])])
        return command

    async def _start_prime(self, request: Mapping[str, Any], proxy_url: str) -> _Child:
        route = request["route"]
        env = self._allowlisted_environment(relay=False)
        if self.prime_command is None:
            kernel_python = Path(str(request["runtime"])).parent / "kernel-venv" / "bin" / "python"
            if not kernel_python.is_file() or not os.access(kernel_python, os.X_OK):
                raise UnsupportedLifecycleHost(
                    f"Prime kernel runtime is missing or not executable: {kernel_python}"
                )
            env["PRIME_AGENT_KERNEL_PYTHON"] = str(kernel_python)
        env.update(
            {
                "HOME": self._runtime_path("agent-home"),
                "PRIME_AGENT_CODING_AGENT_DIR": self._runtime_path("agent-home"),
                "TMPDIR": self._runtime_path("tmp"),
                "HERMES_MINION_PROXY_BASE_URL": proxy_url,
                "HERMES_MINION_PROXY_API_KEY": str(request["synthetic_bearer"]),
                "HERMES_PRIME_MINION_RUNTIME_ROOT": str(request["runtime"]),
            }
        )
        command = self._prime_command(request)
        child = await self._start_child(
            name="prime",
            command=command,
            env=env,
            cwd=Path(str(request["runtime"])),
        )
        stdout = child.process.stdout
        if stdout is None:
            raise ProtocolError("Prime RPC stdout is unavailable")
        # The RPC reader must be installed before the first write and remains
        # alive until EOF, including while cleanup is terminating the child.
        self.rpc = _PrimeRPC(child.process)
        del route, stdout
        return child

    async def _run_prime(self, request: Mapping[str, Any], proxy_url: str) -> dict[str, Any]:
        child = await self._start_prime(request, proxy_url)
        self._emit_evidence("prime_running", prime_pid=child.process.pid)
        if self.rpc is None:
            raise RuntimeError("Prime RPC reader was not initialized")
        route = dict(request["route"])
        initial = await self.rpc.command(
            request_id="hermes-minion-state-initial",
            command={"type": "get_state"},
            expected_command="get_state",
        )
        state = initial.get("data")
        if not isinstance(state, dict):
            raise ProtocolError("Prime Agent returned malformed initial state")
        current = self._route_effective(state)
        if current["provider"] != route["provider"] or current["model"] != route["model"]:
            await self.rpc.command(
                request_id="hermes-minion-set-model",
                command={"type": "set_model", "provider": route["provider"], "modelId": route["model"]},
                expected_command="set_model",
            )
        if current["reasoning_effort"] != route["reasoning_effort"]:
            await self.rpc.command(
                request_id="hermes-minion-set-thinking",
                command={"type": "set_thinking_level", "level": self._prime_effort(route["reasoning_effort"])},
                expected_command="set_thinking_level",
            )
        effective_state_response = await self.rpc.command(
            request_id="hermes-minion-state-effective",
            command={"type": "get_state"},
            expected_command="get_state",
        )
        effective_state = effective_state_response.get("data")
        if not isinstance(effective_state, dict):
            raise ProtocolError("Prime Agent returned malformed effective state")
        effective = self._route_effective(effective_state)
        if effective != route:
            raise ProtocolError(f"Prime Agent effective route mismatch: requested {route}, observed {effective}")
        if request["operation"] == "route_probe" and request["session_mode"] == "resumable":
            await self.rpc.command(
                request_id="hermes-minion-route-probe-session-name",
                command={"type": "set_session_name", "name": "hermes-minion-route-probe"},
                expected_command="set_session_name",
            )
        tool_calls: list[dict[str, Any]] = []
        final_messages: list[Any] = []
        prompt_accepted = False
        if request["operation"] == "delegate":
            await self.rpc.command(
                request_id="hermes-minion-task",
                command={"type": "prompt", "message": str(request["task"])},
                expected_command="prompt",
            )
            prompt_accepted = True
            while True:
                event = await self.rpc.next_event(max(1.0, float(request["timeout_seconds"])))
                if event.get("type") == "tool_execution_end":
                    tool_calls.append(
                        {"name": event.get("toolName"), "is_error": bool(event.get("isError", False))}
                    )
                elif event.get("type") == "agent_end":
                    messages = event.get("messages")
                    final_messages = messages if isinstance(messages, list) else []
                    break
        final_state_response = await self.rpc.command(
            request_id="hermes-minion-state-final",
            command={"type": "get_state"},
            expected_command="get_state",
        )
        final_state = final_state_response.get("data")
        if not isinstance(final_state, dict):
            raise ProtocolError("Prime Agent returned malformed final state")
        final_effective = self._route_effective(final_state)
        if final_effective != effective:
            raise ProtocolError("Prime Agent route changed during the task")
        result = {
            "status": "completed",
            "operation": request["operation"],
            "route": route,
            "effective_route": final_effective,
            "result": (
                self._assistant_text(final_messages)
                if request["operation"] == "delegate"
                else "ROUTE_PROBE_OK"
            ),
            "usage": self._usage(final_messages),
            "tool_calls": tool_calls,
            "event_count": self.rpc.event_count,
            "prompt_accepted": prompt_accepted,
            "provider_request_sent": prompt_accepted,
            "duration_seconds": round(time.monotonic() - self._started, 3),
            "prime_session_id": final_state.get("sessionId") or effective_state.get("sessionId"),
            "session_file": final_state.get("sessionFile") or effective_state.get("sessionFile"),
            "worker_pid_namespace_pid": os.getpid(),
            "relay_listener": proxy_url,
        }
        if request["session_mode"] == "ephemeral":
            result.pop("prime_session_id", None)
            result.pop("session_file", None)
        return result

    @staticmethod
    def _assistant_text(messages: Iterable[Any]) -> str:
        for message in reversed(list(messages)):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            return "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            ).strip()
        return ""

    @staticmethod
    def _usage(messages: Iterable[Any]) -> dict[str, int]:
        result = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            result["input"] += int(usage.get("input") or 0)
            result["output"] += int(usage.get("output") or 0)
            result["cache_read"] += int(usage.get("cacheRead") or 0)
            result["cache_write"] += int(usage.get("cacheWrite") or 0)
        result["total"] = sum(result[key] for key in ("input", "output", "cache_read", "cache_write"))
        return result

    async def _terminate_children(self) -> bool:
        if self.rpc is not None:
            await self.rpc.abort()
        deadline = time.monotonic() + 5.0
        for pid in _namespace_pids():
            if pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    self.protocol_failure = True
        while time.monotonic() < deadline:
            if all(child.process.returncode is not None for child in self.children.values()) and _namespace_pids() == [os.getpid()]:
                break
            await asyncio.sleep(0.05)
        for pid in _namespace_pids():
            if pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    self.protocol_failure = True
        hard_deadline = time.monotonic() + 5.0
        while time.monotonic() < hard_deadline:
            self._reap_orphans()
            if _namespace_pids() == [os.getpid()] and all(child.process.returncode is not None for child in self.children.values()):
                break
            await asyncio.sleep(0.05)
        self._reap_orphans()
        return _namespace_pids() == [os.getpid()] and all(child.process.returncode is not None for child in self.children.values())

    def _reap_orphans(self) -> None:
        direct = {child.process.pid for child in self.children.values()}
        for pid in _namespace_pids():
            if pid == os.getpid() or pid in direct:
                continue
            try:
                raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
                close = raw.rfind(")")
                fields = raw[close + 2 :].split() if close >= 0 else []
                parent_pid = int(fields[1]) if len(fields) > 1 else -1
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if parent_pid != os.getpid():
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                continue
            except OSError as exc:
                if exc.errno in {errno.ECHILD, errno.EINTR}:
                    continue
                self.protocol_failure = True

    async def _settle_drain_task(self, task: asyncio.Task[Any], name: str) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            self.protocol_failure = True
            self._diagnostics.append(f"{name} drain exceeded cleanup deadline".encode())
        except Exception as exc:
            self.protocol_failure = True
            self._diagnostics.append(str(exc).encode())

    async def _cleanup(self) -> CleanupVerdict:
        children_reaped = await self._terminate_children()
        if self.rpc is not None:
            await self._settle_drain_task(self.rpc.reader_task, "Prime RPC")
        for child in self.children.values():
            for task in (child.stdout_task, child.stderr_task):
                if task is not None:
                    await self._settle_drain_task(task, f"{child.name} output")
            self._diagnostics.append(child.stderr_tail.bytes())
        mount_absent = True
        cleanup_error: str | None = None
        if self.mount_active:
            try:
                if self.runtime_fd is None or self.mount_id is None:
                    raise RuntimeError("mounted tmpfs lost its R or mount identity")
                mount_absent = detach_tmpfs(self.runtime_fd, self.mount_id)
            except Exception as exc:
                mount_absent = False
                cleanup_error = str(exc)
        if self.runtime_fd is not None:
            try:
                os.close(self.runtime_fd)
            except OSError:
                pass
            self.runtime_fd = None
        self._close_control_reader()
        try:
            os.close(self.anchor_fd)
        except OSError:
            pass
        clean = children_reaped and mount_absent and not self.protocol_failure and cleanup_error is None
        return CleanupVerdict(
            clean=clean,
            mount_absent=mount_absent,
            children_reaped=children_reaped,
            protocol_failure=self.protocol_failure,
            parent_lost=self.parent_lost,
            cancellation=self.cancelled,
            diagnostics_drained=True,
            error=cleanup_error,
        )

    async def run(self) -> int:
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        operation_task: asyncio.Task[dict[str, Any]] | None = None
        stop_task: asyncio.Task[bool] | None = None
        try:
            self._validate_startup_fds()
            self._install_handlers()
            self._emit_evidence(
                "handlers",
                worker_pid_namespace_pid=os.getpid(),
                pid_namespace_inode=os.stat("/proc/self/ns/pid").st_ino,
                mount_namespace_inode=os.stat("/proc/self/ns/mnt").st_ino,
            )
            self._read_pending_control()
            if self.require_pid1 and os.getpid() != 1:
                raise UnsupportedLifecycleHost("invocation worker is not PID1")
            request = await self._read_request()
            self.anchor_path = Path(str(request["anchor_path"]))
            if self.stop_reason is not None:
                raise ProtocolError(self.stop_reason)
            if self.runtime_fd is not None:
                raise RuntimeError("runtime FD was already initialized")
            self.mount_active = True
            created_mount_id = mount_tmpfs(self.anchor_fd)
            self.runtime_fd, self.mount_id = open_verified_runtime_fd(self.anchor_path, created_mount_id)
            self._emit_evidence("mounted", mount_id=self.mount_id)
            os.mkdir(self._runtime_path("agent-home"), 0o700)
            os.mkdir(self._runtime_path("tmp"), 0o700)
            if self.stop_reason is not None:
                raise ProtocolError(self.stop_reason)
            operation_task = asyncio.create_task(self._execute_operation(request))
            stop_task = asyncio.create_task(self.stop_event.wait())
            done, _ = await asyncio.wait(
                {operation_task, stop_task},
                timeout=float(request["timeout_seconds"]),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                operation_task.cancel()
                try:
                    await operation_task
                except asyncio.CancelledError:
                    pass
                raise asyncio.CancelledError
            if operation_task not in done:
                self.cancelled = True
                self.stop_reason = self.stop_reason or "invocation timed out"
                operation_task.cancel()
                try:
                    await operation_task
                except asyncio.CancelledError:
                    pass
                raise asyncio.TimeoutError
            self._provisional = operation_task.result()
            self._emit_evidence("result_ready")
            if self.stop_reason is not None:
                raise ProtocolError(self.stop_reason)
        except asyncio.TimeoutError:
            self.cancelled = True
            self.stop_reason = self.stop_reason or "invocation timed out"
        except asyncio.CancelledError:
            self.cancelled = True
            self.stop_reason = self.stop_reason or "worker task cancelled"
        except (ProtocolError, UnsupportedLifecycleHost, OSError, RuntimeError) as exc:
            self.stop_reason = self.stop_reason or str(exc)
            if isinstance(exc, (ProtocolError, UnsupportedLifecycleHost)):
                self.protocol_failure = isinstance(exc, ProtocolError)
        except Exception as exc:
            self.stop_reason = self.stop_reason or str(exc)
            self.protocol_failure = True
        finally:
            if stop_task is not None and not stop_task.done():
                stop_task.cancel()
                try:
                    await stop_task
                except asyncio.CancelledError:
                    pass
        cleanup = await self._cleanup()
        self._emit_evidence(
            "cleanup_complete",
            cleanup_verified=cleanup.clean,
            mount_absent=cleanup.mount_absent,
            children_reaped=cleanup.children_reaped,
        )
        success = accept_success_after_cleanup(
            provisional_result=self._provisional,
            cleanup=cleanup,
            parent_lost=self.parent_lost,
        )
        if success:
            payload = dict(self._provisional or {})
            payload["cleanup_verified"] = True
            payload["mount_absent"] = True
            payload["children_reaped"] = True
            payload["diagnostic_tail"] = self._diagnostics.text()[-2_000:]
            exit_code = WORKER_SUCCESS
        else:
            payload = {
                "status": "error",
                "error": self.stop_reason or cleanup.error or "invocation lifecycle failed",
                "route": (request.get("route") if "request" in locals() else {}),
                "cleanup_verified": cleanup.clean,
                "mount_absent": cleanup.mount_absent,
                "children_reaped": cleanup.children_reaped,
                "diagnostic_tail": self._diagnostics.text()[-2_000:],
            }
            exit_code = WORKER_FAILURE
        try:
            os.write(1, encode_frame(payload, MAX_RESULT_BYTES))
        except (BrokenPipeError, OSError):
            exit_code = WORKER_FAILURE
        return exit_code


def _decode_test_fixture(value: str | None) -> tuple[list[str] | None, list[str] | None]:
    if value is None:
        return None, None
    try:
        decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        fixture = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("test worker fixture is malformed") from exc
    if not isinstance(fixture, dict):
        raise ProtocolError("test worker fixture must be an object")
    _validate_exact_keys(fixture, {"relay", "prime"}, {"relay", "prime"}, "test worker fixture")
    for key in ("relay", "prime"):
        InvocationWorker._validate_command(fixture[key], key)
    return list(fixture["relay"]), list(fixture["prime"])


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prime Minion per-invocation lifecycle worker")
    parser.add_argument("--control-fd", type=int, required=True)
    parser.add_argument("--anchor-fd", type=int, required=True)
    parser.add_argument("--test-fixture-b64", help=argparse.SUPPRESS)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        relay_command, prime_command = _decode_test_fixture(args.test_fixture_b64)
        worker = InvocationWorker(
            control_fd=args.control_fd,
            anchor_fd=args.anchor_fd,
            relay_command=relay_command,
            prime_command=prime_command,
        )
        return asyncio.run(worker.run())
    except (ProtocolError, UnsupportedLifecycleHost, OSError) as exc:
        try:
            payload = {"status": "error", "error": str(exc), "cleanup_verified": False, "mount_absent": False, "children_reaped": False}
            os.write(1, encode_frame(payload, MAX_RESULT_BYTES))
        except OSError:
            pass
        return WORKER_FAILURE


__all__ = [
    "CAPABILITY_PROFILE",
    "CleanupVerdict",
    "ControlState",
    "InvocationWorker",
    "MAX_DIAGNOSTIC_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_RESULT_BYTES",
    "MAX_RPC_LINE_BYTES",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "UnsupportedLifecycleHost",
    "accept_success_after_cleanup",
    "anchor_identity",
    "anchor_receipt_path",
    "assert_no_recursive_anchor_cleanup",
    "check_capability_profile",
    "classify_control_bytes",
    "decode_frame",
    "detach_tmpfs",
    "encode_frame",
    "is_persistent_anchor",
    "mount_tmpfs",
    "mountinfo_for_path",
    "open_anchor",
    "open_provisioned_anchor",
    "open_verified_runtime_fd",
    "process_identity",
    "provision_fixed_anchor",
    "read_frame_fd",
    "read_frame_stream",
    "verify_anchor_path",
    "verify_fixed_anchor",
]


if __name__ == "__main__":
    raise SystemExit(main())
