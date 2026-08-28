#!/usr/bin/env python3
"""Install or verify the pinned Prime Agent source runtime."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "prime-agent"
REPOSITORY = "https://github.com/PrimeIntellect-ai/prime-agent.git"
COMMIT = "bc0fa7606abb3b7af0f765319518d255e6ae553d"
EXPECTED_VERSION = "0.8.1"


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
    print(f"verified Prime Agent {version} at {head}")


def install() -> None:
    for command in ("git", "node", "npm"):
        if shutil.which(command) is None:
            raise SystemExit(f"required command is missing: {command}")
    RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    if not RUNTIME.exists():
        run(["git", "clone", "--filter=blob:none", "--no-checkout", REPOSITORY, str(RUNTIME)])
        run(["git", "checkout", "--detach", COMMIT], cwd=RUNTIME)
    else:
        head = run(["git", "rev-parse", "HEAD"], cwd=RUNTIME, capture=True)
        if head != COMMIT:
            raise SystemExit(
                f"existing runtime is {head}; remove or relocate it manually before installing {COMMIT}"
            )
    run(["npm", "ci"], cwd=RUNTIME)
    verify()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    verify() if args.verify_only else install()


if __name__ == "__main__":
    main()
