#!/usr/bin/env python3
"""Install or verify the pinned Prime Agent source runtime."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "prime-agent"
REPOSITORY = "https://github.com/PrimeIntellect-ai/prime-agent.git"
COMMIT = "bc0fa7606abb3b7af0f765319518d255e6ae553d"
EXPECTED_VERSION = "0.8.1"


def ensure_runtime_parent() -> Path:
    """Create the fixed runtime parent once or verify it without repair."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(ROOT, flags)
    except OSError as exc:
        raise SystemExit(f"Prime plugin root is not a trusted directory: {ROOT}: {exc}") from exc
    parent_fd = -1
    try:
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
            raise SystemExit("Prime plugin root must be a directory owned by the invoking uid")
        try:
            parent_fd = os.open(".runtime", flags, dir_fd=root_fd)
        except FileNotFoundError:
            os.mkdir(".runtime", mode=0o700, dir_fd=root_fd)
            parent_fd = os.open(".runtime", flags, dir_fd=root_fd)
        parent_info = os.fstat(parent_fd)
        current = os.lstat(RUNTIME.parent)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.getuid()
            or stat.S_IMODE(parent_info.st_mode) != 0o700
            or (current.st_dev, current.st_ino) != (parent_info.st_dev, parent_info.st_ino)
        ):
            raise SystemExit(
                "existing Prime runtime parent must be the fixed owner-only 0700 directory; "
                "it will not be chmod-repaired"
            )
    except OSError as exc:
        raise SystemExit(f"could not establish fixed Prime runtime parent: {exc}") from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)
    return RUNTIME.parent


def load_worker_module():
    module_path = ROOT / "invocation_worker.py"
    spec = importlib.util.spec_from_file_location("prime_minion_bootstrap_worker", module_path)
    if spec is None or spec.loader is None:
        raise SystemExit("could not load Prime invocation-worker capability verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def provision_lifecycle_anchor() -> None:
    module = load_worker_module()
    anchor = ROOT / ".runtime" / "invocation-anchor"
    try:
        module.provision_fixed_anchor(anchor)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


def verify_lifecycle_profile() -> None:
    module = load_worker_module()
    anchor = ROOT / ".runtime" / "invocation-anchor"
    try:
        receipt = module.check_capability_profile(anchor)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    if receipt.get("profile") != "linux-user-mount-pid-v1" or receipt.get("status") != "pass":
        raise SystemExit("Prime invocation-worker lifecycle capability profile did not pass")
    print(f"verified lifecycle profile {receipt['profile']}")


def run(command: list[str], cwd: Path | None = None, capture: bool = False) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=capture,
        check=False,
        timeout=900,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "")[-3000:]
        raise SystemExit(f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}")
    return (completed.stdout.strip() or completed.stderr.strip()) if capture else ""


def verify() -> None:
    if not (RUNTIME / ".git").exists():
        raise SystemExit(f"Prime runtime is not installed at {RUNTIME}")
    head = run(["git", "rev-parse", "HEAD"], cwd=RUNTIME, capture=True)
    if head != COMMIT:
        raise SystemExit(f"Prime runtime drift: expected {COMMIT}, observed {head}")
    status = run(["git", "status", "--porcelain"], cwd=RUNTIME, capture=True)
    if status:
        raise SystemExit("Prime runtime source is dirty; refusing to treat it as the pinned runtime")
    version = run([str(RUNTIME / "prime-agent.sh"), "--version"], cwd=RUNTIME, capture=True)
    if version != EXPECTED_VERSION:
        raise SystemExit(f"Prime runtime version drift: expected {EXPECTED_VERSION}, observed {version}")
    verify_lifecycle_profile()
    print(f"verified Prime Agent {version} at {head}")


def install() -> None:
    for command in ("git", "node", "npm"):
        if shutil.which(command) is None:
            raise SystemExit(f"required command is missing: {command}")
    ensure_runtime_parent()
    if not RUNTIME.exists():
        run(["git", "clone", "--filter=blob:none", "--no-checkout", REPOSITORY, str(RUNTIME)])
        run(["git", "checkout", "--detach", COMMIT], cwd=RUNTIME)
    else:
        head = run(["git", "rev-parse", "HEAD"], cwd=RUNTIME, capture=True)
        if head != COMMIT:
            raise SystemExit(
                f"existing runtime is {head}; remove or relocate it manually before installing {COMMIT}"
            )
    provision_lifecycle_anchor()
    run(["npm", "ci"], cwd=RUNTIME)
    verify()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    verify() if args.verify_only else install()


if __name__ == "__main__":
    main()
