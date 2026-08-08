from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, TypeVar, cast

try:  # Linux/WSL cross-process locking; the thread lock remains the fallback.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
NodeKind = Literal[
    "action",
    "assumption",
    "blocked",
    "decision",
    "finding",
    "gap",
    "plan",
    "question",
    "verification",
]
NodeStatus = Literal["blocked", "deprecated", "doing", "done", "planned", "verify"]

FACTUAL_KINDS = {"finding", "action", "decision", "blocked", "gap", "verification"}
NONFACTUAL_KINDS = {"plan", "question", "assumption"}
ALLOWED_STATUSES = {"doing", "done", "blocked", "deprecated", "verify", "planned"}
ALLOWED_KINDS = FACTUAL_KINDS | NONFACTUAL_KINDS

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_ChoiceT = TypeVar("_ChoiceT", bound=str)


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_root() -> Path:
    explicit = os.getenv("HERMES_CONTEXT_CANVAS_HOME")
    if explicit:
        return Path(explicit).expanduser()
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        hermes_home = Path(get_hermes_home())
    except Exception:
        hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home.expanduser() / "context-canvas"


def slugify(value: str) -> str:
    cleaned = SAFE_ID_RE.sub("-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned[:80] or "canvas"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"controlled path is not a directory: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"controlled directory has unexpected owner: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700, follow_symlinks=False)


def _regular_owned(path: Path, *, tighten: bool = False) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"controlled path is not a regular file: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"controlled file has unexpected owner: {path}")
    if tighten and stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o600, follow_symlinks=False)
        info = path.lstat()
    return info


def _bounded_header(value: str, *, limit: int) -> str:
    return " ".join(str(value).replace("\x00", "").splitlines()).strip()[:limit]


def _quoted_content(value: str) -> str:
    return str(value).replace("\x00", "\\0").replace("```", "`\u200b``")


def _validated_choice(value: _ChoiceT, *, field: str, allowed: set[str]) -> _ChoiceT:
    normalized = str(value).strip()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"invalid {field}: {normalized}; allowed: {choices}")
    return cast(_ChoiceT, normalized)


