import base64
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugins" / "context-canvas-autopilot" / "__init__.py"
PLUGIN_MANIFEST = ROOT / "plugins" / "context-canvas-autopilot" / "plugin.yaml"
TOOL_ROOT = ROOT / "packages" / "context-canvas"
TOOL_ROOT_DOCS = (
    ROOT / "docs" / "install.md",
    ROOT / "docs" / "technical" / "context-canvas-v2-reverse-shadow.md",
)
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from context_canvas.snapshot import PrivateJsonlLedger, SnapshotStore  # type: ignore[import-not-found]  # noqa: E402


def load_plugin(plugin_path=PLUGIN_PATH):
    name = "context_canvas_autopilot_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        plugin_path,
        submodule_search_locations=[str(plugin_path.parent)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def copy_context_canvas_package(target_root: Path) -> None:
    package = shutil.copytree(TOOL_ROOT / "context_canvas", target_root / "context_canvas")
    for path in (target_root, package, *package.rglob("*")):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


def write_foreign_context_canvas_package(target_root: Path) -> None:
    package = target_root / "context_canvas"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text(
        """import os
from pathlib import Path
Path(os.environ["FOREIGN_IMPORT_MARKER"]).write_text("executed", encoding="utf-8")
class CanvasStore:
    pass
""",
        encoding="utf-8",
    )
    (package / "snapshot.py").write_text(
        """class SnapshotStore:
    pass
class PrivateJsonlLedger:
    pass
def now_iso():
    return "foreign"
""",
        encoding="utf-8",
    )


def run_trust_boundary_probe(
    *,
    tmp_path: Path,
    scenario: str,
    configured_root: Path,
) -> dict[str, Any]:
    isolated_home = tmp_path / "isolated-home"
    installed_plugin = isolated_home / "plugins" / "context-canvas-autopilot" / "__init__.py"
    installed_plugin.parent.mkdir(parents=True)
    shutil.copy2(PLUGIN_PATH, installed_plugin)
    foreign_root = tmp_path / "foreign-root"
    write_foreign_context_canvas_package(foreign_root)
    marker = tmp_path / "foreign-imported"

    probe = r'''
import importlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

plugin_path = Path(sys.argv[1])
configured_root = Path(sys.argv[2])
foreign_root = Path(sys.argv[3])
isolated_home = Path(sys.argv[4])
scenario = sys.argv[5]
sys.path.insert(0, str(foreign_root))
spec = importlib.util.spec_from_file_location(
    "isolated_context_canvas_autopilot",
    plugin_path,
    submodule_search_locations=[str(plugin_path.parent)],
)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
plugin._load_entry_config = lambda: {"tool_root": str(configured_root)}
plugin._hermes_home = lambda: isolated_home
if scenario == "stale":
    plugin._components()
    shutil.rmtree(configured_root)
    for name in tuple(sys.modules):
        if name == "context_canvas" or name.startswith("context_canvas."):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()
error_type = None
error_message = ""
try:
    plugin._components()
except Exception as exc:
    error_type = type(exc).__name__
    error_message = str(exc)
print(json.dumps({
    "error_type": error_type,
    "error_message": error_message,
    "foreign_executed": Path(os.environ["FOREIGN_IMPORT_MARKER"]).exists(),
    "loaded_context_canvas": sorted(
        name for name in sys.modules
        if name == "context_canvas" or name.startswith("context_canvas.")
    ),
}, sort_keys=True))
'''
    env = os.environ.copy()
    env.pop("HERMES_CONTEXT_CANVAS_TOOL", None)
    env["FOREIGN_IMPORT_MARKER"] = str(marker)
    env["HERMES_HOME"] = str(isolated_home)
    env["HOME"] = str(isolated_home)
    env["PYTHONPATH"] = ""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            probe,
            str(installed_plugin),
            str(configured_root),
            str(foreign_root),
            str(isolated_home),
            scenario,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"trusted-root probe failed\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return json.loads(completed.stdout.splitlines()[-1])


def run_copied_plugin_probe(
    *,
    tmp_path: Path,
    config_tool_root: Path,
    preload_root: Path | None = None,
) -> dict[str, Any]:
    hermes_home = tmp_path / ".hermes"
    host_api = tmp_path / "host-api"
    hermes_cli = host_api / "hermes_cli"
    hermes_cli.mkdir(parents=True)
    (hermes_cli / "__init__.py").write_text("", encoding="utf-8")
    (hermes_cli / "config.py").write_text(
        """from pathlib import Path
import os
import yaml

def load_config_readonly():
    path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

def load_config():
    return load_config_readonly()
""",
        encoding="utf-8",
    )
    installed_plugin = hermes_home / "plugins" / "context-canvas-autopilot" / "__init__.py"
    installed_plugin.parent.mkdir(parents=True)
    shutil.copy2(PLUGIN_PATH, installed_plugin)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "entries": {
                        "context-canvas-autopilot": {
                            "tool_root": str(config_tool_root),
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    probe = r'''
import importlib
import importlib.util
import json
import sys
from pathlib import Path

plugin_path = Path(sys.argv[1])
source_tool_root = Path(sys.argv[2]).resolve()
sys.path[:] = [
    entry
    for entry in sys.path
    if not entry or Path(entry).resolve() != source_tool_root
]
importlib.invalidate_caches()
assert importlib.util.find_spec("context_canvas") is None
preload_root = sys.argv[3]
if preload_root:
    sys.path.insert(0, preload_root)
    import context_canvas.core
    import context_canvas.snapshot
spec = importlib.util.spec_from_file_location(
    "copied_context_canvas_autopilot",
    plugin_path,
    submodule_search_locations=[str(plugin_path.parent)],
)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
components = plugin._components()
origins = {
    component.__name__: str(Path(sys.modules[component.__module__].__file__).resolve())
    for component in components
}
print(json.dumps({"origins": origins, "sys_path_0": sys.path[0]}, sort_keys=True))
'''
    env = os.environ.copy()
    env.pop("HERMES_CONTEXT_CANVAS_TOOL", None)
    env["HERMES_HOME"] = str(hermes_home)
    env["PYTHONPATH"] = os.pathsep.join((str(host_api), str(ROOT)))
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            probe,
            str(installed_plugin),
            str(TOOL_ROOT),
            str(preload_root) if preload_root is not None else "",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"copied plugin probe failed\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return json.loads(completed.stdout.splitlines()[-1])


def test_partial_configured_root_falls_through_to_complete_installed_root(tmp_path):
    partial_root = tmp_path / "partial-configured-root"
    partial_package = partial_root / "context_canvas"
    partial_package.mkdir(parents=True)
    shutil.copy2(TOOL_ROOT / "context_canvas" / "__init__.py", partial_package)
    shutil.copy2(TOOL_ROOT / "context_canvas" / "core.py", partial_package)

    complete_root = tmp_path / ".hermes" / "context-canvas-tool"
    copy_context_canvas_package(complete_root)

    result = run_copied_plugin_probe(
        tmp_path=tmp_path,
        config_tool_root=partial_root,
    )

    expected_package = (complete_root / "context_canvas").resolve()
    assert Path(str(result["sys_path_0"])).resolve() == complete_root.resolve()
    assert isinstance(result["origins"], dict)
    for origin in result["origins"].values():
        assert Path(origin).is_relative_to(expected_package)


def test_copied_plugin_loads_components_from_real_configured_root(tmp_path):
    configured_root = tmp_path / "configured-tool-root"
    copy_context_canvas_package(configured_root)

    result = run_copied_plugin_probe(
        tmp_path=tmp_path,
        config_tool_root=configured_root,
    )

    expected_package = (configured_root / "context_canvas").resolve()
    assert Path(str(result["sys_path_0"])).resolve() == configured_root.resolve()
    assert isinstance(result["origins"], dict)
    assert set(result["origins"]) == {
        "CanvasStore",
        "SnapshotStore",
        "PrivateJsonlLedger",
        "now_iso",
    }
    for origin in result["origins"].values():
        assert Path(origin).is_relative_to(expected_package)


def test_configured_root_replaces_preloaded_foreign_context_canvas(tmp_path):
    configured_root = tmp_path / "configured-tool-root"
    foreign_root = tmp_path / "foreign-tool-root"
    copy_context_canvas_package(configured_root)
    copy_context_canvas_package(foreign_root)

    result = run_copied_plugin_probe(
        tmp_path=tmp_path,
        config_tool_root=configured_root,
        preload_root=foreign_root,
    )

    expected_package = (configured_root / "context_canvas").resolve()
    assert Path(str(result["sys_path_0"])).resolve() == configured_root.resolve()
    assert isinstance(result["origins"], dict)
    for origin in result["origins"].values():
        assert Path(origin).is_relative_to(expected_package)


def test_no_valid_root_never_imports_foreign_global_package(tmp_path):
    result = run_trust_boundary_probe(
        tmp_path=tmp_path,
        scenario="missing",
        configured_root=tmp_path / "missing-configured-root",
    )

    assert result["error_type"] == "RuntimeError"
    assert "trusted" in result["error_message"].lower()
    assert result["foreign_executed"] is False
    assert result["loaded_context_canvas"] == []


@pytest.mark.parametrize("hook_path", ["synchronous", "queue-full"])
def test_missing_trusted_root_never_escapes_post_tool_hook(
    tmp_path,
    monkeypatch,
    hook_path,
):
    """Persistence failures must remain fail-open to the original tool call."""
    isolated_home = tmp_path / "isolated-home"
    installed_plugin = (
        isolated_home / "plugins" / "context-canvas-autopilot" / "__init__.py"
    )
    installed_plugin.parent.mkdir(parents=True)
    shutil.copy2(PLUGIN_PATH, installed_plugin)
    plugin = load_plugin(installed_plugin)
    plugin.reset_state_for_tests()
    monkeypatch.setenv("HERMES_HOME", str(isolated_home))
    monkeypatch.delenv("HERMES_CONTEXT_CANVAS_TOOL", raising=False)
    monkeypatch.setattr(plugin, "_hermes_home", lambda: isolated_home)
    plugin.set_test_config(
        {
            "mode": "v2_active_legacy_shadow",
            "cache_root": str(tmp_path / "cache"),
            "metrics_root": str(tmp_path / "metrics"),
            "metrics_enabled": True,
            "require_hermes_redactor": True,
            "async_writes": hook_path == "queue-full",
            "queue_maxsize": 1,
            "worker_count": 1,
            "flush_timeout_seconds": 1,
        }
    )
    if hook_path == "queue-full":
        full_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        full_queue.put_nowait(object())
        monkeypatch.setattr(plugin, "_ensure_worker", lambda _cfg: full_queue)

    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "fixture.txt"},
        result={"ok": True},
        session_id="fail-open-probe",
        tool_call_id="fail-open-call",
        status="ok",
    )


def test_deleted_cached_root_never_imports_foreign_global_package(tmp_path):
    configured_root = tmp_path / "configured-root"
    copy_context_canvas_package(configured_root)

    result = run_trust_boundary_probe(
        tmp_path=tmp_path,
        scenario="stale",
        configured_root=configured_root,
    )

    assert result["error_type"] == "RuntimeError"
    assert "trusted" in result["error_message"].lower()
    assert result["foreign_executed"] is False
    assert result["loaded_context_canvas"] == []


def test_valid_environment_root_avoids_config_load(tmp_path, monkeypatch):
    plugin = load_plugin()
    environment_root = tmp_path / "environment-root"
    copy_context_canvas_package(environment_root)
    plugin.reset_state_for_tests()
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL", str(environment_root))

    def fail_config_load():
        raise AssertionError("valid environment override must avoid config loading")

    monkeypatch.setattr(plugin, "_load_entry_config", fail_config_load)

    plugin._components()
    plugin._components()


def test_configured_root_resolution_is_cached(tmp_path, monkeypatch):
    plugin = load_plugin()
    configured_root = tmp_path / "configured-root"
    copy_context_canvas_package(configured_root)
    plugin.reset_state_for_tests()
    monkeypatch.delenv("HERMES_CONTEXT_CANVAS_TOOL", raising=False)
    config_loads = 0

    def load_config_once():
        nonlocal config_loads
        config_loads += 1
        return {"tool_root": str(configured_root)}

    monkeypatch.setattr(plugin, "_load_entry_config", load_config_once)

    plugin._components()
    plugin._components()

    assert config_loads == 1


def test_unresolvable_environment_tilde_falls_through_to_configured_root(tmp_path, monkeypatch):
    plugin = load_plugin()
    configured_root = tmp_path / "configured-root"
    copy_context_canvas_package(configured_root)
    plugin.reset_state_for_tests()
    monkeypatch.setenv(
        "HERMES_CONTEXT_CANVAS_TOOL",
        "~__hermes_probe_missing_user_8f3f__/tool",
    )
    monkeypatch.setattr(
        plugin,
        "_load_entry_config",
        lambda: {"tool_root": str(configured_root)},
    )

    assert plugin._ensure_context_canvas_importable() == configured_root.resolve()


@pytest.mark.parametrize(
    ("target", "write_bit"),
    [
        ("ancestor", 0o020),
        ("root", 0o002),
        ("package", 0o020),
        ("core.py", 0o002),
    ],
)
def test_tool_root_validation_rejects_group_or_world_writable_tree(
    tmp_path,
    target,
    write_bit,
):
    plugin = load_plugin()
    complete_root = tmp_path / "complete-tool-root"
    copy_context_canvas_package(complete_root)
    paths = {
        "ancestor": tmp_path,
        "root": complete_root,
        "package": complete_root / "context_canvas",
        "core.py": complete_root / "context_canvas" / "core.py",
    }
    path = paths[target]
    path.chmod(path.stat().st_mode | write_bit)

    assert plugin._validated_tool_root(complete_root) is None


def test_tool_root_validation_checks_above_apparent_system_owned_boundary(tmp_path, monkeypatch):
    plugin = load_plugin()
    apparent_boundary = tmp_path / "apparent-system-boundary"
    complete_root = apparent_boundary / "complete-tool-root"
    copy_context_canvas_package(complete_root)
    apparent_boundary.chmod(0o755)
    tmp_path.chmod(0o777)
    real_lstat = Path.lstat

    def root_owned_boundary_lstat(path):
        path_stat = real_lstat(path)
        if path == apparent_boundary:
            return type("RootOwnedStat", (), {"st_mode": path_stat.st_mode, "st_uid": 0})()
        return path_stat

    monkeypatch.setattr(plugin.Path, "lstat", root_owned_boundary_lstat)

    assert plugin._validated_tool_root(complete_root) is None


def test_tool_root_validation_rejects_symlinked_required_file(tmp_path):
    plugin = load_plugin()
    complete_root = tmp_path / "complete-tool-root"
    copy_context_canvas_package(complete_root)
    core = complete_root / "context_canvas" / "core.py"
    core.unlink()
    core.symlink_to("snapshot.py")

    assert plugin._validated_tool_root(complete_root) is None


def test_tool_root_validation_fails_closed_without_posix_euid_api(tmp_path, monkeypatch):
    plugin = load_plugin()
    complete_root = tmp_path / "complete-tool-root"
    copy_context_canvas_package(complete_root)
    monkeypatch.setattr(plugin.os, "geteuid", None)

    assert plugin._validated_tool_root(complete_root) is None


def test_tool_root_validation_requires_absolute_complete_owned_root(tmp_path, monkeypatch):
    plugin = load_plugin()
    complete_root = tmp_path / "complete-tool-root"
    copy_context_canvas_package(complete_root)

    assert plugin._validated_tool_root(complete_root) == complete_root.resolve()
    monkeypatch.chdir(tmp_path)
    assert plugin._validated_tool_root(Path("complete-tool-root")) is None

    (complete_root / "context_canvas" / "snapshot.py").unlink()
    assert plugin._validated_tool_root(complete_root) is None
    copy_context_canvas_package(tmp_path / "owner-check-root")
    if hasattr(os, "geteuid"):
        owner = os.geteuid()
        monkeypatch.setattr(plugin.os, "geteuid", lambda: owner + 1)
        assert plugin._validated_tool_root(tmp_path / "owner-check-root") is None


def test_tool_root_docs_define_the_code_execution_trust_boundary():
    required_terms = {
        "absolute",
        "owner-controlled",
        "code-execution",
        "import precedence",
        "__init__.py",
        "core.py",
        "snapshot.py",
        "posix",
        "group- or world-writable",
        "symbolic links",
        "fail closed",
    }

    for path in TOOL_ROOT_DOCS:
        content = " ".join(
            path.read_text(encoding="utf-8").lower().replace("`", "").split()
        )
        for term in required_terms:
            assert term in content, f"{path}: missing {term}"


def test_plugin_manifest_version_matches_runtime_revision():
    plugin = load_plugin()
    manifest = yaml.safe_load(PLUGIN_MANIFEST.read_text(encoding="utf-8"))

    assert manifest["version"] == plugin.PLUGIN_REVISION.split("-", 1)[0]


def configure(plugin, tmp_path, monkeypatch, **overrides):
    canvas_root = tmp_path / "canvas"
    cache_root = tmp_path / "cache"
    metrics_root = tmp_path / "metrics"
    tool_root = tmp_path / "context-canvas-tool"
    copy_context_canvas_package(tool_root)
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_HOME", str(canvas_root))
    monkeypatch.setenv("HERMES_CONTEXT_CANVAS_TOOL", str(tool_root))
    plugin.reset_state_for_tests()
    config = {
        "mode": "v2_active_legacy_shadow",
        "revision": "test-r1",
        "cache_root": str(cache_root),
        "metrics_root": str(metrics_root),
        "retention_class": "test",
        "retention_days": 1,
        "max_semantic_refs": 12,
        "legacy_tool_threshold": 3,
        "legacy_large_result_chars": 1000,
        "legacy_max_ref_chars": 50000,
        "metrics_enabled": True,
        "require_hermes_redactor": False,
        "async_writes": False,
        "queue_maxsize": 64,
        "flush_timeout_seconds": 10,
    }
    config.update(overrides)
    plugin.set_test_config(config)
    return canvas_root, cache_root, metrics_root


def manifests(cache_root):
    return sorted((cache_root / "sessions").glob("*/snapshots/sr_*.json"))


def test_v2_caches_full_result_from_first_call_without_noisy_canvas(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)

    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "a.py"},
        result="full point-in-time result",
        session_id="s1",
        tool_call_id="call-1",
        status="ok",
    )

    paths = manifests(cache_root)
    assert len(paths) == 1
    store = SnapshotStore(cache_root)
    checked = store.validate_manifest(paths[0])
    assert checked["envelope"]["result"] == "full point-in-time result"
    assert checked["manifest"]["full_snapshot_is_sanitized"] is True
    assert not list(canvas_root.glob("auto-v2-*/canvas.json"))
    rows = PrivateJsonlLedger(metrics_root).read()
    assert rows[0]["active_capture_ok"] is True
    assert rows[0]["legacy_shadow_capture"] is False


