"""Context Canvas Autopilot plugin.

A deliberately lightweight hook for Hermes Agent:
- observes post_tool_call only;
- creates a local Task Canvas after a configurable tool-count threshold or a
  large tool result;
- writes evidence refs and concise nodes through the CanvasStore;
- never mutates the active conversation or compression core.

The plugin is opt-in through ``plugins.enabled``. Runtime knobs are environment
variables so the pilot can be tuned without adding core config schema yet:

- HERMES_CONTEXT_CANVAS_TOOL_THRESHOLD (default: 5)
- HERMES_CONTEXT_CANVAS_LARGE_RESULT_CHARS (default: 6000)
- HERMES_CONTEXT_CANVAS_MAX_REF_CHARS (default: 50000)
- HERMES_CONTEXT_CANVAS_TOOL (path to the context-canvas package source; optional)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

_EXCLUDED_PREFIXES = (
    "mcp_context_canvas_",
    "canvas_",
)
_EXCLUDED_TOOLS = {
    "todo",  # agent-loop tool; included for safety if hook surface changes
    "memory",
    "session_search",
}
_HIGH_SIGNAL_TOOLS = {
    "terminal",
    "execute_code",
    "read_file",
    "search_files",
    "browser_snapshot",
    "browser_console",
    "web_extract",
    "web_search",
    "delegate_task",
    "mcp_qdrant_qdrant_search",
    "mcp_qdrant_qdrant_search_all",
    "mcp_mempalace_mempalace_search",
}

_lock = threading.Lock()
_session_counts: dict[str, int] = {}
_active_canvases: set[str] = set()


class PolicyDecision(NamedTuple):
    should_capture: bool
    reason: str = ""
    canvas_id: str = ""


def reset_state_for_tests() -> None:
    with _lock:
        _session_counts.clear()
        _active_canvases.clear()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def _tracker_key(task_id: str = "", session_id: str = "") -> str:
    return session_id or task_id or "default"


def _canvas_id_for(task_id: str = "", session_id: str = "") -> str:
    key = _tracker_key(task_id, session_id)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in key).strip("-._")
    return f"auto-{safe[:72] or 'default'}"


def _is_excluded_tool(tool_name: str) -> bool:
    if not tool_name or tool_name in _EXCLUDED_TOOLS:
        return True
    return any(tool_name.startswith(prefix) for prefix in _EXCLUDED_PREFIXES)


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        return repr(result)


def evaluate_tool_event(
    *,
    tool_name: str,
    result: Any,
    task_id: str = "",
    session_id: str = "",
) -> PolicyDecision:
    """Return whether a tool event should be captured into a Task Canvas."""
    if _is_excluded_tool(tool_name):
        return PolicyDecision(False, "excluded_tool")

    key = _tracker_key(task_id, session_id)
    canvas_id = _canvas_id_for(task_id, session_id)
    result_len = len(_result_text(result))
    large_threshold = _int_env("HERMES_CONTEXT_CANVAS_LARGE_RESULT_CHARS", 6000)
    tool_threshold = _int_env("HERMES_CONTEXT_CANVAS_TOOL_THRESHOLD", 5)

    with _lock:
        count = _session_counts.get(key, 0) + 1
        _session_counts[key] = count
        already_active = canvas_id in _active_canvases

    if tool_name == "skill_view":
        if already_active:
            return PolicyDecision(True, "skill_view_metadata", canvas_id)
        return PolicyDecision(False, f"skill_view_no_active_canvas:{count}", canvas_id)
    if result_len >= large_threshold:
        return PolicyDecision(True, f"large_result:{result_len}", canvas_id)
    if count >= tool_threshold:
        return PolicyDecision(True, f"tool_threshold:{count}", canvas_id)
    if already_active and tool_name in _HIGH_SIGNAL_TOOLS:
        return PolicyDecision(True, "active_high_signal_tool", canvas_id)
    return PolicyDecision(False, f"below_threshold:{count}", canvas_id)


def _ensure_context_canvas_importable() -> None:
    candidates = []
    if os.getenv("HERMES_CONTEXT_CANVAS_TOOL"):
        candidates.append(Path(os.environ["HERMES_CONTEXT_CANVAS_TOOL"]).expanduser())
    try:
        from hermes_constants import get_hermes_home
        candidates.append(Path(get_hermes_home()) / "context-canvas-tool")
    except Exception:
        pass
    candidates.append(Path.home() / ".hermes" / "context-canvas-tool")

    for candidate in candidates:
        if (candidate / "context_canvas" / "core.py").exists():
            s = str(candidate)
            if s not in sys.path:
                sys.path.insert(0, s)
            return


def _store():
    _ensure_context_canvas_importable()
    from context_canvas.core import CanvasStore

    return CanvasStore()


def _ensure_canvas(canvas_id: str, *, task_id: str = "", session_id: str = "") -> None:
    if canvas_id in _active_canvases:
        return
    store = _store()
    try:
        store.read(canvas_id)
    except FileNotFoundError:
        goal = (
            "Autopilot evidence canvas for Hermes session "
            f"{session_id or task_id or 'default'}; created by post_tool_call policy."
        )
        store.start(goal=goal, session_id=canvas_id, title=f"Autopilot Canvas: {session_id or task_id or 'default'}")
    with _lock:
        _active_canvases.add(canvas_id)


def _summarize_args(args: Any, max_chars: int = 500) -> str:
    try:
        text = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        text = repr(args)
    return text if len(text) <= max_chars else text[:max_chars] + f"... [truncated args from {len(text)} chars]"


def _evidence_text(
    *,
    tool_name: str,
    args: Any,
    result: Any,
    reason: str,
    duration_ms: int | None = None,
) -> str:
    result_text = _result_text(result)
    max_ref_chars = _int_env("HERMES_CONTEXT_CANVAS_MAX_REF_CHARS", 50000)
    truncated = ""
    if len(result_text) > max_ref_chars:
        original_len = len(result_text)
        result_text = result_text[:max_ref_chars]
        truncated = f"\n\n[truncated to {max_ref_chars} chars from {original_len} chars]"
    meta = {
        "tool_name": tool_name,
        "reason": reason,
        "duration_ms": duration_ms,
        "args": _summarize_args(args),
    }
    return (
        "# Autopilot tool evidence\n\n"
        f"```json\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n```\n\n"
        "## Result\n\n"
        f"{result_text}{truncated}\n"
    )


def _skill_name_from_event(args: Any, result: Any) -> str:
    if isinstance(result, dict) and result.get("name"):
        return str(result["name"])
    if isinstance(args, dict) and args.get("name"):
        return str(args["name"])
    return "unknown"


def _skill_metadata_text(
    *,
    args: Any,
    result: Any,
    reason: str,
    duration_ms: int | None = None,
) -> str:
    payload: dict[str, Any] = {
        "tool_name": "skill_view",
        "reason": reason,
        "duration_ms": duration_ms,
        "args": _summarize_args(args),
        "captured_mode": "metadata_only",
    }
    if isinstance(result, dict):
        for key in (
            "success",
            "name",
            "description",
            "tags",
            "related_skills",
            "path",
            "skill_dir",
            "readiness_status",
        ):
            if key in result:
                payload[key] = result[key]
        metadata = result.get("metadata")
        if metadata:
            payload["metadata"] = metadata
    else:
        payload["name"] = _skill_name_from_event(args, result)
    return "# Autopilot skill metadata\n\n" f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n"


def on_post_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int | None = None,
    **_: Any,
) -> None:
    """post_tool_call hook: best-effort evidence capture, fail-open."""
    try:
        decision = evaluate_tool_event(
            tool_name=tool_name,
            result=result,
            task_id=task_id,
            session_id=session_id,
        )
        if not decision.should_capture:
            return

        _ensure_canvas(decision.canvas_id, task_id=task_id, session_id=session_id)
        store = _store()
        if tool_name == "skill_view" and decision.reason == "skill_view_metadata":
            evidence = _skill_metadata_text(
                args=args,
                result=result,
                reason=decision.reason,
                duration_ms=duration_ms,
            )
        else:
            evidence = _evidence_text(
                tool_name=tool_name,
                args=args,
                result=result,
                reason=decision.reason,
                duration_ms=duration_ms,
            )
        ref = store.add_ref(
            decision.canvas_id,
            evidence,
            label=f"autopilot {tool_name}",
            source="context-canvas-autopilot post_tool_call",
            kind="tool-evidence",
        )["ref"]
        if tool_name == "skill_view" and decision.reason == "skill_view_metadata":
            summary = f"Loaded relevant skill: {_skill_name_from_event(args, result)}"
        else:
            summary = f"Captured {tool_name} via {decision.reason}"
        if tool_call_id:
            summary += f" ({tool_call_id})"
        store.upsert_node(
            decision.canvas_id,
            kind="action",
            status="done",
            summary=summary,
            refs=[ref],
        )
    except Exception as exc:  # fail-open: hooks must never break tool execution
        logger.debug("context-canvas-autopilot hook failed: %s", exc)


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", on_post_tool_call)