class CanvasStore:
    """Local filesystem-backed canonical Task Canvas store.

    Canonical state is JSON/JSONL. Mermaid is always regenerated as a derived
    projection and should never be edited as source of truth. Cross-process
    mutation safety uses POSIX ``flock`` and is therefore supported on Linux
    and WSL; non-POSIX hosts retain thread safety only.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else default_root()
        if self.root.exists() or self.root.is_symlink():
            _ensure_private_dir(self.root)

    def _session_dir(self, session_id: str) -> Path:
        safe = slugify(session_id)
        if str(session_id) != safe:
            raise ValueError("session_id must already be a safe, collision-free storage id")
        return self.root / safe

    def _canvas_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "canvas.json"

    def _read_controlled_text(self, path: Path, *, errors: str = "strict") -> str:
        _regular_owned(path)
        root = self.root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PermissionError(f"controlled path escapes canvas root: {path}") from exc
        return resolved.read_text(encoding="utf-8", errors=errors)

    def _safe_ref_path(self, session_id: str, ref: str) -> Path:
        if not re.fullmatch(r"refs/tc_[0-9]+\.md", str(ref)):
            raise ValueError(f"invalid evidence ref: {ref}")
        session_dir = self._session_dir(session_id)
        session_info = session_dir.lstat()
        if not stat.S_ISDIR(session_info.st_mode):
            raise RuntimeError(f"canvas session path is not a directory: {session_dir}")
        refs_dir = session_dir / "refs"
        refs_info = refs_dir.lstat()
        if not stat.S_ISDIR(refs_info.st_mode):
            raise RuntimeError(f"canvas refs path is not a directory: {refs_dir}")
        path = session_dir / ref
        _regular_owned(path)
        refs_root = refs_dir.resolve(strict=True)
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(refs_root)
        except ValueError as exc:
            raise PermissionError(f"evidence ref escapes its canvas: {ref}") from exc
        return resolved

    @contextmanager
    def _session_lock(self, session_id: str) -> Iterator[None]:
        """Serialize one canvas across threads and, on POSIX, processes."""
        session_dir = self._session_dir(session_id)
        _ensure_private_dir(self.root)
        _ensure_private_dir(session_dir)
        thread_lock = _thread_lock_for(session_dir)
        with thread_lock:
            lock_path = session_dir / ".lock"
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(lock_path, flags, 0o600)
            try:
                _regular_owned(lock_path, tighten=True)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """Replace a text file atomically so readers never observe torn JSON."""
        _ensure_private_dir(path.parent)
        if path.exists() or path.is_symlink():
            _regular_owned(path, tighten=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
            _regular_owned(path, tighten=True)
            if os.name == "posix":
                dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _validate_canvas(canvas: Any, session_id: str) -> dict[str, Any]:
        if not isinstance(canvas, dict):
            raise ValueError(f"canvas must be a JSON object: {session_id}")
        if canvas.get("version") != 1:
            raise ValueError(f"unsupported canvas version: {session_id}")
        if canvas.get("session_id") != session_id:
            raise ValueError(f"canvas session identity mismatch: {session_id}")
        for field in ("nodes", "edges"):
            value = canvas.setdefault(field, [])
            if not isinstance(value, list):
                raise ValueError(f"canvas {field} must be a list: {session_id}")
            if any(not isinstance(item, dict) for item in value):
                raise ValueError(f"canvas {field} entries must be objects: {session_id}")
        node_ids: set[str] = set()
        for index, node in enumerate(canvas["nodes"]):
            node_id = node.get("id")
            if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", node_id):
                raise ValueError(f"canvas node {index} requires a string id: {session_id}")
            if node_id in node_ids:
                raise ValueError(f"duplicate canvas node id {node_id}: {session_id}")
            node_ids.add(node_id)
            for field in ("refs", "depends_on"):
                value = node.setdefault(field, [])
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError(
                        f"canvas node {node_id} {field} must be a list of strings: {session_id}"
                    )
        for index, edge in enumerate(canvas["edges"]):
            for field in ("from", "to"):
                value = edge.get(field)
                if value is not None and not isinstance(value, str):
                    raise ValueError(
                        f"canvas edge {index} {field} must be a string when present: {session_id}"
                    )
        for node in canvas["nodes"]:
            missing = [dep for dep in node.get("depends_on", []) if dep not in node_ids]
            if missing:
                raise ValueError(f"canvas node {node['id']} has missing dependencies: {session_id}")
        for index, edge in enumerate(canvas["edges"]):
            if edge.get("from") not in node_ids or edge.get("to") not in node_ids:
                raise ValueError(f"canvas edge {index} references a missing node: {session_id}")
        metadata = canvas.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError(f"canvas metadata must be an object: {session_id}")
        return canvas

    def _load_canvas(self, session_id: str) -> dict[str, Any]:
        path = self._canvas_path(session_id)
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError(f"canvas not found: {session_id}")
        canvas = json.loads(self._read_controlled_text(path))
        validated = self._validate_canvas(canvas, session_id)
        for node in validated.get("nodes", []):
            for ref in node.get("refs", []):
                self._safe_ref_path(session_id, ref)
        return validated

    def _write_canvas(self, session_id: str, canvas: dict[str, Any]) -> None:
        canvas["updated_at"] = now_iso()
        session_dir = self._session_dir(session_id)
        _ensure_private_dir(session_dir)
        self._atomic_write_text(
            session_dir / "canvas.json",
            json.dumps(canvas, ensure_ascii=False, indent=2) + "\n",
        )
        self._write_state(session_id, canvas)
        self._write_mermaid(session_id, canvas)

    def _write_state(self, session_id: str, canvas: dict[str, Any]) -> None:
        refs_dir = self._session_dir(session_id) / "refs"
        _ensure_private_dir(refs_dir)
        ref_ids = [
            int(path.stem.removeprefix("tc_"))
            for path in refs_dir.glob("tc_*.md")
            if path.stem.removeprefix("tc_").isdigit()
        ]
        node_ids = [int(str(n.get("id", "N000"))[1:]) for n in canvas.get("nodes", []) if str(n.get("id", "")).startswith("N") and str(n.get("id", "N0"))[1:].isdigit()]
        state = {
            "session_id": session_id,
            "node_count": len(canvas.get("nodes", [])),
            "edge_count": len(canvas.get("edges", [])),
            "ref_count": len(ref_ids),
            "next_node": (max(node_ids) + 1) if node_ids else 1,
            "next_ref": (max(ref_ids) + 1) if ref_ids else 1,
            "updated_at": canvas.get("updated_at"),
        }
        self._atomic_write_text(
            self._session_dir(session_id) / "state.json",
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        )

    def _append_event(self, session_id: str, event: dict[str, Any]) -> None:
        event = {"ts": now_iso(), **event}
        path = self._session_dir(session_id) / "events.jsonl"
        _ensure_private_dir(path.parent)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            _regular_owned(path, tighten=True)
            raw = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write while appending canvas event")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_mermaid(self, session_id: str, canvas: dict[str, Any]) -> None:
        lines = ["graph TD"]
        if not canvas.get("nodes"):
            lines.append(f"  ROOT[\"goal: {self._mmd_label(canvas.get('goal', ''))}\"]")
        for node in canvas.get("nodes", []):
            refs = ",".join(Path(r).stem for r in node.get("refs", [])[:3]) or "no-ref"
            summary = str(node.get("summary", ""))[:170]
            # Keep evidence IDs ahead of the bounded summary so Mermaid label
            # truncation can never hide the map -> evidence hop.
            label = f"{node.get('kind')}<br/>status: {node.get('status')}<br/>ref: {refs}<br/>{summary}"
            lines.append(f"  {node['id']}[\"{self._mmd_label(label)}\"]")
        edge_set: set[tuple[str, str]] = set()
        for edge in canvas.get("edges", []):
            src, dst = edge.get("from"), edge.get("to")
            if src and dst:
                edge_set.add((src, dst))
        for node in canvas.get("nodes", []):
            for dep in node.get("depends_on", []):
                edge_set.add((dep, node["id"]))
        for src, dst in sorted(edge_set):
            lines.append(f"  {src} --> {dst}")
        self._atomic_write_text(
            self._session_dir(session_id) / "canvas.mmd",
            "\n".join(lines) + "\n",
        )

    @staticmethod
    def _mmd_label(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', "'").replace("\n", "<br/>")[:320]

    def start(
        self,
        goal: str,
        session_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("goal is required")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        if session_id is None:
            generated_at = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            session_id = f"{generated_at}_{uuid.uuid4().hex[:8]}_{slugify(goal)[:24]}"
        else:
            requested = str(session_id)
            if requested != slugify(requested):
                raise ValueError("session_id must already be a safe, collision-free storage id")
            session_id = requested
        with self._session_lock(session_id):
            session_dir = self._session_dir(session_id)
            canvas_path = session_dir / "canvas.json"
            if canvas_path.exists():
                canvas = self._load_canvas(session_id)
                return {
                    "ok": True,
                    "created": False,
                    "session_id": session_id,
                    "path": str(session_dir),
                    "canvas": canvas,
                }
            ts = now_iso()
            _ensure_private_dir(session_dir)
            _ensure_private_dir(session_dir / "refs")
            canvas = {
                "version": 1,
                "session_id": session_id,
                "goal": goal.strip(),
                "title": (title or goal).strip(),
                "created_at": ts,
                "updated_at": ts,
                "nodes": [],
                "edges": [],
                "metadata": dict(metadata or {}),
            }
            self._atomic_write_text(session_dir / "events.jsonl", "")
            self._write_canvas(session_id, canvas)
            self._append_event(session_id, {"event": "canvas_started", "goal": goal.strip(), "title": canvas["title"]})
            return {
                "ok": True,
                "created": True,
                "session_id": session_id,
                "path": str(session_dir),
                "canvas": canvas,
            }

    def update_metadata(self, session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Atomically merge bounded lifecycle/source metadata into a canvas."""
        if not isinstance(updates, dict):
            raise ValueError("updates must be an object")
        with self._session_lock(session_id):
            canvas = self._load_canvas(session_id)
            metadata = canvas.setdefault("metadata", {})
            metadata.update(updates)
            self._write_canvas(session_id, canvas)
            self._append_event(
                session_id,
                {"event": "metadata_updated", "keys": sorted(str(key) for key in updates)},
            )
            return {"ok": True, "session_id": session_id, "metadata": metadata}

    def add_ref(self, session_id: str, content: str, label: str = "evidence", source: str = "", kind: str = "evidence") -> dict[str, Any]:
        if content is None or content == "":
            raise ValueError("content is required")
        with self._session_lock(session_id):
            canvas = self._load_canvas(session_id)
            refs_dir = self._session_dir(session_id) / "refs"
            _ensure_private_dir(refs_dir)
            used_refs = [
                int(path.stem.removeprefix("tc_"))
                for path in refs_dir.glob("tc_*.md")
                if path.stem.removeprefix("tc_").isdigit()
            ]
            next_ref = (max(used_refs) + 1) if used_refs else 1
            ref_name = f"tc_{next_ref:03d}.md"
            rel = f"refs/{ref_name}"
            safe_label = _bounded_header(label or "evidence", limit=200)
            safe_kind = _bounded_header(kind or "evidence", limit=80)
            safe_source = _bounded_header(source, limit=500)
            header = [f"# {safe_label}", "", f"- kind: {safe_kind}"]
            if safe_source:
                header.append(f"- source: {safe_source}")
            header.extend([f"- captured_at: {now_iso()}", "", "```text", _quoted_content(content), "```", ""])
            self._atomic_write_text(refs_dir / ref_name, "\n".join(header))
            self._append_event(session_id, {"event": "ref_added", "ref": rel, "label": safe_label, "source": safe_source, "kind": safe_kind})
            self._write_canvas(session_id, canvas)
            return {"ok": True, "session_id": session_id, "ref": rel, "path": str(self._session_dir(session_id) / rel)}

    def upsert_node(
        self,
        session_id: str,
        *,
        kind: NodeKind,
        status: NodeStatus,
        summary: str,
        refs: list[str] | None = None,
        depends_on: list[str] | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        kind = _validated_choice(kind, field="kind", allowed=ALLOWED_KINDS)
        status = _validated_choice(status, field="status", allowed=ALLOWED_STATUSES)
        if not summary.strip():
            raise ValueError("summary is required")
        if len(summary) > 4000:
            raise ValueError("summary exceeds 4000 characters")
        if node_id is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", node_id):
            raise ValueError("invalid node_id")
        refs = refs or []
        depends_on = depends_on or []
        if kind in FACTUAL_KINDS and status in {"done", "blocked", "deprecated", "verify"} and not refs:
            raise ValueError("factual node with this status requires at least one evidence ref")
        with self._session_lock(session_id):
            canvas = self._load_canvas(session_id)
            nodes = canvas.setdefault("nodes", [])
            for ref in refs:
                self._safe_ref_path(session_id, ref)
            known_ids = {str(node.get("id")) for node in nodes}
            missing_deps = [dep for dep in depends_on if dep not in known_ids]
            if missing_deps:
                raise ValueError("depends_on contains a missing node")
            existing = None
            if node_id:
                existing = next((n for n in nodes if n.get("id") == node_id), None)
            else:
                used = [int(str(n.get("id", "N000"))[1:]) for n in nodes if str(n.get("id", "")).startswith("N") and str(n.get("id", "N0"))[1:].isdigit()]
                node_id = f"N{(max(used) + 1) if used else 1:03d}"
            node = {
                "id": node_id,
                "kind": kind,
                "status": status,
                "summary": summary.strip(),
                "refs": refs,
                "depends_on": depends_on,
                "updated_at": now_iso(),
            }
            if existing is None:
                nodes.append(node)
                event = "node_added"
            else:
                existing.update(node)
                node = existing
                event = "node_updated"
            self._write_canvas(session_id, canvas)
            self._append_event(session_id, {"event": event, "node_id": node_id, "kind": kind, "status": status, "refs": refs})
            return {"ok": True, "session_id": session_id, "node": node}

    def record_evidence_node(
        self,
        session_id: str,
        *,
        content: str,
        label: str,
        source: str,
        ref_kind: str,
        node_kind: NodeKind,
        node_status: NodeStatus,
        node_summary: str,
        node_id: str | None = None,
        depends_on: list[str] | None = None,
        max_refs: int = 12,
    ) -> dict[str, Any]:
        """Write a small ref and aggregate node under one session lock.

        The ref is written before the canonical node, so readers never observe
        a node pointing at a missing file. If the canvas update fails before it
        commits, the newly allocated ref is removed while the lock is held.
        """
        if not content:
            raise ValueError("content is required")
        node_kind = _validated_choice(node_kind, field="kind", allowed=ALLOWED_KINDS)
        node_status = _validated_choice(node_status, field="status", allowed=ALLOWED_STATUSES)
        if not node_summary.strip():
            raise ValueError("node_summary is required")
        if len(node_summary) > 4000:
            raise ValueError("node_summary exceeds 4000 characters")
        if node_id is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", node_id):
            raise ValueError("invalid node_id")
        if max_refs < 1:
            raise ValueError("max_refs must be positive")
        depends_on = depends_on or []
        with self._session_lock(session_id):
            canvas = self._load_canvas(session_id)
            known_ids = {str(node.get("id")) for node in canvas.get("nodes", [])}
            missing_deps = [dep for dep in depends_on if dep not in known_ids]
            if missing_deps:
                raise ValueError("depends_on contains a missing node")
            refs_dir = self._session_dir(session_id) / "refs"
            _ensure_private_dir(refs_dir)
            used_refs = [
                int(path.stem.removeprefix("tc_"))
                for path in refs_dir.glob("tc_*.md")
                if path.stem.removeprefix("tc_").isdigit()
            ]
            next_ref = (max(used_refs) + 1) if used_refs else 1
            ref_name = f"tc_{next_ref:03d}.md"
            rel = f"refs/{ref_name}"
            ref_path = refs_dir / ref_name
            safe_label = _bounded_header(label or "evidence", limit=200)
            safe_kind = _bounded_header(ref_kind or "evidence", limit=80)
            safe_source = _bounded_header(source, limit=500)
            header = [f"# {safe_label}", "", f"- kind: {safe_kind}"]
            if safe_source:
                header.append(f"- source: {safe_source}")
            header.extend([f"- captured_at: {now_iso()}", "", "```text", _quoted_content(content), "```", ""])
            self._atomic_write_text(ref_path, "\n".join(header))
            try:
                nodes = canvas.setdefault("nodes", [])
                existing = next((n for n in nodes if n.get("id") == node_id), None) if node_id else None
                if node_id is None:
                    used = [
                        int(str(n.get("id", "N000"))[1:])
                        for n in nodes
                        if str(n.get("id", "")).startswith("N")
                        and str(n.get("id", "N0"))[1:].isdigit()
                    ]
                    node_id = f"N{(max(used) + 1) if used else 1:03d}"
                previous_refs = list(existing.get("refs", [])) if existing else []
                merged_refs = list(dict.fromkeys([*previous_refs, rel]))[-max_refs:]
                previous_deps = list(existing.get("depends_on", [])) if existing else []
                merged_deps = list(dict.fromkeys([*previous_deps, *depends_on]))
                node = {
                    "id": node_id,
                    "kind": node_kind,
                    "status": node_status,
                    "summary": node_summary.strip(),
                    "refs": merged_refs,
                    "depends_on": merged_deps,
                    "updated_at": now_iso(),
                }
                if existing is None:
                    nodes.append(node)
                    event = "node_added"
                else:
                    existing.update(node)
                    node = existing
                    event = "node_updated"
                self._write_canvas(session_id, canvas)
            except Exception:
                try:
                    ref_path.unlink()
                except FileNotFoundError:
                    pass
                raise
            self._append_event(
                session_id,
                {"event": "ref_added", "ref": rel, "label": safe_label, "source": safe_source, "kind": safe_kind},
            )
            self._append_event(
                session_id,
                {
                    "event": event,
                    "node_id": node_id,
                    "kind": node_kind,
                    "status": node_status,
                    "refs": merged_refs,
                },
            )
            return {
                "ok": True,
                "session_id": session_id,
                "ref": rel,
                "path": str(ref_path),
                "node": node,
            }

    def read(self, session_id: str, include_refs: bool = False) -> dict[str, Any]:
        with self._session_lock(session_id):
            canvas = self._load_canvas(session_id)
            out = {
                "ok": True,
                "session_id": session_id,
                "path": str(self._session_dir(session_id)),
                "canvas": canvas,
            }
            out["mermaid"] = self._read_controlled_text(self._session_dir(session_id) / "canvas.mmd")
            if include_refs:
                refs = {}
                for path in sorted((self._session_dir(session_id) / "refs").glob("tc_*.md")):
                    rel = f"refs/{path.name}"
                    safe_path = self._safe_ref_path(session_id, rel)
                    refs[rel] = self._read_controlled_text(safe_path)
                out["refs"] = refs
            return out

    def recent(self, query: str | None = None, limit: int = 10) -> dict[str, Any]:
        """List recent canvases so callers can recover a lost session id.

        The optional query matches session id, title, or goal. Corrupt sessions
        are reported but never prevent discovery of healthy canvases.
        """
        q = str(query or "").strip().lower()
        hit_limit = min(100, max(1, int(limit)))
        sessions: list[dict[str, Any]] = []
        skipped_sessions: list[dict[str, str]] = []
        if self.root.exists():
            for path in self.root.iterdir():
                try:
                    info = path.lstat()
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    canvas_path = path / "canvas.json"
                    try:
                        canvas_info = canvas_path.lstat()
                    except FileNotFoundError:
                        # Session locking can leave an empty/lock-only directory
                        # after a lookup miss; it is not a canvas to recover.
                        continue
                    if not stat.S_ISREG(canvas_info.st_mode):
                        raise RuntimeError(f"canvas path is not a regular file: {canvas_path}")
                    canvas = self._load_canvas(path.name)
                    identity = " ".join(
                        str(canvas.get(field, ""))
                        for field in ("session_id", "title", "goal")
                    ).lower()
                    if q and q not in identity:
                        continue
                    refs_dir = path / "refs"
                    ref_count = 0
                    if refs_dir.exists():
                        for ref_path in refs_dir.glob("tc_*.md"):
                            try:
                                if stat.S_ISREG(ref_path.lstat().st_mode):
                                    ref_count += 1
                            except OSError:
                                continue
                    sessions.append(
                        {
                            "session_id": canvas["session_id"],
                            "title": canvas.get("title", ""),
                            "goal": canvas.get("goal", ""),
                            "updated_at": canvas.get("updated_at", ""),
                            "node_count": len(canvas.get("nodes", [])),
                            "ref_count": ref_count,
                        }
                    )
                except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                    skipped_sessions.append(
                        {
                            "session_id": path.name,
                            "error": type(exc).__name__,
                            "detail": str(exc)[:300],
                        }
                    )

        def updated_timestamp(row: dict[str, Any]) -> float:
            try:
                return datetime.fromisoformat(str(row.get("updated_at", ""))).timestamp()
            except (TypeError, ValueError):
                return 0.0

        sessions.sort(
            key=lambda row: (updated_timestamp(row), str(row.get("session_id", ""))),
            reverse=True,
        )
        return {
            "ok": True,
            "query": query or "",
            "sessions": sessions[:hit_limit],
            "skipped_count": len(skipped_sessions),
            "skipped_sessions": skipped_sessions,
        }

    def search(self, query: str, session_id: str | None = None, limit: int = 10) -> dict[str, Any]:
        q = query.strip().lower()
        if not q:
            raise ValueError("query is required")
        hit_limit = max(1, int(limit))
        if session_id:
            sessions = [session_id]
        elif self.root.exists():
            sessions = []
            for path in sorted(self.root.iterdir()):
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    sessions.append(path.name)
        else:
            sessions = []
        hits: list[dict[str, Any]] = []
        skipped_sessions: list[dict[str, str]] = []
        for sid in sessions:
            try:
                canvas = self._load_canvas(sid)
                for node in canvas.get("nodes", []):
                    hay = json.dumps(node, ensure_ascii=False).lower()
                    if q in hay:
                        hits.append({"session_id": sid, "type": "node", "id": node.get("id"), "preview": node.get("summary", ""), "refs": node.get("refs", [])})
                        if len(hits) >= hit_limit:
                            break
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                skipped_sessions.append(
                    {
                        "session_id": sid,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:300],
                    }
                )
            if len(hits) < hit_limit:
                try:
                    for ref in sorted((self._session_dir(sid) / "refs").glob("tc_*.md")):
                        rel = f"refs/{ref.name}"
                        safe_ref = self._safe_ref_path(sid, rel)
                        text = self._read_controlled_text(safe_ref, errors="replace")
                        if q in text.lower():
                            line = next((ln.strip() for ln in text.splitlines() if q in ln.lower()), text[:200])
                            hits.append({"session_id": sid, "type": "ref", "id": rel, "preview": line[:300]})
                            if len(hits) >= hit_limit:
                                break
                except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                    skipped_sessions.append(
                        {
                            "session_id": sid,
                            "error": type(exc).__name__,
                            "detail": str(exc)[:300],
                        }
                    )
            if len(hits) >= hit_limit:
                break
        return {
            "ok": True,
            "query": query,
            "hits": hits[:hit_limit],
            "skipped_count": len(skipped_sessions),
            "skipped_sessions": skipped_sessions,
        }

    def closeout(self, session_id: str, write_ref: bool = True) -> dict[str, Any]:
        with self._session_lock(session_id):
            return self._closeout_locked(session_id, write_ref)

    def _closeout_locked(self, session_id: str, write_ref: bool) -> dict[str, Any]:
        canvas = self._load_canvas(session_id)
        lines = [
            f"# Task Canvas Closeout: {canvas.get('title')}",
            "",
            f"- session_id: {session_id}",
            f"- goal: {canvas.get('goal')}",
            f"- updated_at: {canvas.get('updated_at')}",
            "",
            "## MemPalace-ready project/context candidates",
            "",
        ]
        durable = [n for n in canvas.get("nodes", []) if n.get("kind") in {"decision", "finding", "verification"} and n.get("status") in {"done", "verify"}]
        if durable:
            for node in durable:
                lines.append(f"- {node['id']} ({node['kind']}/{node['status']}): {node['summary']} refs={', '.join(node.get('refs', []))}")
        else:
            lines.append("- None identified.")
        lines.extend(["", "## Skill / procedure candidates", ""])
        for node in [n for n in canvas.get("nodes", []) if n.get("kind") == "action" and n.get("status") == "done"] or []:
            lines.append(f"- {node['id']}: {node['summary']} refs={', '.join(node.get('refs', []))}")
        if not [n for n in canvas.get("nodes", []) if n.get("kind") == "action" and n.get("status") == "done"]:
            lines.append("- None identified.")
        lines.extend(["", "## Active blockers / follow-up", ""])
        blockers = [n for n in canvas.get("nodes", []) if n.get("status") in {"blocked", "planned", "verify"}]
        if blockers:
            for node in blockers:
                lines.append(f"- {node['id']} ({node['kind']}/{node['status']}): {node['summary']} refs={', '.join(node.get('refs', []))}")
        else:
            lines.append("- None identified.")
        lines.extend([
            "",
            "## Qdrant/file search hints",
            "",
            f"- Local canvas substring search: `canvas_search(query, session_id=\"{session_id}\")` or `context-canvas search <query> --session-id {session_id}`.",
            "- Broader semantic recall: use `mcp_qdrant_qdrant_search_all` over Hermes sessions / skills / project code, then pin high-signal hits back with `canvas_add_ref`.",
            f"- Raw evidence files live under `{self._session_dir(session_id) / 'refs'}` and can be inspected directly if a summary seems stale.",
            "",
            "## Mermaid projection",
            "",
            "```mermaid",
            self._read_controlled_text(self._session_dir(session_id) / "canvas.mmd").strip(),
            "```",
            "",
        ])
        text = "\n".join(lines)
        export_path = None
        if write_ref:
            path = self._session_dir(session_id) / "closeout.md"
            self._atomic_write_text(path, text)
            export_path = str(path)
            self._append_event(session_id, {"event": "closeout_written", "path": export_path})
        return {"ok": True, "session_id": session_id, "export_path": export_path, "closeout": text}
