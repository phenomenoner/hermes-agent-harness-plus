from __future__ import annotations

import importlib.util
import re
import sys
from itertools import product
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "context-canvas-memory" / "SKILL.md"
AUTOPILOT_PLUGIN = ROOT / "plugins" / "context-canvas-autopilot" / "__init__.py"
PUBLISHABLE_ARTIFACTS = (
    SKILL,
    ROOT / "docs" / "install.md",
    ROOT / "docs" / "technical" / "context-canvas-v2-reverse-shadow.md",
    ROOT / "plugins" / "context-canvas-autopilot" / "__init__.py",
    ROOT / "plugins" / "context-canvas-autopilot" / "plugin.yaml",
    ROOT / "tests" / "test_context_canvas_autopilot.py",
    Path(__file__).resolve(),
)

_SHARE_READY_PATTERNS = {
    "non-placeholder home path": re.compile(
        r"(?<![A-Za-z0-9_.-])/(?:home|Users)/"
        r"(?!(?:you|user|example|<[^/>\s]+>)(?:/|$))"
        r"[^/\s`'\"<>]+(?=/|$)"
    ),
    "mounted workstation path": re.compile(
        r"(?<![A-Za-z0-9_.-])/mnt/[A-Za-z](?=/|$)"
    ),
    "non-placeholder Windows profile path": re.compile(
        r"(?<![A-Za-z0-9_.-])[A-Za-z]:[\\/]+Users[\\/]+"
        r"(?!(?:you|user|example|<[^\\/>\s]+>)(?:[\\/]|$))"
        r"[^\\/\s`'\"<>]+(?=[\\/]|$)",
        re.IGNORECASE,
    ),
    "credential-bearing URL": re.compile(
        r"\bhttps?://[^/\s?#@]+@", re.IGNORECASE
    ),
    "GitHub access token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
    ),
    "AWS access key": re.compile(
        r"\b(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}\b"
    ),
    "private key material": re.compile(
        r"-----BEGIN[ \t]+[^\r\n-]*PRIVATE KEY[^\r\n-]*-----",
        re.IGNORECASE,
    ),
}


def share_ready_violations(label: str, content: str) -> list[str]:
    return [
        f"{label}: {description}"
        for description, pattern in _SHARE_READY_PATTERNS.items()
        if pattern.search(content)
    ]


def load_skill() -> tuple[dict[str, object], str]:
    content = SKILL.read_text(encoding="utf-8")
    marker, frontmatter, body = content.split("---", 2)
    assert marker == ""
    metadata = yaml.safe_load(frontmatter)
    assert isinstance(metadata, dict)
    return metadata, body


def section(body: str, heading: str) -> str:
    start = body.index(heading) + len(heading)
    remainder = body[start:]
    heading_level = len(heading) - len(heading.lstrip("#"))
    next_heading = re.search(rf"\n#{{1,{heading_level}}} ", remainder)
    return remainder if next_heading is None else remainder[: next_heading.start()]


def markdown_table(section_text: str) -> list[dict[str, str]]:
    rows = [line for line in section_text.splitlines() if line.startswith("|")]
    assert len(rows) >= 3
    headers = [cell.strip() for cell in rows[0].strip("|").split("|")]
    return [
        dict(zip(headers, [cell.strip() for cell in row.strip("|").split("|")], strict=True))
        for row in rows[2:]
    ]


def code_token(cell: str) -> str:
    match = re.fullmatch(r"`([^`]+)`", cell.strip())
    assert match is not None, f"expected one code token, got {cell!r}"
    return match.group(1)


def documented_trigger_decision(
    body: str,
    *,
    safety_exclusion: bool,
    decisive_trigger: bool,
    simple_or_bounded: bool,
    complexity_signals: int,
) -> bool:
    trigger_policy = section(body, "## Trigger Policy")
    rows = markdown_table(trigger_policy)
    assert all(set(row) == {"Priority", "Match when", "Decision"} for row in rows)
    assert [int(row["Priority"]) for row in rows] == list(range(1, len(rows) + 1))
    assert sum(code_token(row["Match when"]) == "otherwise" for row in rows) == 1

    values = {
        "safety_exclusion": safety_exclusion,
        "decisive_trigger": decisive_trigger,
        "simple_or_bounded": simple_or_bounded,
    }
    comparisons = {
        ">=": lambda left, right: left >= right,
        ">": lambda left, right: left > right,
        "==": lambda left, right: left == right,
        "<=": lambda left, right: left <= right,
        "<": lambda left, right: left < right,
    }

    for index, row in enumerate(rows):
        condition = code_token(row["Match when"])
        if condition == "otherwise":
            assert index == len(rows) - 1
            matched = True
        else:
            boolean_match = re.fullmatch(
                r"(safety_exclusion|decisive_trigger|simple_or_bounded) = (true|false)",
                condition,
            )
            numeric_match = re.fullmatch(r"complexity_signals (>=|>|==|<=|<) (\d+)", condition)
            assert (boolean_match is None) != (numeric_match is None), condition
            if boolean_match is not None:
                expected = boolean_match.group(2) == "true"
                matched = values[boolean_match.group(1)] is expected
            else:
                assert numeric_match is not None
                matched = comparisons[numeric_match.group(1)](
                    complexity_signals,
                    int(numeric_match.group(2)),
                )
        if matched:
            decision = code_token(row["Decision"])
            assert decision in {"start", "do_not_start"}
            return decision == "start"

    raise AssertionError("trigger decision table has no matching row")


