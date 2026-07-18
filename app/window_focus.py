from __future__ import annotations

import ctypes
import ntpath
import sys
import threading
from ctypes import wintypes


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_cache_lock = threading.Lock()
_cached_foreground_identity: tuple[int, int] | None = None
_cached_foreground_process_name: str | None = None


def _reset_foreground_process_cache() -> None:
    """Clear the foreground process cache (used when focus is unavailable and by tests)."""
    global _cached_foreground_identity, _cached_foreground_process_name
    with _cache_lock:
        _cached_foreground_identity = None
        _cached_foreground_process_name = None


def get_foreground_process_name() -> str | None:
    """Return active foreground executable name in lowercase, or None on failure."""
    if sys.platform != "win32":
        return None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        _reset_foreground_process_cache()
        return None

    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if process_id.value == 0:
        _reset_foreground_process_cache()
        return None

    foreground_identity = (int(hwnd), process_id.value)
    global _cached_foreground_identity, _cached_foreground_process_name
    with _cache_lock:
        if foreground_identity == _cached_foreground_identity:
            return _cached_foreground_process_name

        process_handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id.value,
        )
        executable: str | None = None
        if process_handle:
            try:
                buffer_size = wintypes.DWORD(32768)
                name_buffer = ctypes.create_unicode_buffer(buffer_size.value)
                succeeded = kernel32.QueryFullProcessImageNameW(
                    process_handle,
                    0,
                    name_buffer,
                    ctypes.byref(buffer_size),
                )
                if succeeded:
                    executable = ntpath.basename(name_buffer.value).strip().lower() or None
            finally:
                kernel32.CloseHandle(process_handle)

        _cached_foreground_identity = foreground_identity
        _cached_foreground_process_name = executable
        return executable
