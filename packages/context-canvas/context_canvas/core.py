from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
FACTUAL_KINDS = {"finding", "action", "decision", "blocked", "verification"}
NONFACTUAL_KINDS = {"plan", "question", "assumption"}
ALLOWED_STATUSES = {"doing", "done", "blocked", "deprecated", "verify", "planned"}
ALLOWED_KINDS = FACTUAL_KINDS | NONFACTUAL_KINDS


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
    projection and should never be edited as source of truth.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else default_root()

    def _session_dir(self, session_id: str) -> Path:
        safe = slugify(session_id)
        return self.root / safe

    def _canvas_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "canvas.json"

    def _load_canvas(self, session_id: str) -> dict[str, Any]:
        path = self._canvas_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"canvas not found: {session_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_canvas(self, session_id: str, canvas: dict[str, Any]) -> None:
        canvas["updated_at"] = now_iso()
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "canvas.json").write_text(json.dumps(canvas, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._write_state(session_id, canvas)
        self._write_mermaid(session_id, canvas)

    def _write_state(self, session_id: str, canvas: dict[str, Any]) -> None:
        refs_dir = self._session_dir(session_id) / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
        ref_count = len(list(refs_dir.glob("tc_*.md")))
        node_ids = [int(str(n.get("id", "N000"))[1:]) for n in canvas.get("nodes", []) if str(n.get("id", "")).startswith("N") and str(n.get("id", "N0"))[1:].isdigit()]
        state = {
            "session_id": session_id,
            "node_count": len(canvas.get("nodes", [])),
            "edge_count": len(canvas.get("edges", [])),
            "ref_count": ref_count,
            "next_node": (max(node_ids) + 1) if node_ids else 1,
            "next_ref": ref_count + 1,
            "updated_at": canvas.get("updated_at"),
        }
        (self._session_dir(session_id) / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
        (self._session_dir(session_id) / "canvas.mmd").write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _mmd_label(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', "'").replace("\n", "<br/>")[:260]

    def start(self, goal: str, session_id: str | None = None, title: str | None = None) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("goal is required")
        if session_id is None:
            session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slugify(goal)[:32]}"
        session_id = slugify(session_id)
        ts = now_iso()
        session_dir = self._session_dir(session_id)
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
        (session_dir / "events.jsonl").write_text("", encoding="utf-8")
        self._write_canvas(session_id, canvas)
        self._append_event(session_id, {"event": "canvas_started", "goal": goal.strip(), "title": canvas["title"]})
        return {"ok": True, "session_id": session_id, "path": str(session_dir), "canvas": canvas}

    def add_ref(self, session_id: str, content: str, label: str = "evidence", source: str = "", kind: str = "evidence") -> dict[str, Any]:
        if content is None or content == "":
            raise ValueError("content is required")
        canvas = self._load_canvas(session_id)
        refs_dir = self._session_dir(session_id) / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
        next_ref = len(list(refs_dir.glob("tc_*.md"))) + 1
        ref_name = f"tc_{next_ref:03d}.md"
        rel = f"refs/{ref_name}"
        header = [f"# {label or 'evidence'}", "", f"- kind: {kind}"]
        if source:
            header.append(f"- source: {source}")
        header.extend([f"- captured_at: {now_iso()}", "", "```text", str(content), "```", ""])
        (refs_dir / ref_name).write_text("\n".join(header), encoding="utf-8")
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
        canvas = self._load_canvas(session_id)
        out = {"ok": True, "session_id": session_id, "path": str(self._session_dir(session_id)), "canvas": canvas}
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
        for sid in sessions:
            try:
                canvas = self._load_canvas(sid)
            except FileNotFoundError:
                continue
            for node in canvas.get("nodes", []):
                hay = json.dumps(node, ensure_ascii=False).lower()
                if q in hay:
                    hits.append({"session_id": sid, "type": "node", "id": node.get("id"), "preview": node.get("summary", ""), "refs": node.get("refs", [])})
            for ref in sorted((self._session_dir(sid) / "refs").glob("tc_*.md")):
                text = ref.read_text(encoding="utf-8", errors="replace")
                if q in text.lower():
                    line = next((ln.strip() for ln in text.splitlines() if q in ln.lower()), text[:200])
                    hits.append({"session_id": sid, "type": "ref", "id": f"refs/{ref.name}", "preview": line[:300]})
        return {"ok": True, "query": query, "hits": hits[: max(1, int(limit))]}

    def closeout(self, session_id: str, write_ref: bool = True) -> dict[str, Any]:
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
            path.write_text(text, encoding="utf-8")
            export_path = str(path)
            self._append_event(session_id, {"event": "closeout_written", "path": export_path})
        return {"ok": True, "session_id": session_id, "export_path": export_path, "closeout": text}
