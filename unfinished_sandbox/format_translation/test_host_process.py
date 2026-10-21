from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

import pytest
from host_process import run_worker


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object lifetime contract")
@pytest.mark.parametrize("timeout", [False, True])
def test_worker_lifetime_cleans_its_descendants(tmp_path: Path, timeout: bool) -> None:
    code = (
        "import subprocess,sys,os,time; from pathlib import Path; "
        f"sys.path.insert(0,{str(Path(__file__).resolve().parent)!r}); "
        "from host_process import attach_worker; attach_worker(); "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        f"Path({str(tmp_path / 'descendant.pid')!r}).write_text(str(child.pid)); "
        + ("time.sleep(60)" if timeout else "os._exit(0)")
    )
    if timeout:
        with pytest.raises(RuntimeError, match="exceeded"):
            run_worker([sys.executable, "-c", code], tmp_path, "test", 2)
    else:
        run_worker([sys.executable, "-c", code], tmp_path, "test", 5)
    pid = int((tmp_path / "descendant.pid").read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return
        status = ctypes.c_uint()
        kernel.GetExitCodeProcess(handle, ctypes.byref(status))
        kernel.CloseHandle(handle)
        if status.value != 259:
            return
        time.sleep(0.02)
    pytest.fail("Disposable worker left a running descendant")
