from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "qdrant_ingest_hermes_skills.py"


def load_indexer(monkeypatch):
    """Load index-building helpers without installing the embedding runtime."""

    fastembed = ModuleType("fastembed")
    setattr(fastembed, "TextEmbedding", object)
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)

    spec = importlib.util.spec_from_file_location("qdrant_ingest_hermes_skills_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def write_skill(root: Path, relative_dir: str, *, status: str | None = None) -> None:
    skill_dir = root / relative_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = [
        "---",
        f"name: {skill_dir.name}",
        "description: Test fixture",
    ]
    if status is not None:
        frontmatter.append(f'status: "{status}"')
    frontmatter.extend(["---", "", f"# {skill_dir.name}", "", "Fixture body."])
    (skill_dir / "SKILL.md").write_text("\n".join(frontmatter), encoding="utf-8")


def test_build_chunks_only_includes_visible_active_skills(tmp_path, monkeypatch):
    indexer = load_indexer(monkeypatch)

    write_skill(tmp_path, "general/plain")
    write_skill(tmp_path, "general/current", status="current")
    write_skill(tmp_path, "general/retired", status=" Retired ")
    write_skill(tmp_path, "general/archived", status="ARCHIVED")
    write_skill(tmp_path, "general/deprecated", status="deprecated")
    write_skill(tmp_path, ".archive/old")
    write_skill(tmp_path, "general/.curator_backups/snapshot")

    chunks = indexer.build_chunks(tmp_path, max_chars=10_000, overlap=0)
    indexed_skills = {chunk.payload["skill"] for chunk in chunks}

    assert indexed_skills == {"general/current", "general/plain"}