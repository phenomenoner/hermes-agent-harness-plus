from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_SKILL = ROOT / "skills" / "context-canvas-memory" / "SKILL.md"
REFLECTION_SKILL = ROOT / "skills" / "context-canvas-reflection" / "SKILL.md"
AWARENESS_RULE = ROOT / "ops-rules" / "context-canvas-awareness.md"
INSTALL_GUIDE = ROOT / "docs" / "install.md"
UPSTREAM_LICENSE = ROOT / "docs" / "third-party" / "context-canvas-codex-LICENSE.txt"


def load_skill(path: Path) -> tuple[dict[str, object], str]:
    content = path.read_text(encoding="utf-8")
    marker, frontmatter, body = content.split("---", 2)
    assert marker == ""
    metadata = yaml.safe_load(frontmatter)
    assert isinstance(metadata, dict)
    return metadata, body


def test_checkpoint_skill_declares_optional_non_authoritative_boundary() -> None:
    _, body = load_skill(CHECKPOINT_SKILL)
    normalized = " ".join(body.lower().split())

    assert "optional navigation and context-offload layer" in normalized
    for forbidden_role in ("task authority", "workflow engine", "wal", "release gate"):
        assert forbidden_role in normalized
    assert "untrusted historical data" in normalized
    assert "never blocks the underlying task" in normalized


def test_reflection_skill_is_bounded_and_uses_only_hermes_canvas_surfaces() -> None:
    metadata, body = load_skill(REFLECTION_SKILL)
    description = str(metadata["description"])
    normalized = " ".join(body.lower().split())

    assert metadata["name"] == "context-canvas-reflection"
    assert len(description) <= 60
    assert "failures" in description[:57].lower()
    assert "contradicts" in description[:57].lower()
    assert "advisory disposition only" in normalized
    assert "at most three implicit reflection passes" in normalized
    assert "ordinary `continue` writes nothing" in normalized

    hermes_calls = set(re.findall(r"`(mcp__context_canvas__canvas_[a-z_]+)", body))
    assert hermes_calls == {
        "mcp__context_canvas__canvas_read",
        "mcp__context_canvas__canvas_search",
        "mcp__context_canvas__canvas_upsert_node",
    }
    for codex_only_surface in (
        "reference_put",
        "reference_read",
        "snapshot_capture_next",
        "canvas_continue",
        "sessionstart",
        "userpromptsubmit",
    ):
        assert codex_only_surface not in normalized


def test_global_awareness_rule_selects_skills_without_granting_authority() -> None:
    normalized = " ".join(AWARENESS_RULE.read_text(encoding="utf-8").lower().split())

    assert "context-canvas-memory" in normalized
    assert "context-canvas-reflection" in normalized
    assert "before the third substantive tool call" in normalized
    assert "never task authority" in normalized
    assert "does not grant approval" in normalized
    assert "do not start" in normalized


def test_install_guide_installs_both_skills_and_projects_global_awareness() -> None:
    install = INSTALL_GUIDE.read_text(encoding="utf-8")

    assert "cp -R skills/context-canvas-reflection" in install
    assert "ops-rules/context-canvas-awareness.md" in install
    assert "agent.coding_instructions" in install
    assert "hermes skills list" in install
    assert "hermes config get agent.coding_instructions" in install


def test_adapted_skills_preserve_complete_upstream_mit_notices() -> None:
    license_text = UPSTREAM_LICENSE.read_text(encoding="utf-8")

    assert "Copyright (c) 2026 phenomenoner and ChatGPT Codex App Plus contributors" in license_text
    assert "Copyright (c) 2026 context-canvas-codex contributors" in license_text
    assert "Permission is hereby granted, free of charge" in license_text
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in license_text