def test_content_addressing_deduplicates_identical_envelopes(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)

    for index in range(2):
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": "same.py"},
            result="same result",
            session_id="dedupe",
            tool_call_id=f"call-{index}",
            status="ok",
        )

    paths = manifests(cache_root)
    assert len(paths) == 2
    payloads = [json.loads(path.read_text()) for path in paths]
    assert payloads[0]["object_sha256"] == payloads[1]["object_sha256"]
    assert payloads[0]["object_reused"] is False
    assert payloads[1]["object_reused"] is True
    objects = list((cache_root / "objects" / "text" / "sha256").glob("*/*.json.zlib"))
    assert len(objects) == 1
    rows = PrivateJsonlLedger(metrics_root).read()
    assert rows[1]["active_object_reused"] is True


def test_duplicate_tool_call_id_is_idempotent_for_manifest_and_semantic_projection(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)
    event = {
        "tool_name": "patch",
        "args": {"path": "same.py"},
        "result": {"success": True},
        "session_id": "duplicate-event",
        "tool_call_id": "stable-call-id",
        "status": "ok",
    }

    plugin.on_post_tool_call(**event)
    plugin.on_post_tool_call(**event)

    assert len(manifests(cache_root)) == 1
    rows = PrivateJsonlLedger(metrics_root).read()
    assert len(rows) == 2
    assert rows[0]["event_id"] == rows[1]["event_id"]
    assert rows[0]["active_event_duplicate"] is False
    assert rows[1]["active_event_duplicate"] is True
    canvas = json.loads(next(canvas_root.glob("auto-v2-duplicate-event-*/canvas.json")).read_text())
    assert len(canvas["nodes"]) == 1
    assert len(canvas["nodes"][0]["refs"]) == 1


