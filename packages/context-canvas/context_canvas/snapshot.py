from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # Linux/WSL cross-process locking; thread locks remain the fallback.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback only
    fcntl = None  # type: ignore[assignment]


SNAPSHOT_SCHEMA_VERSION = 2
METRIC_SCHEMA_VERSION = 2
_SAFE_NAME = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def safe_name(value: str, *, default: str = "session", limit: int = 96) -> str:
    cleaned = "".join(ch if ch in _SAFE_NAME else "-" for ch in str(value).strip())
    cleaned = cleaned.strip("-._")
    return (cleaned[:limit] or default)


def session_component(value: str) -> str:
    """Collision-resistant storage component; the slug is display-only."""
    canonical = str(value)
    slug = safe_name(canonical, default="session", limit=64)
    digest = hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    return f"{slug}-{digest}"


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _lstat_regular_owned(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"controlled path is not a regular file: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"controlled file has unexpected owner: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError(f"controlled file is not owner-only: {path}")
    return info


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"controlled path is not a directory: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"controlled directory has unexpected owner: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        # The directory is created and exclusively controlled by this component.
        # Verify ownership before tightening permissions; never follow symlinks.
        os.chmod(path, 0o700, follow_symlinks=False)
        tightened = path.lstat()
        if not stat.S_ISDIR(tightened.st_mode) or stat.S_IMODE(tightened.st_mode) & 0o077:
            raise PermissionError(f"could not make controlled directory private: {path}")


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":  # pragma: no cover - best effort outside POSIX
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    _ensure_private_dir(path.parent)
    if path.exists() or path.is_symlink():
        _lstat_regular_owned(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _lstat_regular_owned(path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _atomic_write_bytes(path, encoded)


@contextmanager
def _private_lock(lock_path: Path, *, shared: bool = False) -> Iterator[None]:
    _ensure_private_dir(lock_path.parent)
    thread_lock = _thread_lock_for(lock_path)
    with thread_lock:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"lock path is not a regular file: {lock_path}")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise PermissionError(f"lock file has unexpected owner: {lock_path}")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError(f"lock file is not owner-only: {lock_path}")
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class SnapshotStore:
    """Private, content-addressed point-in-time snapshot cache.

    The store keeps full *sanitized* invocation/result envelopes outside the
    semantic Canvas tree. Session manifests point to immutable objects; Canvas
    refs may promote selected manifests without copying their full bodies.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        _ensure_private_dir(self.root)
        for relative in (
            ("objects",),
            ("objects", "text"),
            ("objects", "text", "sha256"),
            ("objects", "binary"),
            ("objects", "binary", "sha256"),
            ("sessions",),
        ):
            _ensure_private_dir(self.root.joinpath(*relative))

    def _object_path(self, digest: str, *, binary: bool) -> Path:
        family = "binary" if binary else "text"
        suffix = ".bin" if binary else ".json.zlib"
        return self.root / "objects" / family / "sha256" / digest[:2] / f"{digest}{suffix}"

    def _put_object(self, raw: bytes, *, binary: bool) -> dict[str, Any]:
        digest = hashlib.sha256(raw).hexdigest()
        path = self._object_path(digest, binary=binary)
        _ensure_private_dir(path.parent)
        stored = raw if binary else zlib.compress(raw, level=6)
        reused = False
        if path.exists() or path.is_symlink():
            _lstat_regular_owned(path)
            existing = path.read_bytes()
            decoded = existing if binary else zlib.decompress(existing)
            if hashlib.sha256(decoded).hexdigest() != digest:
                raise RuntimeError(f"existing object failed digest verification: {path}")
            reused = True
        else:
            _atomic_write_bytes(path, stored)
        return {
            "sha256": digest,
            "object_path": str(path),
            "object_relpath": str(path.relative_to(self.root)),
            "raw_bytes": len(raw),
            "stored_bytes": path.stat().st_size,
            "reused": reused,
            "encoding": "raw" if binary else "zlib+json+utf-8",
        }

    def put_binary(self, data: bytes) -> dict[str, Any]:
        return self._put_object(bytes(data), binary=True)

    def put_envelope(self, envelope: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self._put_object(raw, binary=False)

    def read_envelope(self, digest: str) -> dict[str, Any]:
        path = self._object_path(digest, binary=False)
        _lstat_regular_owned(path)
        raw = zlib.decompress(path.read_bytes())
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RuntimeError(f"snapshot object digest mismatch: {path}")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"snapshot envelope is not an object: {path}")
        return payload

    def read_binary(self, digest: str) -> bytes:
        path = self._object_path(digest, binary=True)
        _lstat_regular_owned(path)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RuntimeError(f"binary object digest mismatch: {path}")
        return raw

    def record_manifest(self, session_key: str, manifest: dict[str, Any]) -> dict[str, Any]:
        safe_session = session_component(session_key)
        session_dir = self.root / "sessions" / safe_session
        snapshots_dir = session_dir / "snapshots"
        events_dir = session_dir / "events"
        _ensure_private_dir(session_dir)
        _ensure_private_dir(snapshots_dir)
        _ensure_private_dir(events_dir)
        event_id = str(manifest.get("event_id", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", event_id):
            raise ValueError("manifest requires a lowercase sha256 event_id")
        event_path = events_dir / f"{event_id}.json"
        lock_path = session_dir / ".snapshot.lock"
        state_path = session_dir / "state.json"
        with _private_lock(lock_path):
            if event_path.exists() or event_path.is_symlink():
                _lstat_regular_owned(event_path)
                pointer = json.loads(event_path.read_text(encoding="utf-8"))
                if not isinstance(pointer, dict):
                    raise ValueError(f"event pointer is not an object: {event_path}")
                existing_path = snapshots_dir / f"{pointer.get('snapshot_id', '')}.json"
                _lstat_regular_owned(existing_path)
                payload = json.loads(existing_path.read_text(encoding="utf-8"))
                if payload.get("event_id") != event_id:
                    raise RuntimeError(f"event pointer identity mismatch: {event_path}")
                if payload.get("object_sha256") != manifest.get("object_sha256"):
                    raise RuntimeError(f"duplicate event_id has conflicting payload: {event_id}")
                return {
                    "snapshot_id": payload["snapshot_id"],
                    "manifest_path": str(existing_path),
                    "manifest_relpath": str(existing_path.relative_to(self.root)),
                    "manifest_bytes": existing_path.stat().st_size,
                    "manifest": payload,
                    "duplicate_event": True,
                }
            if state_path.exists() or state_path.is_symlink():
                _lstat_regular_owned(state_path)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    raise ValueError(f"snapshot state is not an object: {state_path}")
            else:
                state = {"next_snapshot": 1, "snapshot_count": 0}
            next_snapshot = int(state.get("next_snapshot", 1))
            while (snapshots_dir / f"sr_{next_snapshot:06d}.json").exists():
                next_snapshot += 1
            snapshot_id = f"sr_{next_snapshot:06d}"
            manifest_path = snapshots_dir / f"{snapshot_id}.json"
            payload = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "session_key": safe_session,
                "captured_at": now_iso(),
                **manifest,
            }
            _atomic_write_json(manifest_path, payload)
            state.update(
                {
                    "next_snapshot": next_snapshot + 1,
                    "snapshot_count": int(state.get("snapshot_count", 0)) + 1,
                    "updated_at": payload["captured_at"],
                }
            )
            _atomic_write_json(state_path, state)
            _atomic_write_json(
                event_path,
                {
                    "schema_version": SNAPSHOT_SCHEMA_VERSION,
                    "event_id": event_id,
                    "snapshot_id": snapshot_id,
                    "object_sha256": payload.get("object_sha256"),
                    "committed_at": now_iso(),
                },
            )
        return {
            "snapshot_id": snapshot_id,
            "manifest_path": str(manifest_path),
            "manifest_relpath": str(manifest_path.relative_to(self.root)),
            "manifest_bytes": manifest_path.stat().st_size,
            "manifest": payload,
            "duplicate_event": False,
        }

    def update_session_state(self, session_key: str, updates: dict[str, Any]) -> dict[str, Any]:
        safe_session = session_component(session_key)
        session_dir = self.root / "sessions" / safe_session
        _ensure_private_dir(session_dir)
        lock_path = session_dir / ".snapshot.lock"
        path = session_dir / "lifecycle.json"
        with _private_lock(lock_path):
            if path.exists() or path.is_symlink():
                _lstat_regular_owned(path)
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError(f"lifecycle state is not an object: {path}")
            else:
                payload = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "session_key": safe_session}
            payload.update(updates)
            payload["updated_at"] = now_iso()
            _atomic_write_json(path, payload)
        return payload

    def validate_manifest(self, path: str | Path) -> dict[str, Any]:
        manifest_path = Path(path)
        _lstat_regular_owned(manifest_path)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"invalid snapshot manifest schema: {manifest_path}")
        digest = payload.get("object_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"invalid snapshot object digest: {manifest_path}")
        envelope = self.read_envelope(digest)
        embedded = payload.get("embedded_objects", [])
        if not isinstance(embedded, list):
            raise ValueError(f"embedded_objects must be a list: {manifest_path}")
        for item in embedded:
            if not isinstance(item, dict):
                raise ValueError(f"embedded object metadata must be an object: {manifest_path}")
            binary_digest = item.get("sha256")
            if not isinstance(binary_digest, str) or len(binary_digest) != 64:
                raise ValueError(f"invalid binary object digest: {manifest_path}")
            self.read_binary(binary_digest)
        return {"ok": True, "manifest": payload, "envelope": envelope}


class PrivateJsonlLedger:
    """Owner-only append-only JSONL with a shared writer/reader lock."""

    def __init__(self, root: str | Path, filename: str = "metrics.jsonl") -> None:
        self.root = Path(root).expanduser()
        _ensure_private_dir(self.root)
        self.path = self.root / safe_name(filename, default="metrics.jsonl", limit=80)
        self.lock_path = self.root / ".ledger.lock"

    def append(self, record: dict[str, Any], *, durable: bool = True) -> None:
        raw = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with _private_lock(self.lock_path):
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError(f"ledger path is not a regular file: {self.path}")
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    raise PermissionError(f"ledger has unexpected owner: {self.path}")
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise PermissionError(f"ledger is not owner-only: {self.path}")
                view = memoryview(raw)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short write while appending private ledger")
                    view = view[written:]
                if durable:
                    os.fsync(fd)
            finally:
                os.close(fd)

    def flush(self) -> None:
        if not self.path.exists():
            return
        with _private_lock(self.lock_path):
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError(f"ledger path is not a regular file: {self.path}")
                os.fsync(fd)
            finally:
                os.close(fd)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with _private_lock(self.lock_path, shared=True):
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError(f"ledger path is not a regular file: {self.path}")
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    raise PermissionError(f"ledger has unexpected owner: {self.path}")
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    records: list[dict[str, Any]] = []
                    for line_no, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        payload = json.loads(line)
                        if not isinstance(payload, dict):
                            raise ValueError(f"ledger row {line_no} is not an object")
                        records.append(payload)
                    return records
            finally:
                if fd >= 0:
                    os.close(fd)
