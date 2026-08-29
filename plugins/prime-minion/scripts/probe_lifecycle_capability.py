#!/usr/bin/env python3
"""Disposable no-provider probe for linux-user-mount-pid-v1."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

TMPFS_MAGIC = 0x01021994
MS_REC = 1 << 14
MS_PRIVATE = 1 << 18


def fail(name: str, detail: str = "") -> "NoReturn":
    print(f"FAIL:{name}:{detail[-500:]}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def statfs_type(path: str) -> int:
    class StatFs(ctypes.Structure):
        _fields_ = [("f_type", ctypes.c_long), ("rest", ctypes.c_byte * 256)]

    result = StatFs()
    call = libc().statfs
    call.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFs)]
    call.restype = ctypes.c_int
    if call(path.encode(), ctypes.byref(result)) != 0:
        fail("bounded_tmpfs", os.strerror(ctypes.get_errno()))
    return int(result.f_type) & 0xFFFFFFFFFFFFFFFF


def fd_mount_id(fd: int) -> int:
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii").splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split(":", 1)[1].strip())
    fail("mount_identity", "fdinfo has no mnt_id")


def mount_ids() -> set[int]:
    return {
        int(line.split(maxsplit=1)[0])
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        if line
    }


def run() -> None:
    if len(sys.argv) != 7:
        fail("probe_arguments")
    anchor_fd = int(sys.argv[1])
    anchor_path = Path(sys.argv[2])
    sample_path = Path(sys.argv[3])
    mount_helper = sys.argv[4]
    umount_helper = sys.argv[5]
    unshare_helper = sys.argv[6]
    if os.getpid() != 1:
        fail("pid1_proc", "probe is not namespace PID1")
    visible = sorted(int(name) for name in os.listdir("/proc") if name.isdigit())
    if visible != [1]:
        fail("pid1_proc", f"unexpected initial /proc PIDs {visible}")

    mount_call = libc().mount
    mount_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
    mount_call.restype = ctypes.c_int
    if mount_call(None, b"/", None, MS_REC | MS_PRIVATE, None) != 0:
        fail("private_propagation", os.strerror(ctypes.get_errno()))

    target = f"/proc/self/fd/{anchor_fd}"
    mounted = subprocess.run(
        [mount_helper, "-t", "tmpfs", "-o", "size=8M,mode=0700,nosuid,nodev,noexec", "tmpfs", target],
        pass_fds=(anchor_fd,),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if mounted.returncode != 0:
        fail("descriptor_mount", mounted.stderr or mounted.stdout)

    runtime_fd = os.open(anchor_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if statfs_type(f"/proc/self/fd/{runtime_fd}") != TMPFS_MAGIC:
            fail("bounded_tmpfs", "R is not tmpfs")
        info = os.fstat(runtime_fd)
        if stat.S_IMODE(info.st_mode) != 0o700:
            fail("bounded_tmpfs", f"mode={oct(stat.S_IMODE(info.st_mode))}")
        volume = os.statvfs(f"/proc/self/fd/{runtime_fd}")
        if volume.f_blocks * volume.f_frsize > 8 * 1024 * 1024:
            fail("bounded_tmpfs", "tmpfs exceeds 8 MiB")
        created_mount_id = fd_mount_id(runtime_fd)
        before_ids = mount_ids()
        if created_mount_id not in before_ids:
            fail("mount_identity", "R mount ID missing")

        nested_target = anchor_path / "nested-mount-target"
        nested_target.mkdir(mode=0o700)
        nested = r'''
import ctypes, ctypes.util, os, stat, sys
rfd = int(sys.argv[1]); sample = sys.argv[2]; target = sys.argv[3]
anchor_identity = (int(sys.argv[4]), int(sys.argv[5]))
if open(sample, encoding="utf-8").read() != "same-user-access": raise SystemExit("sample access failed")
allowed = {0, 1, 2, rfd}
observed = {int(name) for name in os.listdir("/proc/self/fd") if name.isdigit()}
extras = observed - allowed
for fd in sorted(extras):
    try: info = os.fstat(fd)
    except OSError: continue
    if stat.S_ISFIFO(info.st_mode): raise SystemExit(f"unexpected inherited pipe FD {fd}")
    if (info.st_dev, info.st_ino) == anchor_identity: raise SystemExit(f"pre-mount anchor FD leaked as {fd}")
libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
libc.mount.restype = ctypes.c_int
if libc.mount(b"tmpfs", target.encode(), b"tmpfs", 0, b"size=1M") == 0: raise SystemExit("nested child gained outer mount administration")
'''
        unshare = subprocess.run(
            [
                unshare_helper,
                "--user",
                "--map-root-user",
                "--fork",
                sys.executable,
                "-c",
                nested,
                str(runtime_fd),
                str(sample_path),
                str(nested_target),
                str(os.fstat(anchor_fd).st_dev),
                str(os.fstat(anchor_fd).st_ino),
            ],
            pass_fds=(runtime_fd,),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if unshare.returncode != 0:
            fail("nested_user", unshare.stderr or unshare.stdout)

        detached = subprocess.run(
            [umount_helper, "-l", f"/proc/self/fd/{runtime_fd}"],
            pass_fds=(runtime_fd,),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if detached.returncode != 0 or created_mount_id in mount_ids():
            fail("detach_mountinfo", detached.stderr or detached.stdout)
    finally:
        os.close(runtime_fd)

    visible = sorted(int(name) for name in os.listdir("/proc") if name.isdigit())
    if visible != [1]:
        fail("pid1_proc", f"residual /proc PIDs {visible}")
    print(json.dumps({"status": "pass", "mount_id": created_mount_id}, separators=(",", ":")))


if __name__ == "__main__":
    run()
