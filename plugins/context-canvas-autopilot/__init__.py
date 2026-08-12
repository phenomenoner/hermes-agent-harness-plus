"""Retired Context Canvas Autopilot v2 broad-capture experiment.

Runtime contract:

- production runtime configuration is forced to ``off``;
- historical active modes remain available only to source replay and regression
  tests through ``set_test_config``;
- the retained implementation can still validate its former sanitized snapshot,
  semantic projection, and reverse-shadow contracts without authorizing live
  payload capture;
- hooks are fail-open and never mutate model-visible tool results.

Behavioral settings belong under
``plugins.entries.context-canvas-autopilot`` in ``config.yaml``. Existing
HERMES_CONTEXT_CANVAS_* behavior variables remain accepted only by the legacy
policy as a backward-compatibility fallback. ``HERMES_CONTEXT_CANVAS_TOOL`` is
still supported as an installation-path mechanism.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import importlib
import json
import logging
import os
import queue
import re
import stat
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

PLUGIN_REVISION = "0.2.5-retired-broad-capture"
_RUNTIME_MODES = {"off"}
_HISTORICAL_TEST_MODES = {
    "off",
    "v2_active",
    "v2_active_legacy_shadow",
    "legacy_active_safe",
}
_MAX_REDACTION_PASSES = 4
_DATA_URL_RE = re.compile(
    r"data:(?P<mime>[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+);base64,(?P<data>[^\s\"'<>]*)",
    re.IGNORECASE,
)
_SAFE_VALUE_RE = re.compile(r"[^A-Za-z0-9_.:-]+")

# This intentionally models the exact v1 exclusion behavior for shadow parity.
_LEGACY_EXCLUDED_PREFIXES = ("mcp_context_canvas_", "canvas_")
_LEGACY_EXCLUDED_TOOLS = {"todo", "memory", "session_search"}
_LEGACY_HIGH_SIGNAL_TOOLS = {
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

_MUTATION_TOOLS = {
    "patch",
    "write_file",
    "skill_manage",
    "cronjob",
    "memory",
    "image_generate",
    "mcp_mempalace_mempalace_add_drawer",
    "mcp_mempalace_mempalace_checkpoint",
    "mcp_mempalace_mempalace_update_drawer",
    "mcp_mempalace_mempalace_delete_drawer",
    "mcp_mempalace_mempalace_kg_add",
    "mcp_mempalace_mempalace_kg_invalidate",
}
_VERIFICATION_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:pytest|python\s+-m\s+pytest|ruff|mypy|pyright|npm\s+test|pnpm\s+test|"
    r"npm\s+run\s+(?:test|lint|build)|pnpm\s+(?:test|lint|build)|cargo\s+test|go\s+test|"
    r"git\s+diff\s+--check|hermes\s+mcp\s+test|python\s+-m\s+compileall)(?:\s|$)",
    re.IGNORECASE,
)
_MUTATION_COMMAND_RE = re.compile(
    r"(?:git\s+(?:commit|push|merge|rebase)|(?:npm|pnpm)\s+(?:publish|deploy)|"
    r"terraform\s+apply|kubectl\s+(?:apply|delete)|docker\s+(?:compose\s+)?(?:up|down)|"
    r"hermes\s+(?:plugins\s+(?:enable|disable)|gateway\s+(?:start|stop|restart)))",
    re.IGNORECASE,
)

_state_lock = threading.RLock()

_legacy_counts: dict[str, int] = {}
_legacy_active: set[str] = set()
_semantic_counts: dict[tuple[str, str], int] = {}
_captured_sessions: set[str] = set()
_event_sequence = 0
_test_config: dict[str, Any] | None = None
_resolved_tool_root: Path | None = None
_worker_guard = threading.Lock()
_write_queue: queue.Queue["QueuedToolEvent"] | None = None
_worker_threads: list[threading.Thread] = []
_atexit_registered = False


class PolicyDecision(NamedTuple):
    should_capture: bool
    reason: str = ""
    canvas_id: str = ""


@dataclass(frozen=True)
class PluginConfig:
    mode: str
    revision: str
    cache_root: Path
    metrics_root: Path
    retention_class: str
    retention_days: int
    max_semantic_refs: int
    legacy_tool_threshold: int
    legacy_large_result_chars: int
    legacy_max_ref_chars: int
    metrics_enabled: bool
    require_hermes_redactor: bool
    async_writes: bool
    queue_maxsize: int
    worker_count: int
    flush_timeout_seconds: int


@dataclass(frozen=True)
class QueuedToolEvent:
    """Immutable hook receipt passed to async workers.

    Legacy policy state is resolved before this value is queued. Workers only
    persist the already ordered decision; they never re-run the stateful
    evaluator while scheduling is concurrent.
    """

    cfg: PluginConfig
    queued_at: float
    queue_depth: int
    hook_ms: float
    event_sequence: int
    legacy: PolicyDecision
    tool_name: str
    args: Any
    result: Any
    task_id: str
    session_id: str
    tool_call_id: str
    duration_ms: int | None
    status: str
    error_type: str


def reset_state_for_tests() -> None:
    global _event_sequence, _resolved_tool_root, _test_config
    with _state_lock:
        _legacy_counts.clear()
        _legacy_active.clear()
        _semantic_counts.clear()
        _captured_sessions.clear()
        _event_sequence = 0
        _test_config = None
        _resolved_tool_root = None


def set_test_config(config: dict[str, Any] | None) -> None:
    global _resolved_tool_root, _test_config
    _test_config = dict(config) if config is not None else None
    _resolved_tool_root = None


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _int_value(value: Any, default: int, *, minimum: int = 1, maximum: int = 10_000_000) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _bool_value(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _bounded_token(value: Any, default: str, *, limit: int = 96) -> str:
    text = _SAFE_VALUE_RE.sub("-", str(value or default)).strip("-._:")
    return (text[:limit] or default)


def _load_entry_config() -> dict[str, Any]:
    if _test_config is not None:
        return dict(_test_config)
    try:
        try:
            from hermes_cli.config import load_config_readonly as load_config  # type: ignore[import-not-found]
        except ImportError:
            from hermes_cli.config import load_config  # type: ignore[import-not-found]

        cfg = load_config() or {}
        plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get("context-canvas-autopilot") if isinstance(entries, dict) else None
        return dict(entry) if isinstance(entry, dict) else {}
    except Exception as exc:
        logger.debug("context-canvas-autopilot could not load config: %s", type(exc).__name__)
        return {}


def _config() -> PluginConfig:
    entry = _load_entry_config()
    home = _hermes_home()
    requested_mode = str(entry.get("mode", "off")).strip()
    allowed_modes = _HISTORICAL_TEST_MODES if _test_config is not None else _RUNTIME_MODES
    mode = requested_mode if requested_mode in allowed_modes else "off"
    cache_root = Path(entry.get("cache_root") or home / "context-canvas-cache-v2").expanduser()
    metrics_root = Path(entry.get("metrics_root") or home / "context-canvas-soak" / "v2-active-legacy-shadow").expanduser()

    # Existing environment variables are compatibility fallbacks only. New
    # installations and docs use config.yaml.
    legacy_tool_threshold = entry.get("legacy_tool_threshold", os.getenv("HERMES_CONTEXT_CANVAS_TOOL_THRESHOLD", 5))
    legacy_large = entry.get("legacy_large_result_chars", os.getenv("HERMES_CONTEXT_CANVAS_LARGE_RESULT_CHARS", 6000))
    legacy_max = entry.get("legacy_max_ref_chars", os.getenv("HERMES_CONTEXT_CANVAS_MAX_REF_CHARS", 50000))
    return PluginConfig(
        mode=mode,
        revision=_bounded_token(entry.get("revision"), PLUGIN_REVISION),
        cache_root=cache_root,
        metrics_root=metrics_root,
        retention_class=_bounded_token(entry.get("retention_class"), "ephemeral-cache"),
        retention_days=_int_value(entry.get("retention_days"), 30, maximum=3650),
        max_semantic_refs=_int_value(entry.get("max_semantic_refs"), 12, maximum=100),
        legacy_tool_threshold=_int_value(legacy_tool_threshold, 5, maximum=1000),
        legacy_large_result_chars=_int_value(legacy_large, 6000, maximum=100_000_000),
        legacy_max_ref_chars=_int_value(legacy_max, 50000, maximum=100_000_000),
        metrics_enabled=_bool_value(entry.get("metrics_enabled"), True),
        require_hermes_redactor=_bool_value(entry.get("require_hermes_redactor"), True),
        async_writes=_bool_value(entry.get("async_writes"), True),
        queue_maxsize=_int_value(entry.get("queue_maxsize"), 256, maximum=4096),
        worker_count=_int_value(entry.get("worker_count"), 8, maximum=32),
        flush_timeout_seconds=_int_value(entry.get("flush_timeout_seconds"), 30, maximum=600),
    )


def _validated_tool_root(value: Any) -> Path | None:
    """Return a POSIX-owner-controlled absolute root with the required package."""
    if not isinstance(value, (str, os.PathLike)):
        return None
    try:
        raw_value = os.fspath(value)
        if not isinstance(raw_value, str) or not raw_value.strip():
            return None
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            return None
        candidate_stat = candidate.lstat()
        if not stat.S_ISDIR(candidate_stat.st_mode):
            return None
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None

    get_euid = getattr(os, "geteuid", None)
    if os.name != "posix" or not callable(get_euid):
        return None
    owner = get_euid()
    package = root / "context_canvas"
    required = tuple(package / name for name in ("__init__.py", "core.py", "snapshot.py"))
    try:
        code_paths = ((root, stat.S_ISDIR), (package, stat.S_ISDIR), *((path, stat.S_ISREG) for path in required))
        for path, expected_kind in code_paths:
            path_stat = path.lstat()
            if not expected_kind(path_stat.st_mode):
                return None
            if path_stat.st_uid != owner or path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                return None

        # Validate every replacement-capable ancestor through the filesystem
        # root. Sticky directories may be writable, but only while they protect
        # a direct child owned by this EUID or by root.
        child_owner = owner
        filesystem_root_seen = False
        for ancestor in root.parents:
            ancestor_stat = ancestor.lstat()
            if not stat.S_ISDIR(ancestor_stat.st_mode):
                return None
            if ancestor_stat.st_uid not in {owner, 0}:
                return None
            writable_by_others = bool(ancestor_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
            if writable_by_others:
                if not ancestor_stat.st_mode & stat.S_ISVTX or child_owner not in {owner, 0}:
                    return None
            if ancestor.parent == ancestor:
                if ancestor_stat.st_uid != 0 or writable_by_others:
                    return None
                filesystem_root_seen = True
            child_owner = ancestor_stat.st_uid
        if not filesystem_root_seen:
            return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return root


def _context_canvas_modules() -> dict[str, Any]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "context_canvas" or name.startswith("context_canvas.")
    }


def _module_is_from_package(module: Any, package: Path) -> bool:
    spec = getattr(module, "__spec__", None)
    origins = {
        origin
        for origin in (getattr(module, "__file__", None), getattr(spec, "origin", None))
        if origin
    }
    if not origins:
        return False
    try:
        return all(Path(origin).resolve(strict=True).is_relative_to(package) for origin in origins)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _evict_context_canvas_modules() -> None:
    for name in _context_canvas_modules():
        sys.modules.pop(name, None)


def _activate_tool_root(root: Path) -> None:
    """Give the selected root precedence and reject cached foreign modules."""
    root_text = str(root)
    sys.path[:] = [entry for entry in sys.path if entry != root_text]
    sys.path.insert(0, root_text)
    importlib.invalidate_caches()

    package = (root / "context_canvas").resolve(strict=True)
    loaded = _context_canvas_modules()
    if any(not _module_is_from_package(module, package) for module in loaded.values()):
        _evict_context_canvas_modules()


def _assert_context_canvas_origins(root: Path) -> None:
    package = (root / "context_canvas").resolve(strict=True)
    loaded = _context_canvas_modules()
    required = {"context_canvas", "context_canvas.core", "context_canvas.snapshot"}
    if not required.issubset(loaded):
        missing = ", ".join(sorted(required.difference(loaded)))
        raise ImportError(f"trusted context_canvas import incomplete: {missing}")
    foreign = [name for name, module in loaded.items() if not _module_is_from_package(module, package)]
    if foreign:
        raise ImportError(f"context_canvas import escaped trusted root: {', '.join(sorted(foreign))}")


def _ensure_context_canvas_importable() -> Path | None:
    """Resolve and activate a currently valid trusted package root."""
    global _resolved_tool_root
    with _state_lock:
        if _resolved_tool_root is not None:
            cached_root = _validated_tool_root(_resolved_tool_root)
            if cached_root is not None and cached_root == _resolved_tool_root:
                _activate_tool_root(cached_root)
                return cached_root
            _resolved_tool_root = None

        # A valid explicit process override wins without parsing config.
        env_root = _validated_tool_root(os.getenv("HERMES_CONTEXT_CANVAS_TOOL"))
        if env_root is not None:
            _activate_tool_root(env_root)
            _resolved_tool_root = env_root
            return env_root

        configured_tool_root = _load_entry_config().get("tool_root")
        candidates: tuple[Any, ...] = (
            configured_tool_root,
            _hermes_home() / "context-canvas-tool",
            Path.home() / ".hermes" / "context-canvas-tool",
            Path(__file__).resolve().parents[2] / "packages" / "context-canvas",
        )
        for candidate in candidates:
            root = _validated_tool_root(candidate)
            if root is None:
                continue
            _activate_tool_root(root)
            _resolved_tool_root = root
            return root
    return None


def _components():
    with _state_lock:
        root = _ensure_context_canvas_importable()
        if root is None:
            raise RuntimeError("no trusted context_canvas tool root is available")
        importlib.invalidate_caches()
        try:
            from context_canvas.core import CanvasStore  # type: ignore[import-not-found]
            from context_canvas.snapshot import PrivateJsonlLedger, SnapshotStore, now_iso  # type: ignore[import-not-found]

            _assert_context_canvas_origins(root)
        except Exception:
            _evict_context_canvas_modules()
            raise
        return CanvasStore, SnapshotStore, PrivateJsonlLedger, now_iso


def normalize_tool_name(tool_name: str) -> str:
    normalized = str(tool_name or "").strip().replace("__", "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized


def _is_context_canvas_tool(tool_name: str) -> bool:
    raw = str(tool_name or "").strip()
    normalized = normalize_tool_name(raw)
    return (
        raw.startswith("mcp__context_canvas__")
        or normalized.startswith("mcp_context_canvas_")
        or normalized.startswith("canvas_")
        or normalized.startswith("context_canvas_")
    )


def _tracker_key(task_id: str = "", session_id: str = "") -> str:
    return str(session_id or task_id or "default")


def _legacy_safe_session_key(task_id: str = "", session_id: str = "") -> str:
    key = _tracker_key(task_id, session_id)
    safe = _SAFE_VALUE_RE.sub("-", key).strip("-._:")
    return safe[:96] or "default"


def _safe_session_key(
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
) -> tuple[str, bool]:
    canonical = str(session_id or task_id)
    identity_unknown = not bool(canonical)
    if not canonical:
        canonical = f"tool-call:{tool_call_id}" if tool_call_id else f"unscoped:{uuid.uuid4().hex}"
    slug = _bounded_token(canonical, "session", limit=44)
    digest = hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    return f"{slug}-{digest}", identity_unknown


def _event_id(session_key: str, tool_call_id: str) -> tuple[str, bool]:
    if tool_call_id:
        material = f"{session_key}\x00{tool_call_id}"
        unknown = False
    else:
        material = f"{session_key}\x00unscoped-event:{uuid.uuid4().hex}"
        unknown = True
    return hashlib.sha256(material.encode("utf-8", errors="surrogatepass")).hexdigest(), unknown


def _canvas_id(session_key: str) -> str:
    return f"auto-v2-{session_key[:68]}"


def _legacy_is_excluded(tool_name: str) -> bool:
    if not tool_name or tool_name in _LEGACY_EXCLUDED_TOOLS:
        return True
    return any(tool_name.startswith(prefix) for prefix in _LEGACY_EXCLUDED_PREFIXES)


def _serialize(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        return "text-verbatim", value
    try:
        return "json-normalized", json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return "repr", repr(value)


def evaluate_legacy_event(
    *,
    tool_name: str,
    result_chars: int,
    task_id: str = "",
    session_id: str = "",
    config: PluginConfig | None = None,
) -> PolicyDecision:
    """Pure v1 policy simulation used by the reverse-shadow comparator."""
    cfg = config or _config()
    if _legacy_is_excluded(tool_name):
        return PolicyDecision(False, "excluded_tool")
    key = _tracker_key(task_id, session_id)
    legacy_canvas_id = "auto-" + _legacy_safe_session_key(task_id, session_id)[:72]
    with _state_lock:
        count = _legacy_counts.get(key, 0) + 1
        _legacy_counts[key] = count
        already_active = legacy_canvas_id in _legacy_active
    if tool_name == "skill_view":
        if already_active:
            return PolicyDecision(True, "skill_view_metadata", legacy_canvas_id)
        return PolicyDecision(False, f"skill_view_no_active_canvas:{count}", legacy_canvas_id)
    if result_chars >= cfg.legacy_large_result_chars:
        return PolicyDecision(True, f"large_result:{result_chars}", legacy_canvas_id)
    if count >= cfg.legacy_tool_threshold:
        return PolicyDecision(True, f"tool_threshold:{count}", legacy_canvas_id)
    if already_active and tool_name in _LEGACY_HIGH_SIGNAL_TOOLS:
        return PolicyDecision(True, "active_high_signal_tool", legacy_canvas_id)
    return PolicyDecision(False, f"below_threshold:{count}", legacy_canvas_id)


def _mark_legacy_active(decision: PolicyDecision) -> None:
    if decision.should_capture and decision.canvas_id:
        with _state_lock:
            _legacy_active.add(decision.canvas_id)


def _prepare_legacy_event(
    *,
    cfg: PluginConfig,
    tool_name: str,
    result: Any,
    task_id: str,
    session_id: str,
) -> tuple[int, PolicyDecision, str | None]:
    """Assign receive order and resolve legacy state before queueing.

    The lock covers sequence allocation, the stateful evaluator, and the
    active-canvas transition. This makes the decision stream independent of
    worker scheduling while keeping the event payload content-free in its
    metrics representation.
    """
    global _event_sequence
    with _state_lock:
        _event_sequence += 1
        event_sequence = _event_sequence
        if cfg.mode not in {"v2_active_legacy_shadow", "legacy_active_safe"}:
            return event_sequence, PolicyDecision(False, "disabled"), None
        result_text = result if isinstance(result, str) else _serialize(result)[1]
        decision = evaluate_legacy_event(
            tool_name=tool_name,
            result_chars=len(result_text),
            task_id=task_id,
            session_id=session_id,
            config=cfg,
        )
        _mark_legacy_active(decision)
        return event_sequence, decision, result_text


def _force_redact_text(text: str, *, tool_name: str, required: bool) -> tuple[str, bool, str]:
    try:
        from agent.redact import redact_sensitive_text  # type: ignore[import-not-found]

        redacted = redact_sensitive_text(
            text,
            force=True,
            file_read=normalize_tool_name(tool_name) in {"read_file", "search_files"},
            redact_url_credentials=True,
        )
        return redacted, redacted != text, "hermes_force"
    except Exception as exc:
        if required:
            raise RuntimeError("required Hermes force-redactor unavailable") from exc
        return text, False, "disabled-test-only"


def _redact_to_fixed_point(text: str, *, tool_name: str, required: bool) -> tuple[str, bool, str]:
    """Apply the selected redactor until another pass cannot change persisted text."""
    original = text
    current = text
    backend = ""
    seen = {current}
    for _ in range(_MAX_REDACTION_PASSES):
        redacted, _, pass_backend = _force_redact_text(
            current,
            tool_name=tool_name,
            required=required,
        )
        if backend and pass_backend != backend:
            raise RuntimeError("redactor backend changed before reaching a fixed point")
        backend = pass_backend
        if redacted == current:
            return redacted, redacted != original, backend
        if redacted in seen:
            raise RuntimeError("redactor entered a cycle before reaching a fixed point")
        seen.add(redacted)
        current = redacted
    raise RuntimeError("redactor did not reach a fixed point within the bounded pass limit")


def _externalize_data_urls(
    text: str,
    snapshot_store: Any,
) -> tuple[str, list[dict[str, Any]], int, int]:
    embedded: list[dict[str, Any]] = []
    storage_errors = 0
    invalid_removed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal invalid_removed, storage_errors
        mime = _bounded_token(match.group("mime"), "application-octet-stream", limit=80).replace("-", "/", 1)
        encoded = match.group("data")
        try:
            raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except Exception:
            invalid_removed += 1
            digest = hashlib.sha256(encoded.encode("ascii", errors="ignore")).hexdigest()
            # Invalid caller input was safely removed. It is not a persistence
            # failure and therefore must not trip the storage hard gate.
            return f"[invalid-data-url-removed sha256={digest} encoded_chars={len(encoded)}]"
        try:
            stored = snapshot_store.put_binary(raw)
            item = {
                "sha256": stored["sha256"],
                "mime": mime,
                "raw_bytes": stored["raw_bytes"],
                "stored_bytes": stored["stored_bytes"],
                "object_relpath": stored["object_relpath"],
                "reused": stored["reused"],
            }
            embedded.append(item)
            return f"[context-canvas-binary sha256={stored['sha256']} mime={mime} bytes={len(raw)}]"
        except Exception:
            storage_errors += 1
            digest = hashlib.sha256(encoded.encode("ascii", errors="ignore")).hexdigest()
            # Never retain binary text after a storage failure; preserve only a
            # content-free receipt while the metric keeps the hard failure.
            return f"[data-url-storage-failed-removed sha256={digest} encoded_chars={len(encoded)}]"

    return _DATA_URL_RE.sub(replace, text), embedded, storage_errors, invalid_removed


def _status_from_result(
    result: Any,
    result_text: str,
    *,
    observer_status: str = "",
    observer_error_type: str = "",
) -> tuple[str, str]:
    observed = str(observer_status or "").strip().lower()
    if observed in {"error", "failed", "failure", "blocked"} or str(observer_error_type or "").strip():
        reason = _bounded_token(observer_error_type, "observer_error", limit=80)
        return "error", reason
    candidate = result
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith(("{", "[")) and len(stripped) <= 2_000_000:
            try:
                candidate = json.loads(stripped)
            except Exception:
                candidate = result
    if isinstance(candidate, dict):
        if candidate.get("success") is False or candidate.get("ok") is False:
            return "error", "reported_failure"
        if candidate.get("error") not in (None, "", False):
            return "error", "error_field"
        exit_code = candidate.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
            return "error", "nonzero_exit"
        if isinstance(exit_code, str) and exit_code.strip():
            try:
                if int(exit_code.strip()) != 0:
                    return "error", "nonzero_exit"
            except ValueError:
                pass
        status = str(candidate.get("status", "")).lower()
        if status in {"error", "failed", "failure", "blocked"}:
            return "error", "failure_status"
    if re.search(r"(?:->\s*exit|exit[_ ]code[\"':= ]+)\s*[1-9]\d*", result_text, re.IGNORECASE):
        return "error", "nonzero_exit_text"
    if "Traceback (most recent call last)" in result_text:
        return "error", "traceback"
    return "ok", ""


def _args_command(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def _semantic_class(tool_name: str, args: Any, status: str) -> str:
    normalized = normalize_tool_name(tool_name)
    if status != "ok":
        return "failure"
    command = _args_command(args)
    if normalized in {"terminal", "execute_code"} and _VERIFICATION_COMMAND_RE.search(command):
        return "verification"
    if normalized in _MUTATION_TOOLS:
        return "action"
    if normalized in {"terminal", "execute_code"} and _MUTATION_COMMAND_RE.search(command):
        return "action"
    if normalized == "delegate_task":
        return "action"
    return "none"


def _semantic_spec(semantic_class: str) -> tuple[str, str, str]:
    if semantic_class == "failure":
        return "AUTO_V2_FAILURES", "blocked", "blocked"
    if semantic_class == "verification":
        return "AUTO_V2_VERIFICATIONS", "verification", "done"
    return "AUTO_V2_ACTIONS", "action", "done"


def _ensure_canvas(canvas_id: str, session_key: str, cfg: PluginConfig) -> Any:
    CanvasStore, _, _, _ = _components()
    store = CanvasStore()
    try:
        store.read(canvas_id)
    except FileNotFoundError:
        store.start(
            goal=f"Selective semantic projection for Context Canvas v2 cache session {session_key}.",
            session_id=canvas_id,
            title=f"Autopilot v2: {session_key}",
            metadata={
                "mode": "v2_active",
                "source_session_id": session_key,
                "plugin_revision": cfg.revision,
                "lifecycle": "active",
                "cache_root": str(cfg.cache_root),
            },
        )
    return store


def _promote_snapshot(
    *,
    cfg: PluginConfig,
    session_key: str,
    canvas_id: str,
    tool_name: str,
    semantic_class: str,
    status_reason: str,
    snapshot: dict[str, Any],
    object_info: dict[str, Any],
    result_excerpt: str,
) -> str:
    node_id, node_kind, node_status = _semantic_spec(semantic_class)
    with _state_lock:
        counter_key = (session_key, semantic_class)
        count = _semantic_counts.get(counter_key, 0) + 1
        _semantic_counts[counter_key] = count
    store = _ensure_canvas(canvas_id, session_key, cfg)
    cleaned_excerpt = result_excerpt[:800].replace("```", "` ` `")
    evidence = (
        f"snapshot_id: {snapshot['snapshot_id']}\n"
        f"manifest: {snapshot['manifest_path']}\n"
        f"object_sha256: {object_info['sha256']}\n"
        f"tool_name: {normalize_tool_name(tool_name)}\n"
        f"semantic_class: {semantic_class}\n"
        f"status_reason: {status_reason or 'none'}\n"
        f"full_snapshot: sanitized invocation/result envelope in the manifest-linked object\n\n"
        f"excerpt:\n{cleaned_excerpt}"
    )
    summary = f"{count} {semantic_class} event(s); latest {normalize_tool_name(tool_name)} -> {snapshot['snapshot_id']}"
    recorded = store.record_evidence_node(
        canvas_id,
        content=evidence,
        label=f"v2 {semantic_class} snapshot",
        source="context-canvas-autopilot v2 active projection",
        ref_kind="snapshot-manifest-pointer",
        node_kind=node_kind,
        node_status=node_status,
        node_summary=summary[:150],
        node_id=node_id,
        max_refs=cfg.max_semantic_refs,
    )
    return str(recorded["ref"])


def _session_hash(session_key: str) -> str:
    return hashlib.sha256(session_key.encode("utf-8", errors="replace")).hexdigest()[:20]


def _append_metric(cfg: PluginConfig, record: dict[str, Any]) -> None:
    if not cfg.metrics_enabled:
        return
    _, _, PrivateJsonlLedger, _ = _components()
    durable = bool(
        (record.get("active_capture_attempted") and not record.get("active_capture_ok"))
        or record.get("active_error_type")
        or record.get("active_semantic_error_type")
    )
    PrivateJsonlLedger(cfg.metrics_root).append(record, durable=durable)


def _flush_metrics(cfg: PluginConfig) -> None:
    if not cfg.metrics_enabled:
        return
    _, _, PrivateJsonlLedger, _ = _components()
    PrivateJsonlLedger(cfg.metrics_root).flush()


def _metric_base(
    *,
    cfg: PluginConfig,
    session_key: str,
    event_id: str,
    event_sequence: int,
    identity_unknown: bool,
    event_identity_unknown: bool,
    tool_name: str,
    result_chars: int,
    legacy: PolicyDecision,
) -> dict[str, Any]:
    _, _, _, now_iso = _components()
    legacy_bytes = min(result_chars, cfg.legacy_max_ref_chars) + 800 if legacy.should_capture else 0
    return {
        "schema_version": 2,
        "revision": cfg.revision,
        "ts": now_iso(),
        "mode": cfg.mode,
        "session_hash": _session_hash(session_key),
        "event_id": event_id,
        "event_sequence": max(0, int(event_sequence)),
        "identity_unknown": identity_unknown,
        "event_identity_unknown": event_identity_unknown,
        "tool_name": normalize_tool_name(tool_name)[:120],
        "result_chars": max(0, result_chars),
        "active_excluded": False,
        "self_capture_excluded": False,
        "active_capture_attempted": False,
        "active_capture_ok": False,
        "active_error_type": "",
        "active_event_duplicate": False,
        "active_snapshot_id": "",
        "active_object_raw_bytes": 0,
        "active_object_stored_bytes": 0,
        "active_embedded_raw_bytes": 0,
        "active_embedded_stored_bytes": 0,
        "active_manifest_bytes": 0,
        "active_object_reused": False,
        "active_redaction_applied": False,
        "active_redactor_backend": "",
        "active_embedded_objects": 0,
        "active_externalization_errors": 0,
        "active_semantic_class": "none",
        "active_semantic_promoted": False,
        "active_semantic_ref": "",
        "active_semantic_error_type": "",
        "legacy_shadow_capture": legacy.should_capture,
        "legacy_shadow_reason": legacy.reason[:120],
        "legacy_shadow_estimated_bytes": legacy_bytes,
        "legacy_shadow_estimated_nodes": 1 if legacy.should_capture else 0,
        "replacement_applied": False,
        "async_write": cfg.async_writes,
        "queue_depth": 0,
        "queue_wait_ms": 0.0,
        "persist_ms": 0.0,
        "hook_ms": 0.0,
    }


def _process_tool_event(
    *,
    cfg: PluginConfig,
    hook_ms: float,
    queue_wait_ms: float,
    queue_depth: int,
    event_sequence: int = 0,
    legacy: PolicyDecision | None = None,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int | None = None,
    status: str = "",
    error_type: str = "",
    **_: Any,
) -> None:
    """Persist one active v2 snapshot and one content-free v1 shadow decision."""
    persist_started = time.perf_counter()
    session_key, identity_unknown = _safe_session_key(task_id, session_id, tool_call_id)
    event_id, event_identity_unknown = _event_id(session_key, tool_call_id)
    result_format, result_text = _serialize(result)
    if legacy is None:
        legacy = PolicyDecision(False, "disabled")
    metric = _metric_base(
        cfg=cfg,
        session_key=session_key,
        event_id=event_id,
        event_sequence=event_sequence,
        identity_unknown=identity_unknown,
        event_identity_unknown=event_identity_unknown,
        tool_name=tool_name,
        result_chars=len(result_text),
        legacy=legacy,
    )
    metric["hook_ms"] = 0.0 if hook_ms < 0 else round(hook_ms, 3)
    metric["queue_wait_ms"] = round(max(0.0, queue_wait_ms), 3)
    metric["queue_depth"] = max(0, int(queue_depth))
    if _is_context_canvas_tool(tool_name):
        metric["active_excluded"] = True
        metric["self_capture_excluded"] = True
        metric["persist_ms"] = round((time.perf_counter() - persist_started) * 1000, 3)
        if hook_ms < 0:
            metric["hook_ms"] = metric["persist_ms"]
        try:
            _append_metric(cfg, metric)
        except Exception as exc:
            logger.debug("context-canvas-autopilot metric append failed: %s", type(exc).__name__)
        return

    metric["active_capture_attempted"] = True
    try:
        with nullcontext():
            _, SnapshotStore, _, _ = _components()
            snapshot_store = SnapshotStore(cfg.cache_root)
            args_format, args_text = _serialize(args or {})
            (
                args_no_binary,
                args_embedded,
                args_externalization_errors,
                args_invalid_data_urls,
            ) = _externalize_data_urls(args_text, snapshot_store)
            (
                result_no_binary,
                result_embedded,
                result_externalization_errors,
                result_invalid_data_urls,
            ) = _externalize_data_urls(result_text, snapshot_store)
            args_redacted, args_changed, args_redactor = _redact_to_fixed_point(
                args_no_binary,
                tool_name=tool_name,
                required=cfg.require_hermes_redactor,
            )
            result_redacted, result_changed, result_redactor = _redact_to_fixed_point(
                result_no_binary,
                tool_name=tool_name,
                required=cfg.require_hermes_redactor,
            )
            if args_redactor != result_redactor:
                raise RuntimeError("redactor backend mismatch")
            embedded = [*args_embedded, *result_embedded]
            capture_status, status_reason = _status_from_result(
                result,
                result_redacted,
                observer_status=status,
                observer_error_type=error_type,
            )
            envelope = {
                "schema_version": 2,
                "revision": cfg.revision,
                "tool_name": normalize_tool_name(tool_name),
                "args_format": args_format,
                "args": args_redacted,
                "result_format": result_format,
                "result": result_redacted,
                "embedded_object_digests": [item["sha256"] for item in embedded],
            }
            object_info = snapshot_store.put_envelope(envelope)
            semantic_class = _semantic_class(tool_name, args, capture_status)
            if cfg.mode == "legacy_active_safe":
                semantic_class = "action" if legacy.should_capture else "none"
            if identity_unknown:
                semantic_class = "none"
            manifest = {
                "revision": cfg.revision,
                "event_id": event_id,
                "identity_unknown": identity_unknown,
                "event_identity_unknown": event_identity_unknown,
                "source_session_hash": _session_hash(session_key),
                "tool_name": normalize_tool_name(tool_name)[:120],
                "tool_call_id": _bounded_token(tool_call_id, "none", limit=120),
                "duration_ms": duration_ms if isinstance(duration_ms, int) and duration_ms >= 0 else None,
                "status": capture_status,
                "status_reason": status_reason,
                "object_sha256": object_info["sha256"],
                "object_relpath": object_info["object_relpath"],
                "object_raw_bytes": object_info["raw_bytes"],
                "object_stored_bytes": object_info["stored_bytes"],
                "object_reused": object_info["reused"],
                "redaction_applied": args_changed or result_changed,
                "redactor_backend": result_redactor,
                "embedded_objects": embedded,
                "externalization_errors": args_externalization_errors + result_externalization_errors,
                "invalid_data_urls_removed": args_invalid_data_urls + result_invalid_data_urls,
                "retention_class": cfg.retention_class,
                "retention_days": cfg.retention_days,
                "pinned": semantic_class != "none",
                "semantic_class": semantic_class,
                "legacy_shadow_capture": legacy.should_capture,
                "legacy_shadow_reason": legacy.reason[:120],
                "full_snapshot_is_sanitized": True,
                "raw_unsanitized_copy_persisted": False,
            }
            snapshot = snapshot_store.record_manifest(session_key, manifest)
            duplicate_event = bool(snapshot.get("duplicate_event"))
            with _state_lock:
                _captured_sessions.add(session_key)
            metric.update(
                {
                    "active_capture_ok": True,
                    "active_event_duplicate": duplicate_event,
                    "active_snapshot_id": snapshot["snapshot_id"],
                    "active_object_raw_bytes": object_info["raw_bytes"],
                    "active_object_stored_bytes": object_info["stored_bytes"],
                    "active_embedded_raw_bytes": sum(item["raw_bytes"] for item in embedded),
                    "active_embedded_stored_bytes": sum(
                        item["stored_bytes"] for item in embedded if not item["reused"]
                    ),
                    "active_manifest_bytes": snapshot["manifest_bytes"],
                    "active_object_reused": object_info["reused"],
                    "active_redaction_applied": args_changed or result_changed,
                    "active_redactor_backend": result_redactor,
                    "active_embedded_objects": len(embedded),
                    "active_externalization_errors": args_externalization_errors + result_externalization_errors,
                    "active_semantic_class": semantic_class,
                }
            )
            if semantic_class != "none" and not duplicate_event:
                try:
                    semantic_ref = _promote_snapshot(
                        cfg=cfg,
                        session_key=session_key,
                        canvas_id=_canvas_id(session_key),
                        tool_name=tool_name,
                        semantic_class=semantic_class,
                        status_reason=status_reason,
                        snapshot=snapshot,
                        object_info=object_info,
                        result_excerpt=result_redacted,
                    )
                    metric["active_semantic_promoted"] = bool(semantic_ref)
                    metric["active_semantic_ref"] = semantic_ref[:120]
                except Exception as exc:
                    metric["active_semantic_error_type"] = type(exc).__name__[:80]
                    logger.debug("context-canvas-autopilot semantic promotion failed: %s", type(exc).__name__)
    except Exception as exc:  # fail-open: never affect the tool result
        metric["active_error_type"] = type(exc).__name__[:80]
        logger.debug("context-canvas-autopilot v2 capture failed: %s", type(exc).__name__)
    finally:
        metric["persist_ms"] = round((time.perf_counter() - persist_started) * 1000, 3)
        if hook_ms < 0:
            metric["hook_ms"] = metric["persist_ms"]
        try:
            _append_metric(cfg, metric)
        except Exception as exc:
            logger.debug("context-canvas-autopilot metric append failed: %s", type(exc).__name__)


def _worker_loop(work_queue: queue.Queue[QueuedToolEvent]) -> None:
    while True:
        task = work_queue.get()
        try:
            _process_tool_event(
                cfg=task.cfg,
                hook_ms=task.hook_ms,
                queue_wait_ms=(time.perf_counter() - task.queued_at) * 1000,
                queue_depth=task.queue_depth,
                event_sequence=task.event_sequence,
                legacy=task.legacy,
                tool_name=task.tool_name,
                args=task.args,
                result=task.result,
                task_id=task.task_id,
                session_id=task.session_id,
                tool_call_id=task.tool_call_id,
                duration_ms=task.duration_ms,
                status=task.status,
                error_type=task.error_type,
            )
        except Exception as exc:
            logger.debug("context-canvas-autopilot worker failed: %s", type(exc).__name__)
        finally:
            work_queue.task_done()


def _ensure_worker(cfg: PluginConfig) -> queue.Queue[QueuedToolEvent]:
    global _write_queue, _worker_threads
    with _worker_guard:
        if _write_queue is not None and _worker_threads and all(thread.is_alive() for thread in _worker_threads):
            return _write_queue
        _write_queue = queue.Queue(maxsize=cfg.queue_maxsize)
        _worker_threads = []
        for index in range(cfg.worker_count):
            thread = threading.Thread(
                target=_worker_loop,
                args=(_write_queue,),
                name=f"context-canvas-autopilot-v2-writer-{index + 1}",
                daemon=True,
            )
            thread.start()
            _worker_threads.append(thread)
        return _write_queue


def _flush_worker(timeout_seconds: int) -> bool:
    work_queue = _write_queue
    if work_queue is None:
        return True
    deadline = time.monotonic() + max(0, timeout_seconds)
    while work_queue.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _record_queue_full(
    *,
    cfg: PluginConfig,
    hook_ms: float,
    queue_depth: int,
    event_sequence: int,
    legacy: PolicyDecision,
    result_chars: int,
    tool_name: str,
    result: Any,
    task_id: str,
    session_id: str,
    tool_call_id: str,
) -> None:
    session_key, identity_unknown = _safe_session_key(task_id, session_id, tool_call_id)
    event_id, event_identity_unknown = _event_id(session_key, tool_call_id)
    metric = _metric_base(
        cfg=cfg,
        session_key=session_key,
        event_id=event_id,
        event_sequence=event_sequence,
        identity_unknown=identity_unknown,
        event_identity_unknown=event_identity_unknown,
        tool_name=tool_name,
        result_chars=result_chars,
        legacy=legacy,
    )
    metric.update(
        {
            "active_capture_attempted": not _is_context_canvas_tool(tool_name),
            "active_error_type": "QueueFull",
            "active_excluded": _is_context_canvas_tool(tool_name),
            "self_capture_excluded": _is_context_canvas_tool(tool_name),
            "queue_depth": max(0, queue_depth),
            "hook_ms": round(max(0.0, hook_ms), 3),
        }
    )
    try:
        _append_metric(cfg, metric)
    except Exception as exc:
        logger.debug("context-canvas-autopilot queue-full metric failed: %s", type(exc).__name__)


def _on_post_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int | None = None,
    status: str = "",
    error_type: str = "",
    **_: Any,
) -> None:
    """Enqueue active persistence; synchronous mode exists for tests/fallback."""
    hook_started = time.perf_counter()
    cfg = _config()
    if cfg.mode == "off":
        return
    event_sequence, legacy, serialized_result_text = _prepare_legacy_event(
        cfg=cfg,
        tool_name=tool_name,
        result=result,
        task_id=task_id,
        session_id=session_id,
    )
    if serialized_result_text is None:
        serialized_result_text = _serialize(result)[1]
    if not cfg.async_writes:
        _process_tool_event(
            cfg=cfg,
            hook_ms=-1.0,
            queue_wait_ms=0.0,
            queue_depth=0,
            event_sequence=event_sequence,
            legacy=legacy,
            tool_name=tool_name,
            args=args,
            result=result,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            duration_ms=duration_ms,
            status=status,
            error_type=error_type,
        )
        return
    work_queue = _ensure_worker(cfg)
    queue_depth = work_queue.qsize()
    queued_at = time.perf_counter()
    task = QueuedToolEvent(
        cfg=cfg,
        queued_at=queued_at,
        queue_depth=queue_depth,
        hook_ms=(queued_at - hook_started) * 1000,
        event_sequence=event_sequence,
        legacy=legacy,
        tool_name=tool_name,
        args=args,
        result=result,
        task_id=task_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        duration_ms=duration_ms,
        status=status,
        error_type=error_type,
    )
    try:
        work_queue.put_nowait(task)
    except queue.Full:
        _record_queue_full(
            cfg=cfg,
            hook_ms=(time.perf_counter() - hook_started) * 1000,
            queue_depth=queue_depth,
            event_sequence=event_sequence,
            legacy=legacy,
            result_chars=len(serialized_result_text),
            tool_name=tool_name,
            result=result,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )


def on_post_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int | None = None,
    status: str = "",
    error_type: str = "",
    **extra: Any,
) -> None:
    """Keep every Autopilot persistence failure outside the original tool call."""
    try:
        _on_post_tool_call(
            tool_name=tool_name,
            args=args,
            result=result,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            duration_ms=duration_ms,
            status=status,
            error_type=error_type,
            **extra,
        )
    except Exception as exc:
        logger.debug("context-canvas-autopilot hook failed open: %s", type(exc).__name__)


def _lifecycle_update(
    *,
    task_id: str = "",
    session_id: str = "",
    lifecycle: str,
    completed: bool | None = None,
    interrupted: bool | None = None,
) -> None:
    cfg = _config()
    if cfg.mode == "off":
        return
    if cfg.async_writes and not _flush_worker(cfg.flush_timeout_seconds):
        logger.warning("context-canvas-autopilot writer flush timed out before lifecycle update")
        return
    try:
        _flush_metrics(cfg)
    except Exception as exc:
        logger.warning("context-canvas-autopilot metric flush failed: %s", type(exc).__name__)
        return
    if not (session_id or task_id):
        return
    session_key, _ = _safe_session_key(task_id, session_id)
    with _state_lock:
        if session_key not in _captured_sessions:
            return
    try:
        CanvasStore, SnapshotStore, _, now_iso = _components()
        snapshot_store = SnapshotStore(cfg.cache_root)
        updates: dict[str, Any] = {"lifecycle": lifecycle, "at": now_iso(), "revision": cfg.revision}
        if completed is not None:
            updates["completed"] = bool(completed)
        if interrupted is not None:
            updates["interrupted"] = bool(interrupted)
        snapshot_store.update_session_state(session_key, updates)
        canvas_id = _canvas_id(session_key)
        store = CanvasStore()
        try:
            store.update_metadata(canvas_id, {"lifecycle": lifecycle, "last_lifecycle_at": updates["at"]})
        except FileNotFoundError:
            pass
    except Exception as exc:
        logger.debug("context-canvas-autopilot lifecycle update failed: %s", type(exc).__name__)


def on_session_end(
    *,
    task_id: str = "",
    session_id: str = "",
    completed: bool = False,
    interrupted: bool = False,
    **_: Any,
) -> None:
    _lifecycle_update(
        task_id=task_id,
        session_id=session_id,
        lifecycle="turn-ended",
        completed=completed,
        interrupted=interrupted,
    )


def on_session_finalize(*, task_id: str = "", session_id: str = "", **_: Any) -> None:
    _lifecycle_update(task_id=task_id, session_id=session_id, lifecycle="closed")


def register(ctx) -> None:
    global _atexit_registered
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    if not _atexit_registered:
        atexit.register(lambda: _flush_worker(5))
        _atexit_registered = True
