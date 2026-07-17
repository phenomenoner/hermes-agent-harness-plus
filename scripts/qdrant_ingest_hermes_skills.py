#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastembed>=0.8.0",
#   "PyYAML>=6.0.0",
# ]
# ///
"""Ingest Hermes skills into local Qdrant.

Default collection: hermes_skills_multilingual_v1
Default root: ~/.hermes/skills
Default embedding: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (384 dims)

Examples:
  uv run --script scripts/qdrant_ingest_hermes_skills.py --dry-run
  uv run --script scripts/qdrant_ingest_hermes_skills.py --recreate
  uv run --script scripts/qdrant_ingest_hermes_skills.py --collection hermes_skills_multilingual_v1 --recreate
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml
from fastembed import TextEmbedding

DEFAULT_URL = "http://127.0.0.1:6333"
DEFAULT_COLLECTION = "hermes_skills_multilingual_v1"
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
VECTOR_SIZE = 384
DISTANCE = "Cosine"

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Chunk:
    point_id: str
    text: str
    payload: dict[str, Any]


def http_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 120) -> dict[str, Any]:
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


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    body = text[match.end():]
    try:
        meta = yaml.safe_load(raw) or {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    return meta, body


def skill_id_for(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    if rel.endswith("/SKILL.md"):
        rel = rel[: -len("/SKILL.md")]
    elif rel.endswith(".md"):
        rel = rel[:-3]
    return rel


def category_for(skill_id: str) -> str:
    parts = skill_id.split("/")
    return parts[0] if len(parts) > 1 else "uncategorized"


def split_markdown(text: str, max_chars: int = 1400, overlap: int = 180) -> list[str]:
    text = text.replace("\r\n", "\n")
    blocks = re.split(r"\n(?=#{1,4}\s)", text)
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        s = current.strip()
        if s:
            chunks.append(s)
        current = ""

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if len(block) > max_chars:
            flush()
            start = 0
            while start < len(block):
                end = min(len(block), start + max_chars)
                cut = block[start:end]
                if end < len(block):
                    # Prefer breaking at paragraph/sentence/space near the end.
                    window = cut[max(0, len(cut) - 350):]
                    rel = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("。"), window.rfind(" "))
                    if rel > 80:
                        end = start + max(0, len(cut) - 350) + rel + 1
                        cut = block[start:end]
                chunks.append(cut.strip())
                if end >= len(block):
                    break
                start = max(0, end - overlap)
        elif len(current) + len(block) + 2 <= max_chars:
            current = f"{current}\n\n{block}" if current else block
        else:
            flush()
            current = block
    flush()
    return chunks


def iter_skill_files(root: Path) -> Iterable[Path]:
    # Keep semantic recall aligned with the active catalog. Curator archives and
    # backups live under hidden directories such as .archive/ and
    # .curator_backups/; indexing them would make retired skills discoverable
    # again even though Hermes no longer routes to them.
    for path in sorted(root.glob("**/SKILL.md")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        yield path


def stable_uuid(source: str, chunk_index: int, content_hash: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-skill:{source}:{chunk_index}:{content_hash}"))


def build_chunks(root: Path, max_chars: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in iter_skill_files(root):
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = path.read_text(errors="replace")
        meta, body = parse_frontmatter(raw)
        lifecycle_status = str(meta.get("status") or "").strip().casefold()
        if lifecycle_status in {"retired", "archived", "deprecated"}:
            continue
        skill_id = skill_id_for(path, root)
        category = category_for(skill_id)
        description = str(meta.get("description") or "")
        name = str(meta.get("name") or Path(skill_id).name)
        prefix = f"Skill: {skill_id}\nName: {name}\nDescription: {description}\nSource: {path}\n\n"
        text_for_chunks = prefix + body.strip()
        file_hash = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
        stat = path.stat()
        for idx, chunk_text in enumerate(split_markdown(text_for_chunks, max_chars=max_chars, overlap=overlap)):
            content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()[:24]
            payload = {
                "text": chunk_text,
                "source": str(path),
                "source_rel": path.relative_to(root).as_posix(),
                "skill": skill_id,
                "skill_name": name,
                "description": description,
                "category": category,
                "chunk_index": idx,
                "chars": len(chunk_text),
                "content_hash": content_hash,
                "file_hash": file_hash[:24],
                "mtime": int(stat.st_mtime),
                "indexed_at": int(time.time()),
                "indexer": "qdrant_ingest_hermes_skills.py",
            }
            chunks.append(Chunk(stable_uuid(str(path), idx, content_hash), chunk_text, payload))
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
    if not root.exists():
        raise SystemExit(f"skills root not found: {root}")
    chunks = build_chunks(root, max_chars=args.max_chars, overlap=args.overlap)
    print(f"skills_root={root}")
    print(f"chunks={len(chunks)} collection={args.collection} model={args.model}")
    if args.dry_run:
        for ch in chunks[:5]:
            print(json.dumps({"id": ch.point_id, "skill": ch.payload["skill"], "chunk": ch.payload["chunk_index"], "chars": ch.payload["chars"], "preview": ch.text[:180]}, ensure_ascii=False))
        return

    ensure_collection(args.url, args.collection, recreate=args.recreate)
    model = TextEmbedding(model_name=args.model)
    total = 0
    for batch in batched(chunks, args.batch_size):
        vectors = [[float(x) for x in v] for v in model.embed([c.text for c in batch])]
        points = [
            {"id": c.point_id, "vector": vec, "payload": c.payload}
            for c, vec in zip(batch, vectors)
        ]
        http_json("PUT", f"{args.url}/collections/{args.collection}/points?wait=true", {"points": points}, timeout=180)
        total += len(points)
        print(f"upserted {total}/{len(chunks)}")
    info = http_json("GET", f"{args.url}/collections/{args.collection}")
    print(json.dumps(info.get("result", {}), ensure_ascii=False, indent=2)[:3000])


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Ingest Hermes skills into local Qdrant")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--root", default=str(Path.home()/".hermes/skills"))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--recreate", action="store_true", help="delete and recreate collection first")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--max-chars", type=int, default=1400)
    p.add_argument("--overlap", type=int, default=180)
    args = p.parse_args(argv)
    ingest(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
