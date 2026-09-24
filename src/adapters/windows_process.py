"""Graceful closure of a window owned by an identified run process."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


def process_creation_time(pid: int) -> int | None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def request_window_close(record: dict) -> dict:
    """Request normal close only; never force past an unsaved-document dialog."""
    pid = record.get("process_id")
    started = record.get("process_creation_time")
    if not pid or started is None or process_creation_time(pid) != started:
        return {"requested": False, "reason": "Process exited or identity is unavailable"}
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user.IsWindowVisible.argtypes = (wintypes.HWND,)
    user.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    count = 0

    @callback_type
    def visit(window, _):
        nonlocal count
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(window):
            count += bool(user.PostMessageW(window, 0x0010, 0, 0))
        return True

    user.EnumWindows(visit, 0)
    return {"requested": bool(count), "windows": count, "process_id": pid}