def test_data_url_is_externalized_as_binary_object(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, _ = configure(plugin, tmp_path, monkeypatch)
    raw = b"\x89PNG\r\n\x1a\n" + b"Z" * 512
    encoded = base64.b64encode(raw).decode("ascii")

    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"image_url": f"data:image/png;base64,{encoded}"},
        result={"ok": True, "description": "fixture"},
        session_id="image",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    envelope_text = json.dumps(checked["envelope"])
    assert ";base64," not in envelope_text
    embedded = checked["manifest"]["embedded_objects"]
    assert len(embedded) == 1
    assert SnapshotStore(cache_root).read_binary(embedded[0]["sha256"]) == raw


def test_short_data_url_is_externalized_as_binary_object(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)

    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"image_url": "data:image/gif;base64,YWJj"},
        result={"ok": True},
        session_id="short-image",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    envelope_text = json.dumps(checked["envelope"])
    assert ";base64,YWJj" not in envelope_text
    embedded = checked["manifest"]["embedded_objects"]
    assert len(embedded) == 1
    assert checked["manifest"]["externalization_errors"] == 0
    assert SnapshotStore(cache_root).read_binary(embedded[0]["sha256"]) == b"abc"
    assert PrivateJsonlLedger(metrics_root).read()[0]["active_externalization_errors"] == 0


