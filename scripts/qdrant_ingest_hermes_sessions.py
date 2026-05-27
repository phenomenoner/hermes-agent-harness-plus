#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastembed>=0.8.0",
# ]
# ///
"""Ingest recent Hermes session conversations into local Qdrant.

Privacy/safety defaults:
- Indexes only user + assistant text messages.
- Skips system prompts, tool schemas, and tool outputs by default.
- Redacts common API-key/token/secret patterns.
- Uses recent session_*.json files only.

Default collection: hermes_sessions_recent_multilingual_v1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from fastembed import TextEmbedding

DEFAULT_URL = "http://127.0.0.1:6333"
DEFAULT_COLLECTION = "hermes_sessions_recent_multilingual_v1"
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
VECTOR_SIZE = 384
DISTANCE = "Cosine"

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)(\s*[:=]\s*|\s+)[^\s,;\]}]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{20,}"),
]
LONG_BLOB_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{120,}\b")


@dataclass
class Chunk:
    point_id: str
    text: str
    payload: dict[str, Any]


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) + "=<REDACTED>") if m.lastindex else "<REDACTED_SECRET>", text)
    text = LONG_BLOB_RE.sub("<REDACTED_LONG_BLOB>", text)
    return text


def http_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 180) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach {url}: {e}") from e
    return json.loads(body) if body else {}


def load_session_index(root: Path) -> dict[str, dict[str, Any]]:
    idx_path = root / "sessions.json"
    if not idx_path.exists():
        return {}
    try:
        raw = json.loads(idx_path.read_text(errors="replace"))
    except Exception:
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for item in raw.values():
            if isinstance(item, dict) and item.get("session_id"):
                by_id[item["session_id"]] = item
    return by_id


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def session_files(root: Path, days: int, max_sessions: int) -> list[Path]:
    files = sorted(root.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if days > 0:
        cutoff = datetime.now().timestamp() - days * 86400
        files = [p for p in files if p.stat().st_mtime >= cutoff]
    return files[:max_sessions]


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    return ""


def compact_messages(messages: list[dict[str, Any]], include_tools: bool, max_message_chars: int) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role not in {"user", "assistant"} and not include_tools:
            continue
        if role == "system":
            continue
        if role == "tool" and include_tools:
            text = content_to_text(m.get("content"))
            label = "tool"
            if len(text) > 1200:
                text = text[:1200] + " …<tool output truncated>"
        else:
            text = content_to_text(m.get("content"))
            label = str(role)
        text = redact(" ".join(text.split()))
        if not text:
            continue
        if len(text) > max_message_chars:
            text = text[:max_message_chars] + " …<message truncated>"
        out.append((i, label, text))
    return out


def stable_uuid(session_id: str, chunk_index: int, content_hash: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-session:{session_id}:{chunk_index}:{content_hash}"))


def build_chunks(root: Path, days: int, max_sessions: int, max_chars: int, overlap_messages: int, include_tools: bool, max_message_chars: int) -> list[Chunk]:
    index = load_session_index(root)
    chunks: list[Chunk] = []
    for path in session_files(root, days=days, max_sessions=max_sessions):
        try:
            data = json.loads(path.read_text(errors="replace"))
        except Exception as e:
            print(f"skip unreadable {path}: {e}", file=sys.stderr)
            continue
        session_id = data.get("session_id") or path.stem.removeprefix("session_")
        meta = index.get(session_id, {})
        platform = data.get("platform") or meta.get("platform")
        display_name = meta.get("display_name") or ""
        session_start = data.get("session_start") or meta.get("created_at")
        last_updated = data.get("last_updated") or meta.get("updated_at")
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        compact = compact_messages(messages, include_tools=include_tools, max_message_chars=max_message_chars)
        if not compact:
            continue
        header = f"Session: {session_id}\nPlatform: {platform}\nDisplay: {display_name}\nStarted: {session_start}\nUpdated: {last_updated}\nSource: {path}\n\n"
        buf: list[tuple[int, str, str]] = []
        buf_chars = len(header)
        chunk_idx = 0

        def flush() -> None:
            nonlocal buf, buf_chars, chunk_idx
            if not buf:
                return
            body = "\n\n".join(f"[{idx}] {role}: {text}" for idx, role, text in buf)
            full = header + body
            content_hash = hashlib.sha256(full.encode("utf-8")).hexdigest()[:24]
            payload = {
                "text": full,
                "session_id": session_id,
                "session_file": str(path),
                "platform": platform,
                "display_name": display_name,
                "session_start": session_start,
                "last_updated": last_updated,
                "chunk_index": chunk_idx,
                "message_start": buf[0][0],
                "message_end": buf[-1][0],
                "message_count": len(buf),
                "chars": len(full),
                "content_hash": content_hash,
                "indexed_at": int(time.time()),
                "indexer": "qdrant_ingest_hermes_sessions.py",
                "includes_tools": include_tools,
            }
            chunks.append(Chunk(stable_uuid(session_id, chunk_idx, content_hash), full, payload))
            chunk_idx += 1
            keep = buf[-overlap_messages:] if overlap_messages > 0 else []
            buf = keep[:]
            buf_chars = len(header) + sum(len(x[2]) + 32 for x in buf)

        for item in compact:
            item_chars = len(item[2]) + 32
            if buf and buf_chars + item_chars > max_chars:
                flush()
            buf.append(item)
            buf_chars += item_chars
        flush()
    return chunks


def ensure_collection(base_url: str, collection: str, recreate: bool) -> None:
    if recreate:
        try:
            http_json("DELETE", f"{base_url}/collections/{collection}")
            print(f"deleted existing collection {collection}")
        except RuntimeError as e:
            if "404" not in str(e):
                raise
    create_payload = {"vectors": {"size": VECTOR_SIZE, "distance": DISTANCE}, "on_disk_payload": True}
    try:
        http_json("PUT", f"{base_url}/collections/{collection}", create_payload)
        print(f"created collection {collection}")
    except RuntimeError as e:
        if "already exists" in str(e) or "409" in str(e):
            print(f"collection {collection} already exists")
        else:
            raise


def batched(seq: list[Chunk], n: int) -> Iterable[list[Chunk]]:
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def ingest(args: argparse.Namespace) -> None:
    root = Path(args.root).expanduser().resolve()
    selected_files = session_files(root, days=args.days, max_sessions=args.max_sessions)
    chunks = build_chunks(root, args.days, args.max_sessions, args.max_chars, args.overlap_messages, args.include_tools, args.max_message_chars)
    print(f"sessions_root={root}")
    print(f"chunks={len(chunks)} collection={args.collection} days={args.days} max_sessions={args.max_sessions} include_tools={args.include_tools}")
    print(f"selected_sessions={len(selected_files)}")
    if selected_files:
        mtimes = [p.stat().st_mtime for p in selected_files]
        newest = datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat()
        oldest = datetime.fromtimestamp(min(mtimes), tz=timezone.utc).isoformat()
        sample_ids = [p.stem.removeprefix("session_") for p in selected_files[:10]]
        chunk_counts: dict[str, int] = {}
        for ch in chunks:
            sid = str(ch.payload.get("session_id") or "")
            chunk_counts[sid] = chunk_counts.get(sid, 0) + 1
        sample_counts = ",".join(f"{sid}:{chunk_counts.get(sid, 0)}" for sid in sample_ids)
        print(f"selected_mtime_range_utc={oldest}..{newest}")
        print("selected_session_ids_sample=" + ",".join(sample_ids))
        print("selected_session_chunk_counts_sample=" + sample_counts)
    if args.dry_run:
        for ch in chunks[:8]:
            print(json.dumps({k: ch.payload[k] for k in ["session_id","platform","chunk_index","message_start","message_end","chars"]}, ensure_ascii=False))
            print(ch.text[:350].replace("\n", " ") + "\n")
        return
    ensure_collection(args.url, args.collection, args.recreate)
    model = TextEmbedding(model_name=args.model)
    total = 0
    for batch in batched(chunks, args.batch_size):
        vectors = [[float(x) for x in v] for v in model.embed([c.text for c in batch])]
        points = [{"id": c.point_id, "vector": vec, "payload": c.payload} for c, vec in zip(batch, vectors)]
        http_json("PUT", f"{args.url}/collections/{args.collection}/points?wait=true", {"points": points})
        total += len(points)
        print(f"upserted {total}/{len(chunks)}")
    info = http_json("GET", f"{args.url}/collections/{args.collection}")
    result = info.get("result", {})
    points_count = result.get("points_count") or result.get("vectors_count")
    indexed_vectors_count = result.get("indexed_vectors_count")
    status = result.get("status")
    print(f"final_collection_status={status} final_point_count={points_count} indexed_vectors_count={indexed_vectors_count}")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:3000])


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Ingest recent Hermes sessions into local Qdrant")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--root", default=str(Path.home()/".hermes/sessions"))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--max-sessions", type=int, default=150)
    p.add_argument("--max-chars", type=int, default=2200)
    p.add_argument("--max-message-chars", type=int, default=1200)
    p.add_argument("--overlap-messages", type=int, default=1)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--include-tools", action="store_true", help="include redacted/truncated tool outputs; off by default")
    p.add_argument("--recreate", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    ingest(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
