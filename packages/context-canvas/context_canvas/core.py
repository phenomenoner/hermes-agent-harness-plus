from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # Linux/WSL cross-process locking; the thread lock remains the fallback.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
FACTUAL_KINDS = {"finding", "action", "decision", "blocked", "verification"}
NONFACTUAL_KINDS = {"plan", "question", "assumption"}
ALLOWED_STATUSES = {"doing", "done", "blocked", "deprecated", "verify", "planned"}
ALLOWED_KINDS = FACTUAL_KINDS | NONFACTUAL_KINDS

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_root() -> Path:
    return Path(os.getenv("HERMES_CONTEXT_CANVAS_HOME", Path.home() / ".hermes" / "context-canvas")).expanduser()


def slugify(value: str) -> str:
    cleaned = SAFE_ID_RE.sub("-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned[:80] or "canvas"


class CanvasStore:
    """Local filesystem-backed canonical Task Canvas store.

    Canonical state is JSON/JSONL. Mermaid is always regenerated as a derived
    projection and should never be edited as source of truth. Cross-process
    mutation safety uses POSIX ``flock`` and is therefore supported on Linux
    and WSL; non-POSIX hosts retain thread safety only.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else default_root()

    def _session_dir(self, session_id: str) -> Path:
        safe = slugify(session_id)
        return self.root / safe

    def _canvas_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "canvas.json"

    @contextmanager
    def _session_lock(self, session_id: str) -> Iterator[None]:
        """Serialize one canvas across threads and, on POSIX, processes."""
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        thread_lock = _thread_lock_for(session_dir)
        with thread_lock:
            lock_path = session_dir / ".lock"
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """Replace a text file atomically so readers never observe torn JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
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
        for field in ("nodes", "edges"):
            value = canvas.setdefault(field, [])
            if not isinstance(value, list):
                raise ValueError(f"canvas {field} must be a list: {session_id}")
            if any(not isinstance(item, dict) for item in value):
                raise ValueError(f"canvas {field} entries must be objects: {session_id}")
        for index, node in enumerate(canvas["nodes"]):
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id:
                raise ValueError(f"canvas node {index} requires a string id: {session_id}")
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
        return canvas

    def _load_canvas(self, session_id: str) -> dict[str, Any]:
        path = self._canvas_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"canvas not found: {session_id}")
        canvas = json.loads(path.read_text(encoding="utf-8"))
        return self._validate_canvas(canvas, session_id)

    def _write_canvas(self, session_id: str, canvas: dict[str, Any]) -> None:
        canvas["updated_at"] = now_iso()
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_text(
            session_dir / "canvas.json",
            json.dumps(canvas, ensure_ascii=False, indent=2) + "\n",
        )
        self._write_state(session_id, canvas)
        self._write_mermaid(session_id, canvas)

    def _write_state(self, session_id: str, canvas: dict[str, Any]) -> None:
        refs_dir = self._session_dir(session_id) / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
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
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _write_mermaid(self, session_id: str, canvas: dict[str, Any]) -> None:
        lines = ["graph TD"]
        if not canvas.get("nodes"):
            lines.append(f"  ROOT[\"goal: {self._mmd_label(canvas.get('goal', ''))}\"]")
        for node in canvas.get("nodes", []):
            refs = ",".join(Path(r).stem for r in node.get("refs", [])[:3]) or "no-ref"
            label = f"{node.get('kind')}<br/>status: {node.get('status')}<br/>{node.get('summary', '')}<br/>ref: {refs}"
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
        return str(value).replace("\\", "\\\\").replace('"', "'").replace("\n", "<br/>")[:260]

    def start(self, goal: str, session_id: str | None = None, title: str | None = None) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("goal is required")
        if session_id is None:
            generated_at = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            session_id = f"{generated_at}_{uuid.uuid4().hex[:8]}_{slugify(goal)[:24]}"
        session_id = slugify(session_id)
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
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "refs").mkdir(exist_ok=True)
            canvas = {
                "version": 1,
                "session_id": session_id,
                "goal": goal.strip(),
                "title": (title or goal).strip(),
                "created_at": ts,
                "updated_at": ts,
                "nodes": [],
                "edges": [],
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

    def add_ref(self, session_id: str, content: str, label: str = "evidence", source: str = "", kind: str = "evidence") -> dict[str, Any]:
        if content is None or content == "":
            raise ValueError("content is required")
        with self._session_lock(session_id):
            canvas = self._load_canvas(session_id)
            refs_dir = self._session_dir(session_id) / "refs"
            refs_dir.mkdir(parents=True, exist_ok=True)
            used_refs = [
                int(path.stem.removeprefix("tc_"))
                for path in refs_dir.glob("tc_*.md")
                if path.stem.removeprefix("tc_").isdigit()
            ]
            next_ref = (max(used_refs) + 1) if used_refs else 1
            ref_name = f"tc_{next_ref:03d}.md"
            rel = f"refs/{ref_name}"
            header = [f"# {label or 'evidence'}", "", f"- kind: {kind}"]
            if source:
                header.append(f"- source: {source}")
            header.extend([f"- captured_at: {now_iso()}", "", "```text", str(content), "```", ""])
            self._atomic_write_text(refs_dir / ref_name, "\n".join(header))
            self._append_event(session_id, {"event": "ref_added", "ref": rel, "label": label, "source": source, "kind": kind})
            self._write_canvas(session_id, canvas)
            return {"ok": True, "session_id": session_id, "ref": rel, "path": str(self._session_dir(session_id) / rel)}

    def upsert_node(
        self,
        session_id: str,
        *,
        kind: str,
        status: str,
        summary: str,
        refs: list[str] | None = None,
        depends_on: list[str] | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        kind = kind.strip()
        status = status.strip()
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"invalid kind: {kind}")
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"invalid status: {status}")
        if not summary.strip():
            raise ValueError("summary is required")
        refs = refs or []
        depends_on = depends_on or []
        if kind in FACTUAL_KINDS and status in {"done", "blocked", "deprecated", "verify"} and not refs:
            raise ValueError("factual node with this status requires at least one evidence ref")
        with self._session_lock(session_id):
            canvas = self._load_canvas(session_id)
            nodes = canvas.setdefault("nodes", [])
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

    def read(self, session_id: str, include_refs: bool = False) -> dict[str, Any]:
        with self._session_lock(session_id):
            canvas = self._load_canvas(session_id)
            out = {
                "ok": True,
                "session_id": session_id,
                "path": str(self._session_dir(session_id)),
                "canvas": canvas,
            }
            out["mermaid"] = (self._session_dir(session_id) / "canvas.mmd").read_text(encoding="utf-8")
            if include_refs:
                refs = {}
                for path in sorted((self._session_dir(session_id) / "refs").glob("tc_*.md")):
                    refs[f"refs/{path.name}"] = path.read_text(encoding="utf-8")
                out["refs"] = refs
            return out

    def search(self, query: str, session_id: str | None = None, limit: int = 10) -> dict[str, Any]:
        q = query.strip().lower()
        if not q:
            raise ValueError("query is required")
        sessions = [slugify(session_id)] if session_id else [p.name for p in sorted(self.root.iterdir()) if p.is_dir()] if self.root.exists() else []
        hits: list[dict[str, Any]] = []
        skipped_sessions: list[dict[str, str]] = []
        for sid in sessions:
            canvas: dict[str, Any] = {}
            try:
                canvas = self._load_canvas(sid)
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError, ValueError) as exc:
                skipped_sessions.append(
                    {
                        "session_id": sid,
                        "error": type(exc).__name__,
                        "detail": str(exc),
                    }
                )
            for node in canvas.get("nodes", []):
                hay = json.dumps(node, ensure_ascii=False).lower()
                if q in hay:
                    hits.append({"session_id": sid, "type": "node", "id": node.get("id"), "preview": node.get("summary", ""), "refs": node.get("refs", [])})
            for ref in sorted((self._session_dir(sid) / "refs").glob("tc_*.md")):
                text = ref.read_text(encoding="utf-8", errors="replace")
                if q in text.lower():
                    line = next((ln.strip() for ln in text.splitlines() if q in ln.lower()), text[:200])
                    hits.append({"session_id": sid, "type": "ref", "id": f"refs/{ref.name}", "preview": line[:300]})
        return {
            "ok": True,
            "query": query,
            "hits": hits[: max(1, int(limit))],
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
            (self._session_dir(session_id) / "canvas.mmd").read_text(encoding="utf-8").strip(),
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