def test_embedded_binary_metrics_count_raw_bytes_and_first_write_storage_only(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch, legacy_tool_threshold=1)
    raw = bytes((index * 73 + index // 11 * 19 + 17) % 256 for index in range(4096))
    encoded = base64.b64encode(raw).decode("ascii")
    url = f"data:application/octet-stream;base64,{encoded}"

    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"first": url, "second": url},
        result={"ok": True},
        session_id="binary-ledger",
        tool_call_id="binary-1",
        status="ok",
    )
    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"first": url},
        result={"ok": True},
        session_id="binary-ledger",
        tool_call_id="binary-2",
        status="ok",
    )

    rows = PrivateJsonlLedger(metrics_root).read()
    assert len(rows) == 2
    assert rows[0]["active_embedded_raw_bytes"] == len(raw) * 2
    assert rows[0]["active_embedded_stored_bytes"] == len(raw)
    assert rows[1]["active_embedded_raw_bytes"] == len(raw)
    assert rows[1]["active_embedded_stored_bytes"] == 0

    checked = [SnapshotStore(cache_root).validate_manifest(path) for path in manifests(cache_root)]
    assert [[item["reused"] for item in entry["manifest"]["embedded_objects"]] for entry in checked] == [
        [False, True],
        [True],
    ]
    assert all(entry["manifest"]["embedded_objects"][0]["raw_bytes"] == len(raw) for entry in checked)


