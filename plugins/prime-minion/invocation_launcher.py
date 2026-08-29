#!/usr/bin/env python3
"""Direct invocation launcher with parent-death defense in depth."""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import sys
from collections.abc import Sequence

PR_SET_PDEATHSIG = 1


def _arm_parent_death(expected_parent_pid: int) -> None:
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("Hermes parent disappeared before launcher startup")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-parent-pid", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv if argv is not None else sys.argv[1:]))
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("launcher command is required")
    _arm_parent_death(args.expected_parent_pid)
    os.execvp(command[0], command)
    raise AssertionError("execvp returned")


if __name__ == "__main__":
    raise SystemExit(main())
