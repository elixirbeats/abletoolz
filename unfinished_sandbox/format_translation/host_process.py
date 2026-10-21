"""Bound native workers and their Windows bridge descendants to one lifetime."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class WorkerJob:
    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.name = "AbletoolzMapper-" + uuid.uuid4().hex
        self.handle = self.kernel.CreateJobObjectW(None, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def attach_worker() -> None:
    """Join the parent's job before loading plugins or starting bridge helpers."""
    name = os.environ.get("ABLETOOLZ_WORKER_JOB")
    if sys.platform != "win32" or not name:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenJobObjectW(1, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def run_worker(command: list[str], out: Path, mode: str, timeout: int) -> None:
    job = WorkerJob() if sys.platform == "win32" else None
    env = dict(os.environ)
    if job is not None:
        env["ABLETOOLZ_WORKER_JOB"] = job.name
    stdout_path, stderr_path = out / f"{mode}.stdout.log", out / f"{mode}.stderr.log"
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                env=env,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                if job is not None:
                    job.close()
                process.kill()
                process.wait(timeout=10)
                (out / f"{mode}.timeout.txt").write_text(str(error), encoding="utf-8")
                raise RuntimeError(f"{mode} exceeded {timeout}s; partial evidence retained at {out}") from error
    finally:
        if job is not None:
            job.close()
    print(stdout_path.read_text(encoding="utf-8"), end="", flush=True)
    if code:
        raise RuntimeError(f"{mode} worker failed ({code}): {stderr_path.read_text(encoding='utf-8')[-1500:]}")