def test_malformed_data_url_is_removed_and_externalization_error_is_recorded(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)
    malformed = "data:image/gif;base64,%%%not-valid%%%"

    plugin.on_post_tool_call(
        tool_name="vision_analyze",
        args={"image_url": malformed},
        result={"ok": True},
        session_id="invalid-image",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    envelope_text = json.dumps(checked["envelope"])
    assert malformed not in envelope_text
    assert ";base64," not in envelope_text
    assert checked["manifest"]["embedded_objects"] == []
    assert checked["manifest"]["externalization_errors"] == 1
    assert PrivateJsonlLedger(metrics_root).read()[0]["active_externalization_errors"] == 1


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        ({"exit_code": 1, "output": "failed"}, "nonzero_exit"),
        ({"success": False}, "reported_failure"),
        ({"ok": False}, "reported_failure"),
        ({"error": "tool failed"}, "error_field"),
    ],
)
def test_observer_ok_cannot_hide_payload_failure(result, reason):
    plugin = load_plugin()
    assert plugin._status_from_result(result, json.dumps(result), observer_status="ok") == ("error", reason)


def test_observer_error_type_remains_failure_even_with_ok_status():
    plugin = load_plugin()
    assert plugin._status_from_result(
        {"ok": True},
        '{"ok":true}',
        observer_status="ok",
        observer_error_type="tool_error",
    ) == ("error", "tool_error")