def assert_trigger_contract(metadata: dict[str, object], body: str) -> None:
    description = str(metadata["description"])
    preview = description[:57].lower()

    assert len(description) <= 60
    assert description.endswith(".")
    assert {"compaction", "coordination", "two"} <= set(preview.rstrip(".").replace(",", "").split())
    assert "5 tools" not in preview

    for safety, decisive, bounded, signal_count in product(
        (False, True),
        (False, True),
        (False, True),
        range(4),
    ):
        expected = not safety and (decisive or (not bounded and signal_count >= 2))
        actual = documented_trigger_decision(
            body,
            safety_exclusion=safety,
            decisive_trigger=decisive,
            simple_or_bounded=bounded,
            complexity_signals=signal_count,
        )
        assert actual is expected, (
            f"trigger mismatch for safety={safety}, decisive={decisive}, "
            f"bounded={bounded}, complexity_signals={signal_count}"
        )

    trigger_policy = section(body, "## Trigger Policy")
    deliverable_rule = next(line for line in trigger_policy.splitlines() if "deliverables" in line)
    assert "non-trivial" in deliverable_rule
    assert "edit/test/report" in deliverable_rule
    assert "do not count" in deliverable_rule


def documented_manual_policy(body: str) -> dict[str, tuple[frozenset[str], bool]]:
    record_section = section(body, "### 2. Record evidence and nodes atomically")
    rows = markdown_table(record_section)
    assert all(
        set(row) == {"Selector", "Required before the call", "Automatic protection"}
        for row in rows
    )

    policies: dict[str, tuple[frozenset[str], bool]] = {}
    for row in rows:
        selector = code_token(row["Selector"])
        actions = frozenset(code_token(row["Required before the call"]).split("+"))
        automatic = code_token(row["Automatic protection"])
        assert selector not in policies
        assert automatic in {"yes", "no"}
        policies[selector] = (actions, automatic == "yes")
    return policies


def assert_manual_safety_contract(_: dict[str, object], body: str) -> None:
    record_section = section(body, "### 2. Record evidence and nodes atomically")
    policies = documented_manual_policy(body)
    assert policies == {
        "*": (frozenset({"minimize", "sanitize"}), False),
        "credential-bearing-url": (
            frozenset({"minimize", "sanitize", "remove-secret"}),
            False,
        ),
        "sensitive-path": (
            frozenset({"minimize", "sanitize", "replace-with-non-identifying-label"}),
            False,
        ),
        "autopilot-sanitization": (frozenset({"manual-safety-still-required"}), False),
    }

    field_line = next(line for line in record_section.splitlines() if "wildcard includes" in line.lower())
    documented_fields = set(re.findall(r"`([a-z_]+)`", field_line))
    assert documented_fields == {
        "goal",
        "title",
        "summary",
        "label",
        "source",
        "content",
        "session_id",
        "node_id",
        "depends_on",
        "metadata",
    }
    for field in (*sorted(documented_fields), "any_future_manual_field"):
        actions, automatic = policies["*"]
        assert actions == {"minimize", "sanitize"}, field
        assert automatic is False, field

    manual_calls = set(re.findall(r"`(canvas_[a-z_*]+)`", record_section))
    assert {"canvas_start", "canvas_add_ref", "canvas_upsert_node", "canvas_record", "canvas_*"} <= manual_calls
    normalized = " ".join(record_section.lower().split())
    assert "manual canvas calls do not perform automatic redaction" in normalized
    for storage_surface in ("canonical json", "events", "refs", "search results", "closeout exports"):
        assert storage_surface in normalized


