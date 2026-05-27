#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.22.0",
#   "fastembed>=0.8.0",
# ]
# ///
"""Local Qdrant MCP sidecar for Hermes Agent.

Provides a tiny stdio MCP server around local Qdrant at
http://127.0.0.1:6333. Designed to be launched by Hermes native MCP.

Tools registered by Hermes as mcp_qdrant_*:
- qdrant_collections
- qdrant_collection_info
- qdrant_search
- qdrant_search_all
"""
from __future__ import annotations

import json
import os
import textwrap
import urllib.error
import urllib.request
from typing import Any

from fastembed import TextEmbedding
from mcp.server.fastmcp import FastMCP

DEFAULT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
DEFAULT_COLLECTION = os.getenv("QDRANT_COLLECTION", "hermes_skills_multilingual_v1")
DEFAULT_MODEL = os.getenv("QDRANT_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
DEFAULT_SEARCH_ALL_COLLECTIONS = [
    c.strip()
    for c in os.getenv("QDRANT_SEARCH_ALL_COLLECTIONS", "hermes_skills_multilingual_v1,hermes_sessions_recent_multilingual_v1").split(",")
    if c.strip()
]

mcp = FastMCP("qdrant")
_embedding_model: TextEmbedding | None = None


def _http_json(method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    url = f"{DEFAULT_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        return {"ok": False, "error": f"HTTP {e.code}", "detail": detail, "url": url}
    except urllib.error.URLError as e:
        return {"ok": False, "error": "connection_failed", "detail": str(e), "url": url}
    try:
        return {"ok": True, "response": json.loads(body)}
    except json.JSONDecodeError:
        return {"ok": True, "response_text": body}


def _embed(text: str, model_name: str = DEFAULT_MODEL) -> list[float]:
    global _embedding_model
    if model_name != DEFAULT_MODEL:
        model = TextEmbedding(model_name=model_name)
    else:
        if _embedding_model is None:
            _embedding_model = TextEmbedding(model_name=model_name)
        model = _embedding_model
    # FastEmbed returns numpy.float32 values; JSON needs plain Python floats.
    return [float(x) for x in next(model.embed([text]))]


def _parse_collections(collections: str | list[str] | None) -> list[str]:
    if collections is None:
        return DEFAULT_SEARCH_ALL_COLLECTIONS[:]
    if isinstance(collections, str):
        out = [c.strip() for c in collections.split(",") if c.strip()]
    else:
        out = [str(c).strip() for c in collections if str(c).strip()]
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(out))


def _compact_hit(hit: dict[str, Any], collection: str, preview_chars: int) -> dict[str, Any]:
    pl = hit.get("payload") or {}
    text = " ".join((pl.get("text") or "").split())
    return {
        "collection": collection,
        "score": hit.get("score"),
        "skill": pl.get("skill"),
        "source": pl.get("source") or pl.get("session_file") or pl.get("path"),
        "chunk_index": pl.get("chunk_index"),
        "session_id": pl.get("session_id"),
        "platform": pl.get("platform"),
        "preview": textwrap.shorten(text, width=max(80, int(preview_chars)), placeholder=" …"),
    }


def _search_collection(
    *,
    query: str,
    collection: str,
    vector: list[float],
    limit: int,
    filter_skill: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
    }
    if filter_skill:
        payload["filter"] = {"must": [{"key": "skill", "match": {"value": filter_skill}}]}
    return _http_json("POST", f"/collections/{collection}/points/search", payload, timeout=90)


@mcp.tool()
def qdrant_collections() -> str:
    """List local Qdrant collections with basic status and vector configuration."""
    data = _http_json("GET", "/collections")
    if not data.get("ok"):
        return json.dumps(data, ensure_ascii=False)
    collections = data.get("response", {}).get("result", {}).get("collections", [])
    out: list[dict[str, Any]] = []
    for item in collections:
        name = item.get("name")
        info = _http_json("GET", f"/collections/{name}")
        result = (info.get("response") or {}).get("result", {}) if info.get("ok") else {}
        out.append({
            "name": name,
            "status": result.get("status"),
            "points_count": result.get("points_count"),
            "indexed_vectors_count": result.get("indexed_vectors_count"),
            "vectors": result.get("config", {}).get("params", {}).get("vectors"),
        })
    return json.dumps({"ok": True, "url": DEFAULT_URL, "collections": out}, ensure_ascii=False, indent=2)


@mcp.tool()
def qdrant_collection_info(collection: str = DEFAULT_COLLECTION) -> str:
    """Get detailed Qdrant collection metadata for a collection."""
    data = _http_json("GET", f"/collections/{collection}")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def qdrant_search(
    query: str,
    collection: str = DEFAULT_COLLECTION,
    limit: int = 5,
    filter_skill: str | None = None,
    preview_chars: int = 500,
    raw_json: bool = False,
) -> str:
    """Semantic-search a local Qdrant collection.

    Args:
        query: Natural-language query to embed and search.
        collection: Qdrant collection name. Defaults to DEFAULT_COLLECTION, currently hermes_skills_multilingual_v1 unless QDRANT_COLLECTION overrides it.
        limit: Number of hits to return.
        filter_skill: Optional exact payload skill filter, e.g. autonomous-ai-agents/hermes-agent.
        preview_chars: Maximum text preview length per hit when raw_json is false.
        raw_json: Return raw Qdrant hits instead of compact summaries.
    """
    if not query.strip():
        return json.dumps({"ok": False, "error": "query_required"}, ensure_ascii=False)
    limit = max(1, min(int(limit), 25))
    vector = _embed(query)
    data = _search_collection(query=query, collection=collection, vector=vector, limit=limit, filter_skill=filter_skill)
    if not data.get("ok"):
        return json.dumps(data, ensure_ascii=False, indent=2)
    hits = data.get("response", {}).get("result", [])
    if raw_json:
        return json.dumps({"ok": True, "query": query, "collection": collection, "hits": hits}, ensure_ascii=False, indent=2)

    summaries = [_compact_hit(hit, collection, preview_chars) for hit in hits]
    return json.dumps({"ok": True, "query": query, "collection": collection, "hits": summaries}, ensure_ascii=False, indent=2)


@mcp.tool()
def qdrant_search_all(
    query: str,
    collections: str | list[str] | None = None,
    limit: int = 8,
    per_collection_limit: int = 5,
    preview_chars: int = 420,
    raw_json: bool = False,
) -> str:
    """Semantic-search multiple Qdrant collections and merge/rank hits.

    Defaults to the common Hermes recall collections from env
    QDRANT_SEARCH_ALL_COLLECTIONS, currently
    hermes_skills_multilingual_v1 and hermes_sessions_recent_multilingual_v1 by
    default. Pass a comma-separated string or list of collection names to include
    project-specific corpora. Avoid mixing legacy `_v1` collections with multilingual collections
    in one run because query-time and ingest-time embedding models must match.
    """
    if not query.strip():
        return json.dumps({"ok": False, "error": "query_required"}, ensure_ascii=False)
    target_collections = _parse_collections(collections)
    if not target_collections:
        return json.dumps({"ok": False, "error": "collections_required"}, ensure_ascii=False)

    limit = max(1, min(int(limit), 50))
    per_collection_limit = max(1, min(int(per_collection_limit), 25))
    vector = _embed(query)

    merged_raw: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for collection in target_collections:
        data = _search_collection(query=query, collection=collection, vector=vector, limit=per_collection_limit)
        if not data.get("ok"):
            errors.append({"collection": collection, "error": data.get("error"), "detail": data.get("detail")})
            continue
        for hit in data.get("response", {}).get("result", []):
            item = dict(hit)
            item["collection"] = collection
            merged_raw.append(item)

    merged_raw.sort(key=lambda h: float(h.get("score") or 0), reverse=True)
    merged_raw = merged_raw[:limit]
    if raw_json:
        return json.dumps({
            "ok": not errors,
            "query": query,
            "collections": target_collections,
            "errors": errors,
            "hits": merged_raw,
        }, ensure_ascii=False, indent=2)

    summaries = [_compact_hit(hit, str(hit.get("collection")), preview_chars) for hit in merged_raw]
    return json.dumps({
        "ok": not errors,
        "query": query,
        "collections": target_collections,
        "errors": errors,
        "hits": summaries,
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