def test_observer_ok_nonzero_exit_promotes_failure_not_verification(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch)

    plugin.on_post_tool_call(
        tool_name="terminal",
        args={"command": "python -m pytest -q"},
        result={"exit_code": 1, "output": "failed"},
        session_id="observer-ok-failure",
        tool_call_id="failed-call",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    assert checked["manifest"]["status"] == "error"
    assert checked["manifest"]["status_reason"] == "nonzero_exit"
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["active_semantic_class"] == "failure"
    assert row["legacy_shadow_capture"] is False
    canvas = json.loads(next(canvas_root.glob("auto-v2-observer-ok-failure-*/canvas.json")).read_text())
    assert {node["id"] for node in canvas["nodes"]} == {"AUTO_V2_FAILURES"}


def test_registered_context_canvas_tool_is_active_excluded_but_legacy_bug_is_measured(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        legacy_tool_threshold=1,
    )

    plugin.on_post_tool_call(
        tool_name="mcp__context_canvas__canvas_add_ref",
        args={"session_id": "x"},
        result="self capture",
        session_id="self",
        status="ok",
    )

    assert manifests(cache_root) == []
    assert not canvas_root.exists()
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["active_excluded"] is True
    assert row["self_capture_excluded"] is True
    assert row["active_capture_attempted"] is False
    assert row["legacy_shadow_capture"] is True


def test_verification_failure_and_action_promote_three_bounded_nodes(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, _ = configure(plugin, tmp_path, monkeypatch)
    events = [
        ("terminal", {"command": "python -m pytest -q"}, {"exit_code": 0, "output": "3 passed"}, "ok"),
        ("terminal", {"command": "python fail.py"}, {"exit_code": 1, "output": "failed"}, "error"),
        ("patch", {"path": "a.py"}, {"success": True}, "ok"),
    ]
    for index, (tool, args, result, status) in enumerate(events):
        plugin.on_post_tool_call(
            tool_name=tool,
            args=args,
            result=result,
            session_id="semantic",
            tool_call_id=f"call-{index}",
            status=status,
            error_type="fixture" if status == "error" else "",
        )

    assert len(manifests(cache_root)) == 3
    canvas_path = next(canvas_root.glob("auto-v2-semantic-*/canvas.json"))
    canvas = json.loads(canvas_path.read_text())
    assert {node["id"] for node in canvas["nodes"]} == {
        "AUTO_V2_VERIFICATIONS",
        "AUTO_V2_FAILURES",
        "AUTO_V2_ACTIONS",
    }
    assert {node["kind"] for node in canvas["nodes"]} == {"verification", "blocked", "action"}
    assert all(node["refs"] for node in canvas["nodes"])
    assert len(list((canvas_path.parent / "refs").glob("tc_*.md"))) == 3


def test_legacy_shadow_threshold_never_materializes_legacy_canvas(tmp_path, monkeypatch):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch, legacy_tool_threshold=3)

    for index in range(4):
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": f"{index}.py"},
            result="small",
            session_id="threshold",
            status="ok",
        )

    rows = PrivateJsonlLedger(metrics_root).read()
    assert [row["legacy_shadow_capture"] for row in rows] == [False, False, True, True]
    assert len(manifests(cache_root)) == 4
    assert not (canvas_root / "auto-threshold").exists()
    assert not list(canvas_root.glob("auto-v2-*/canvas.json"))