def load_autopilot_policy():
    module_name = "context_canvas_autopilot_skill_contract"
    spec = importlib.util.spec_from_file_location(
        module_name,
        AUTOPILOT_PLUGIN,
        submodule_search_locations=[str(AUTOPILOT_PLUGIN.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def documented_autopilot_classes(body: str) -> dict[str, str]:
    autopilot = section(body, "## Manual Canvas vs Autopilot")
    rows = markdown_table(autopilot)
    assert all(set(row) == {"Policy case", "Semantic class"} for row in rows)
    classes = {
        code_token(row["Policy case"]): code_token(row["Semantic class"])
        for row in rows
    }
    assert len(classes) == len(rows)
    return classes


def assert_autopilot_contract(_: dict[str, object], body: str) -> None:
    scenarios = {
        "failed-tool-result": ("read_file", {"path": "fixture.txt"}, "error"),
        "failed-allowlisted-verification-command": (
            "terminal",
            {"command": "python -m pytest -q"},
            "error",
        ),
        "successful-allowlisted-verification-command": (
            "terminal",
            {"command": "python -m pytest -q"},
            "ok",
        ),
        "successful-allowlisted-mutation-tool": ("patch", {"path": "fixture.txt"}, "ok"),
        "successful-allowlisted-mutation-command": (
            "terminal",
            {"command": "git commit -m fixture"},
            "ok",
        ),
        "successful-delegation": ("delegate_task", {"goal": "fixture"}, "ok"),
        "successful-ordinary-tool-result": ("read_file", {"path": "fixture.txt"}, "ok"),
        "successful-ordinary-command": (
            "terminal",
            {"command": "python fixture.py"},
            "ok",
        ),
    }
    documented = documented_autopilot_classes(body)
    assert set(documented) == set(scenarios)

    plugin = load_autopilot_policy()
    runtime = {
        case: plugin._semantic_class(tool_name, args, status)
        for case, (tool_name, args, status) in scenarios.items()
    }
    assert documented == runtime

    autopilot = " ".join(section(body, "## Manual Canvas vs Autopilot").lower().split())
    assert "fixed tool and command allowlists determine which successful calls match" in autopilot
    assert "failure classification takes precedence" in autopilot
    assert "fail-open mechanism" in autopilot
    assert "sanitized snapshots" in autopilot


def test_trigger_policy_defines_preview_and_exclusion_precedence() -> None:
    metadata, body = load_skill()
    assert_trigger_contract(metadata, body)


def test_manual_field_safety_contract_covers_every_persisted_value() -> None:
    metadata, body = load_skill()
    assert_manual_safety_contract(metadata, body)


def test_factual_evidence_contract_matches_canvas_validation() -> None:
    _, body = load_skill()
    graph_section = section(body, "### 3. Keep the graph truthful")
    rows = {row["Rule"]: row for row in markdown_table(graph_section)}

    evidence_rule = rows["Evidence required"]
    kinds = set(re.findall(r"`([a-z]+)`", evidence_rule["Node kinds"]))
    statuses = set(re.findall(r"`([a-z]+)`", evidence_rule["Statuses"]))
    assert kinds == {"finding", "action", "decision", "blocked", "gap", "verification"}
    assert statuses == {"done", "blocked", "deprecated", "verify"}
    assert "at least one readable evidence ref" in evidence_rule["Contract"].lower()


def test_closeout_examples_use_real_arguments_statuses_and_lifecycle() -> None:
    _, body = load_skill()
    closeout_section = section(body, "## Closeout and Durable Triage")
    calls = set(re.findall(r"`(mcp__context_canvas__canvas_(?:read|closeout)\([^`]+\))`", closeout_section))

    assert calls == {
        'mcp__context_canvas__canvas_read(session_id="<active-session-id>", include_refs=false)',
        'mcp__context_canvas__canvas_closeout(session_id="<active-session-id>", write_ref=false)',
        'mcp__context_canvas__canvas_closeout(session_id="<active-session-id>", write_ref=true)',
    }
    assert all('session_id="<active-session-id>"' in call for call in calls)

    stale_rule = next(line for line in closeout_section.splitlines() if "stale `doing`" in line)
    assert set(re.findall(r"`([a-z]+)`", stale_rule)) == {
        "doing",
        "verify",
        "done",
        "blocked",
        "deprecated",
        "planned",
    }
    assert "open state" not in stale_rule.lower()

    lifecycle_contract = closeout_section.lower()
    assert "does not set lifecycle state" in lifecycle_contract
    assert "does not mark the canvas closed" in lifecycle_contract


def test_child_canvas_handoff_uses_export_and_real_unfinished_statuses() -> None:
    _, body = load_skill()
    coordination = section(body, "### 5. Coordinate parent and child work")

    assert 'mcp__context_canvas__canvas_closeout(session_id="<child-session-id>", write_ref=true)' in coordination
    assert {"planned", "blocked"} <= set(re.findall(r"`([a-z]+)`", coordination))
    assert "does not close" in coordination.lower()
    assert "close or explicitly leave open" not in coordination.lower()


def test_verification_checklist_reuses_exact_session_scoped_calls() -> None:
    _, body = load_skill()
    verification = section(body, "## Verification")

    assert 'mcp__context_canvas__canvas_read(session_id="<active-session-id>", include_refs=false)' in verification
    assert 'mcp__context_canvas__canvas_closeout(session_id="<active-session-id>", write_ref=true)' in verification


def test_autopilot_documented_policy_matches_runtime_behavior() -> None:
    metadata, body = load_skill()
    assert_autopilot_contract(metadata, body)


@pytest.mark.parametrize(
    ("validator_name", "original", "reversed_semantics"),
    (
        (
            "assert_trigger_contract",
            "| 2 | `decisive_trigger = true` | `start` |",
            "| 2 | `decisive_trigger = true` | `do_not_start` |",
        ),
        (
            "assert_manual_safety_contract",
            "| `*` | `minimize+sanitize` | `no` |",
            "| `*` | `do-not-minimize+do-not-sanitize` | `no` |",
        ),
        (
            "assert_autopilot_contract",
            "| `successful-allowlisted-verification-command` | `verification` |",
            "| `successful-allowlisted-verification-command` | `none` |",
        ),
    ),
)
def test_contract_validators_reject_reversed_semantics(
    validator_name: str,
    original: str,
    reversed_semantics: str,
) -> None:
    metadata, body = load_skill()
    assert body.count(original) == 1
    mutated_body = body.replace(original, reversed_semantics, 1)

    with pytest.raises(AssertionError):
        globals()[validator_name](metadata, mutated_body)


def test_share_ready_validator_rejects_generic_unsafe_fixtures() -> None:
    unsafe_fixtures = (
        "/home/" + "local-user/private-repository",
        "/mnt/" + "d/private-repository",
        "C:" + "\\Users\\local-user\\private-repository",
        "https://" + "operator:credential@example.invalid/source",
        "gh" + "p_" + "A" * 30,
    )

    for fixture in unsafe_fixtures:
        assert share_ready_violations("fixture", fixture)


@pytest.mark.parametrize(
    ("fixture", "expected_violation"),
    (
        ("/Users/" + "alice/private-repository", "non-placeholder home path"),
        ("C:/" + "Users/alice/private-repository", "non-placeholder Windows profile path"),
        ("C:" + "\\Users\\alice\\private-repository", "non-placeholder Windows profile path"),
        ("/home/" + "you-private/repository", "non-placeholder home path"),
        ("/Users/" + "example-private/repository", "non-placeholder home path"),
        ("C:/" + "Users/user-private/repository", "non-placeholder Windows profile path"),
        ("https://" + "tokenvalue@example.invalid/source", "credential-bearing URL"),
        ("github" + "_pat_" + "A" * 40, "GitHub access token"),
        ("AK" + "IA" + "A1" * 8, "AWS access key"),
        ("-" * 5 + "BEGIN " + "PRIVATE KEY" + "-" * 5, "private key material"),
    ),
)
def test_share_ready_validator_rejects_portable_path_and_secret_mutants(
    fixture: str,
    expected_violation: str,
) -> None:
    assert f"fixture: {expected_violation}" in share_ready_violations("fixture", fixture)


def test_share_ready_validator_allows_only_exact_placeholder_home_segments() -> None:
    placeholder_fixtures = (
        "/home/you/project",
        "/home/user/project",
        "/home/example/project",
        "/home/<local-user>/project",
        "/Users/you/project",
        "/Users/<local-user>/project",
        "C:/Users/example/project",
        "C:\\Users\\<local-user>\\project",
    )

    for fixture in placeholder_fixtures:
        assert share_ready_violations("placeholder", fixture) == []


def test_publishable_artifact_set_is_complete_and_explicit() -> None:
    assert PUBLISHABLE_ARTIFACTS == (
        SKILL,
        ROOT / "docs" / "install.md",
        ROOT / "docs" / "technical" / "context-canvas-v2-reverse-shadow.md",
        ROOT / "plugins" / "context-canvas-autopilot" / "__init__.py",
        ROOT / "plugins" / "context-canvas-autopilot" / "plugin.yaml",
        ROOT / "tests" / "test_context_canvas_autopilot.py",
        Path(__file__).resolve(),
    )


def test_all_publishable_context_canvas_artifacts_are_share_ready() -> None:
    violations: list[str] = []
    for path in PUBLISHABLE_ARTIFACTS:
        violations.extend(
            share_ready_violations(
                str(path.relative_to(ROOT)),
                path.read_text(encoding="utf-8"),
            )
        )

    assert violations == []
