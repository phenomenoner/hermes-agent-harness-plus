"""Durable metadata and leases for resumable Prime minion sessions."""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

SESSION_SCHEMA_VERSION = 1
SESSION_ID_PATTERN = re.compile(r"^minion_[0-9a-f]{32}$")
_TERMINAL_STATES = frozenset({"IDLE", "INTERRUPTED", "CLOSED"})


class SessionError(RuntimeError):
    pass


class SessionBusyError(SessionError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_root() -> Path:
    configured = os.environ.get("PRIME_MINION_STATE_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        try:
            from hermes_constants import get_hermes_home

            hermes_home = get_hermes_home()
        except ImportError:
            hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        root = Path(hermes_home).expanduser().resolve() / "state" / "prime-minion"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def validate_session_id(session_id: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise SessionError("invalid minion session_id")
    return session_id


def session_root(session_id: str) -> Path:
    validate_session_id(session_id)
    return state_root() / "sessions" / session_id


def transcript_dir(session_id: str) -> Path:
    path = session_root(session_id) / "transcript"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _manifest_path(session_id: str) -> Path:
    return session_root(session_id) / "manifest.json"


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def create_manifest(*, workdir: Path, prime_commit: str) -> dict[str, Any]:
    sessions_dir = state_root() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    while True:
        session_id = f"minion_{uuid.uuid4().hex}"
        root = sessions_dir / session_id
        try:
            root.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    (root / "transcript").mkdir(mode=0o700)
    timestamp = _now()
    manifest: dict[str, Any] = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session_id,
        "state": "IDLE",
        "generation": 0,
        "canonical_workdir": str(workdir.resolve()),
        "prime_commit": prime_commit,
        "prime_session_id": None,
        "session_file": None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "closed_at": None,
        "turns": [],
    }
    _atomic_write_json(root / "manifest.json", manifest)
    return manifest


def load_manifest(session_id: str) -> dict[str, Any]:
    path = _manifest_path(session_id)
    if not path.is_file():
        raise SessionError("minion session not found")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError("minion session manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise SessionError("minion session manifest is invalid")
    if value.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise SessionError("unsupported minion session manifest version")
    if value.get("session_id") != session_id:
        raise SessionError("minion session manifest identity mismatch")
    if value.get("state") not in _TERMINAL_STATES | {"RUNNING"}:
        raise SessionError("minion session manifest has an invalid state")
    if not isinstance(value.get("turns"), list):
        raise SessionError("minion session manifest has invalid turn history")
    return value


def write_manifest(manifest: dict[str, Any]) -> None:
    session_id = validate_session_id(str(manifest.get("session_id") or ""))
    manifest["updated_at"] = _now()
    _atomic_write_json(_manifest_path(session_id), manifest)


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


def _boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def owner_identity(pid: int | None = None) -> dict[str, int | str]:
    owner_pid = os.getpid() if pid is None else int(pid)
    start_time = _proc_start_time(owner_pid)
    boot_id = _boot_id()
    if start_time is None or boot_id is None:
        raise SessionError("exact process identity is unavailable")
    return {
        "pid": owner_pid,
        "proc_start_time": start_time,
        "boot_id": boot_id,
    }


def _read_lock_owner(lock_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _owner_is_live(owner: Mapping[str, Any] | None) -> bool:
    if not isinstance(owner, Mapping):
        return False
    pid = owner.get("pid")
    start_time = owner.get("proc_start_time")
    boot_id = owner.get("boot_id")
    if not isinstance(pid, int) or not isinstance(start_time, str) or not isinstance(boot_id, str):
        return False
    try:
        current = owner_identity(pid)
    except SessionError:
        return False
    return current == {
        "pid": pid,
        "proc_start_time": start_time,
        "boot_id": boot_id,
    }


def _remove_exact_lease_dir(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    expected_token: str | None = None,
) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    if not path.is_dir() or path.is_symlink():
        return False
    if expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity:
        return False
    owner = _read_lock_owner(path)
    if expected_token is not None and (not isinstance(owner, dict) or owner.get("lease_token") != expected_token):
        return False
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    if {entry.name for entry in entries} - {"owner.json"}:
        return False
    try:
        (path / "owner.json").unlink(missing_ok=True)
        path.rmdir()
    except OSError:
        return False
    return True


@contextmanager
def session_lease(session_id: str) -> Iterator[None]:
    root = session_root(session_id)
    if not root.is_dir():
        raise SessionError("minion session not found")
    lock_dir = root / ".lease"
    acquired = False
    lease_token = uuid.uuid4().hex
    lease_identity: tuple[int, int] | None = None
    for _ in range(3):
        candidate = root / f".lease.candidate.{uuid.uuid4().hex}"
        candidate.mkdir(mode=0o700)
        _atomic_write_json(
            candidate / "owner.json",
            {**owner_identity(), "acquired_at": _now(), "lease_token": lease_token},
        )
        try:
            os.rename(candidate, lock_dir)
            info = lock_dir.stat()
            lease_identity = (info.st_dev, info.st_ino)
            acquired = True
            break
        except OSError:
            _remove_exact_lease_dir(candidate, expected_token=lease_token)
            owner = _read_lock_owner(lock_dir)
            if _owner_is_live(owner):
                raise SessionBusyError("minion session is already active")
            stale = root / f".lease.stale.{uuid.uuid4().hex}"
            try:
                os.replace(lock_dir, stale)
            except (FileNotFoundError, OSError):
                continue
            _remove_exact_lease_dir(stale)
    if not acquired:
        raise SessionBusyError("could not acquire minion session lease")
    try:
        yield
    finally:
        _remove_exact_lease_dir(
            lock_dir,
            expected_identity=lease_identity,
            expected_token=lease_token,
        )


def validate_resume_binding(manifest: dict[str, Any], *, workdir: Path, prime_commit: str) -> None:
    if manifest.get("state") == "CLOSED":
        raise SessionError("minion session is closed")
    if manifest.get("canonical_workdir") != str(workdir.resolve()):
        raise SessionError("minion session workdir mismatch")
    if manifest.get("prime_commit") != prime_commit:
        raise SessionError("minion session Prime runtime pin mismatch")


def _current_lease_owner(manifest: Mapping[str, Any]) -> dict[str, Any]:
    session_id = manifest.get("session_id")
    if not isinstance(session_id, str):
        raise SessionError("minion session identity is invalid")
    owner = _read_lock_owner(session_root(session_id) / ".lease")
    if not _owner_is_live(owner) or not isinstance(owner, dict) or not isinstance(owner.get("lease_token"), str):
        raise SessionBusyError("minion session has no live exact mutation lease")
    return {
        "pid": owner["pid"],
        "proc_start_time": owner["proc_start_time"],
        "boot_id": owner["boot_id"],
        "lease_token": owner["lease_token"],
    }


def _repair_stale_running_turn(manifest: dict[str, Any]) -> None:
    turns = manifest.get("turns")
    if manifest.get("state") != "RUNNING" or not isinstance(turns, list) or not turns:
        return
    previous = turns[-1]
    if not isinstance(previous, dict) or previous.get("status") != "RUNNING":
        return
    prior_owner = previous.get("lease_owner")
    if isinstance(prior_owner, Mapping) and _owner_is_live(prior_owner):
        raise SessionBusyError("previous RUNNING turn still has a live exact owner")
    previous.update(
        {
            "status": "INTERRUPTED",
            "ended_at": _now(),
            "error": "previous owner disappeared before terminal lifecycle closure",
        }
    )


def begin_turn(manifest: dict[str, Any], requested_route: dict[str, str]) -> int:
    lease_owner = _current_lease_owner(manifest)
    turns = manifest["turns"]
    _repair_stale_running_turn(manifest)
    generation = int(manifest.get("generation") or 0) + 1
    manifest["generation"] = generation
    manifest["state"] = "RUNNING"
    turns.append(
        {
            "generation": generation,
            "status": "RUNNING",
            "requested_route": dict(requested_route),
            "started_at": _now(),
            "lease_owner": lease_owner,
        }
    )
    write_manifest(manifest)
    return generation


def _active_turn(manifest: dict[str, Any]) -> dict[str, Any]:
    turns = manifest.get("turns")
    if not isinstance(turns, list) or not turns or not isinstance(turns[-1], dict):
        raise SessionError("minion session has no active turn")
    return turns[-1]


def record_completed(
    manifest: dict[str, Any],
    *,
    effective_route: dict[str, str],
    prime_session_id: str,
    session_file: Path,
) -> None:
    root = session_root(str(manifest["session_id"])).resolve()
    resolved_file = session_file.resolve()
    try:
        relative = resolved_file.relative_to(root)
    except ValueError as exc:
        raise SessionError("Prime session file escaped the managed session root") from exc
    if not resolved_file.is_file():
        raise SessionError("Prime session file was not persisted")
    turn = _active_turn(manifest)
    turn.update(
        {
            "status": "COMPLETED",
            "effective_route": dict(effective_route),
            "ended_at": _now(),
        }
    )
    manifest.update(
        {
            "state": "IDLE",
            "prime_session_id": prime_session_id,
            "session_file": relative.as_posix(),
        }
    )
    write_manifest(manifest)


def record_interrupted(manifest: dict[str, Any], error: str) -> None:
    turn = _active_turn(manifest)
    if turn.get("status") == "RUNNING":
        turn.update({"status": "INTERRUPTED", "ended_at": _now(), "error": error[-2000:]})
    manifest["state"] = "INTERRUPTED"
    write_manifest(manifest)


def resume_file(manifest: dict[str, Any]) -> Path | None:
    relative = manifest.get("session_file")
    if relative is None:
        return None
    if not isinstance(relative, str) or not relative:
        raise SessionError("minion session file reference is invalid")
    root = session_root(str(manifest["session_id"])).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SessionError("minion session file reference escaped its managed root") from exc
    if not path.is_file():
        raise SessionError("minion session transcript is missing")
    return path


def public_status(manifest: dict[str, Any]) -> dict[str, Any]:
    turns = manifest.get("turns") if isinstance(manifest.get("turns"), list) else []
    raw_turn = turns[-1] if turns and isinstance(turns[-1], dict) else None
    last_turn = None
    if raw_turn is not None:
        public_keys = (
            "generation",
            "status",
            "requested_route",
            "effective_route",
            "started_at",
            "ended_at",
        )
        last_turn = {key: raw_turn[key] for key in public_keys if key in raw_turn}
    return {
        "session_id": manifest["session_id"],
        "state": manifest["state"],
        "generation": manifest["generation"],
        "workdir": manifest["canonical_workdir"],
        "prime_commit": manifest["prime_commit"],
        "prime_session_id": manifest.get("prime_session_id"),
        "created_at": manifest["created_at"],
        "updated_at": manifest["updated_at"],
        "closed_at": manifest.get("closed_at"),
        "last_turn": last_turn,
    }


def close_manifest(manifest: dict[str, Any]) -> None:
    _current_lease_owner(manifest)
    if manifest.get("state") == "RUNNING":
        _repair_stale_running_turn(manifest)
        manifest["state"] = "INTERRUPTED"
    manifest["state"] = "CLOSED"
    manifest["closed_at"] = _now()
    write_manifest(manifest)


__all__ = [
    "SessionBusyError",
    "SessionError",
    "begin_turn",
    "close_manifest",
    "create_manifest",
    "load_manifest",
    "owner_identity",
    "public_status",
    "record_completed",
    "record_interrupted",
    "resume_file",
    "session_lease",
    "transcript_dir",
    "validate_resume_binding",
    "validate_session_id",
]