def test_v2_active_does_not_run_or_emit_legacy_evaluator(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        mode="v2_active",
        legacy_tool_threshold=1,
    )

    def forbidden_evaluator(**kwargs):
        raise AssertionError("v2_active must not run the stateful legacy evaluator")

    monkeypatch.setattr(plugin, "evaluate_legacy_event", forbidden_evaluator)
    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "active.py"},
        result="active v2",
        session_id="v2-only",
        tool_call_id="v2-call",
        status="ok",
    )

    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["mode"] == "v2_active"
    assert row["event_sequence"] == 1
    assert row["legacy_shadow_capture"] is False
    assert row["legacy_shadow_reason"] == "disabled"
    assert row["legacy_shadow_estimated_bytes"] == 0
    assert row["legacy_shadow_estimated_nodes"] == 0
    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    assert checked["manifest"]["legacy_shadow_capture"] is False
    assert checked["manifest"]["legacy_shadow_reason"] == "disabled"


@pytest.mark.parametrize("mode", ["v2_active_legacy_shadow", "legacy_active_safe"])
def test_legacy_modes_keep_stateful_decision_contract(tmp_path, monkeypatch, mode):
    plugin = load_plugin()
    canvas_root, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        mode=mode,
        legacy_tool_threshold=1,
    )

    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "legacy.py"},
        result="legacy contract",
        session_id="legacy-contract",
        tool_call_id="legacy-call",
        status="ok",
    )

    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["mode"] == mode
    assert row["event_sequence"] == 1
    assert row["legacy_shadow_capture"] is True
    assert row["legacy_shadow_reason"] == "tool_threshold:1"
    assert row["legacy_shadow_estimated_nodes"] == 1
    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    assert checked["manifest"]["legacy_shadow_capture"] is True
    if mode == "legacy_active_safe":
        assert row["active_semantic_class"] == "action"
        assert canvas_root.exists()
    else:
        assert row["active_semantic_class"] == "none"
        assert not canvas_root.exists()


def _ordered_decisions(plugin, tmp_path, monkeypatch, *, async_writes):
    _, _, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        async_writes=async_writes,
        worker_count=8,
        legacy_tool_threshold=3,
    )
    for index in range(4):
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": f"ordered-{index}.py"},
            result="small",
            session_id="ordered",
            tool_call_id=f"ordered-{index}",
            status="ok",
        )
    if async_writes:
        plugin.on_session_finalize(session_id="ordered")
    rows = PrivateJsonlLedger(metrics_root).read()
    return [
        (row["event_sequence"], row["legacy_shadow_capture"], row["legacy_shadow_reason"])
        for row in sorted(rows, key=lambda row: row["event_sequence"])
    ]


def test_ordered_legacy_stream_matches_sync_and_async_receive_order(tmp_path, monkeypatch):
    sync_plugin = load_plugin()
    sync = _ordered_decisions(sync_plugin, tmp_path / "sync", monkeypatch, async_writes=False)
    async_plugin = load_plugin()
    asynchronous = _ordered_decisions(async_plugin, tmp_path / "async", monkeypatch, async_writes=True)

    expected = [
        (1, False, "below_threshold:1"),
        (2, False, "below_threshold:2"),
        (3, True, "tool_threshold:3"),
        (4, True, "tool_threshold:4"),
    ]
    assert sync == expected
    assert asynchronous == expected


def test_concurrent_capture_keeps_unique_manifests_and_valid_metrics(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(plugin, tmp_path, monkeypatch, legacy_tool_threshold=99)
    workers = 20

    def capture(index):
        plugin.on_post_tool_call(
            tool_name="read_file",
            args={"path": f"{index}.txt"},
            result=f"payload-{index}",
            session_id="parallel",
            tool_call_id=f"call-{index}",
            status="ok",
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(capture, range(workers)))

    paths = manifests(cache_root)
    assert len(paths) == workers
    assert len({path.name for path in paths}) == workers
    store = SnapshotStore(cache_root)
    assert all(store.validate_manifest(path)["ok"] for path in paths)
    rows = PrivateJsonlLedger(metrics_root).read()
    assert len(rows) == workers
    assert all(row["active_capture_ok"] for row in rows)


def test_required_redactor_failure_is_fail_open_and_metricized(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        require_hermes_redactor=True,
    )

    def fail_redactor(*args, **kwargs):
        raise RuntimeError("fixture redactor unavailable")

    monkeypatch.setattr(plugin, "_force_redact_text", fail_redactor)
    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "secret.env"},
        result="secret fixture",
        session_id="redactor-fail",
        status="ok",
    )

    assert manifests(cache_root) == []
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["active_capture_attempted"] is True
    assert row["active_capture_ok"] is False
    assert row["active_error_type"] == "RuntimeError"


def test_redacted_full_snapshot_never_persists_raw_canary(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        require_hermes_redactor=True,
    )
    canary = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"

    def fixture_redactor(text, *, tool_name, required):
        redacted = text.replace(canary, "«redacted:sk-…»")
        return redacted, redacted != text, "hermes_force"

    monkeypatch.setattr(plugin, "_force_redact_text", fixture_redactor)
    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "secret.env"},
        result=f"API_KEY={canary}",
        session_id="secret",
        status="ok",
    )

    checked = SnapshotStore(cache_root).validate_manifest(manifests(cache_root)[0])
    combined = json.dumps(checked["envelope"])
    assert canary not in combined
    assert "redacted" in combined
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["active_redaction_applied"] is True
    assert row["active_redactor_backend"] == "hermes_force"


def test_lifecycle_updates_only_captured_session(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, _ = configure(plugin, tmp_path, monkeypatch)
    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "a.py"},
        result="data",
        session_id="life",
        status="ok",
    )
    plugin.on_session_end(session_id="life", completed=True, interrupted=False)
    lifecycle_path = next((cache_root / "sessions").glob("life-*/lifecycle.json"))
    assert json.loads(lifecycle_path.read_text())["lifecycle"] == "turn-ended"
    plugin.on_session_finalize(session_id="life")
    assert json.loads(lifecycle_path.read_text())["lifecycle"] == "closed"
    plugin.on_session_finalize(session_id="never-captured")
    assert not list((cache_root / "sessions").glob("never-captured-*"))


def test_async_hook_flushes_on_finalize_and_records_queue_latency(tmp_path, monkeypatch):
    plugin = load_plugin()
    _, cache_root, metrics_root = configure(
        plugin,
        tmp_path,
        monkeypatch,
        async_writes=True,
        queue_maxsize=16,
        flush_timeout_seconds=10,
    )

    plugin.on_post_tool_call(
        tool_name="read_file",
        args={"path": "async.py"},
        result="async snapshot",
        session_id="async",
        status="ok",
    )
    plugin.on_session_finalize(session_id="async")

    assert len(manifests(cache_root)) == 1
    row = PrivateJsonlLedger(metrics_root).read()[0]
    assert row["async_write"] is True
    assert row["active_capture_ok"] is True
    assert row["hook_ms"] >= 0
    assert row["persist_ms"] > 0
    assert row["queue_wait_ms"] >= 0


def test_registers_all_required_hooks():
    plugin = load_plugin()

    class FakeContext:
        def __init__(self):
            self.hooks = []

        def register_hook(self, name, callback):
            self.hooks.append((name, callback))

    ctx = FakeContext()
    plugin.register(ctx)
    assert [name for name, _ in ctx.hooks] == ["post_tool_call", "on_session_end", "on_session_finalize"]
